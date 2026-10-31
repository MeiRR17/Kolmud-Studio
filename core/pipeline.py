"""Service layer: the whole lesson workflow, persisted in SQLite, shared by both UIs.

Hebrew audio -> Whisper transcript -> LLM translation (display / TTS-phonetic / coaching)
-> cloned-voice segments -> master WAV/MP3/SRT, plus shadowing practice scoring.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from core import audio_utils
from core.accent_evaluator import AccentAndFluencyEvaluator, normalize_text
from core.asr_whisper import LocalWhisperEngine
from core.config import Settings
from core.database import Lesson, PracticeAttempt, Segment, session_scope
from core.lexicon import PhoneticLexicon, clean_for_tts
from core.llm_translator import (
    AnthropicProvider,
    JSONCompletionProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    TorahTranslatorEngine,
)
from core.schemas import AttemptOut, LessonOut, LessonSummary, MasterOutputs, SegmentOut, SegmentUpdate
from core.prosody import add_content_word_stress, auto_coach_guide
from core.runtime import exclusive_inference
from core.tts_cloner import ElevenLabsVoiceCloner, TTSEngine, TTSError, XTTSVoiceCloner


class NotFoundError(LookupError):
    pass


class WorkflowStateError(RuntimeError):
    """Inputs changed or required outputs are missing; further processing is unsafe."""


def file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


@dataclass
class LLMChoice:
    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""


@dataclass
class TTSChoice:
    engine: str
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""


class StudioService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.configure_runtime()
        self.db_url = settings.database_url
        self.lexicon = PhoneticLexicon(settings.lexicon_path)
        self.asr = LocalWhisperEngine(settings.hebrew_asr_model, settings.english_asr_model)
        self._xtts: XTTSVoiceCloner | None = None
        self._xtts_lock = threading.Lock()

    # ------------------------------------------------------------------ paths
    def _lesson_dir(self, lesson_id: int) -> Path:
        return self.settings.projects_dir / f"lesson_{lesson_id}"

    def _rel(self, path: Path) -> str:
        return str(Path(path).resolve().relative_to(self.settings.data_dir.resolve()))

    def abs_path(self, stored: str | None) -> Path | None:
        return self.settings.data_dir / stored if stored else None

    @staticmethod
    def _invalidate_master(lesson: Lesson) -> None:
        lesson.master_audio_path = None
        lesson.master_signature = None

    @staticmethod
    def _require_current_translation(lesson: Lesson) -> None:
        if not lesson.segments or lesson.translation_is_stale:
            raise WorkflowStateError("Translate the current Hebrew transcript before generating or exporting audio.")

    # ------------------------------------------------------------------ engines
    def default_llm_choice(self) -> LLMChoice:
        s = self.settings
        if s.llm_provider == "openai":
            return LLMChoice("openai", s.openai_model, s.openai_api_key, s.openai_base_url)
        if s.llm_provider == "anthropic":
            return LLMChoice("anthropic", s.anthropic_model, s.anthropic_api_key)
        return LLMChoice("ollama", s.ollama_model, base_url=s.ollama_base_url)

    def make_llm_provider(self, choice: LLMChoice | None = None) -> JSONCompletionProvider:
        c = choice or self.default_llm_choice()
        s = self.settings
        if c.provider == "ollama":
            return OllamaProvider(
                model=c.model, base_url=(c.base_url or s.ollama_base_url).rstrip("/"),
                num_ctx=s.ollama_num_ctx, temperature=s.llm_temperature, timeout=s.llm_timeout_seconds,
            )
        if c.provider == "openai":
            return OpenAICompatibleProvider(
                model=c.model, api_key=c.api_key or s.openai_api_key,
                base_url=c.base_url or s.openai_base_url, temperature=s.llm_temperature,
                timeout=s.llm_timeout_seconds,
            )
        if c.provider == "anthropic":
            return AnthropicProvider(model=c.model, api_key=c.api_key or s.anthropic_api_key,
                                     timeout=s.llm_timeout_seconds)
        raise ValueError(f"Unknown LLM provider: {c.provider}")

    def default_tts_choice(self) -> TTSChoice:
        s = self.settings
        return TTSChoice(s.tts_engine, s.elevenlabs_api_key, s.elevenlabs_voice_id)

    def make_tts_engine(self, choice: TTSChoice | None = None) -> TTSEngine:
        c = choice or self.default_tts_choice()
        if c.engine == "elevenlabs":
            return ElevenLabsVoiceCloner(
                c.elevenlabs_api_key or self.settings.elevenlabs_api_key,
                c.elevenlabs_voice_id or self.settings.elevenlabs_voice_id,
                self.settings.elevenlabs_model,
            )
        with self._xtts_lock:  # one resident XTTS model per process
            if self._xtts is None:
                self._xtts = XTTSVoiceCloner(
                    reference_wav=self.settings.reference_voice_path,
                    device_preference=self.settings.tts_device,
                    temperature=self.settings.xtts_temperature,
                    speed=self.settings.xtts_speed,
                )
            return self._xtts

    # ------------------------------------------------------------------ lessons
    def list_lessons(self) -> list[LessonSummary]:
        with session_scope(self.db_url) as s:
            rows = s.scalars(select(Lesson).order_by(Lesson.created_at.desc())).all()
            return [LessonSummary.model_validate(r) for r in rows]

    def get_lesson(self, lesson_id: int) -> LessonOut:
        with session_scope(self.db_url) as s:
            return LessonOut.model_validate(self._lesson(s, lesson_id))

    @staticmethod
    def _lesson(s, lesson_id: int) -> Lesson:  # noqa: ANN001
        lesson = s.get(Lesson, lesson_id)
        if lesson is None:
            raise NotFoundError(f"Lesson {lesson_id} not found")
        return lesson

    @staticmethod
    def _segment(s, segment_id: int) -> Segment:  # noqa: ANN001
        seg = s.get(Segment, segment_id)
        if seg is None:
            raise NotFoundError(f"Segment {segment_id} not found")
        return seg

    def create_lesson(self, title: str) -> LessonOut:
        title = title.strip() or f"שיעור {datetime.now():%Y-%m-%d %H:%M}"
        with session_scope(self.db_url) as s:
            lesson = Lesson(title=title)
            s.add(lesson)
            s.flush()
            self._lesson_dir(lesson.id).mkdir(parents=True, exist_ok=True)
            return LessonOut.model_validate(lesson)

    def rename_lesson(self, lesson_id: int, title: str) -> LessonOut:
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            lesson.title = title.strip() or lesson.title
            return LessonOut.model_validate(lesson)

    def delete_lesson(self, lesson_id: int) -> None:
        with session_scope(self.db_url) as s:
            s.delete(self._lesson(s, lesson_id))
        shutil.rmtree(self._lesson_dir(lesson_id), ignore_errors=True)

    # ------------------------------------------------------------------ step 1: Hebrew
    def attach_source_audio(self, lesson_id: int, filename: str, data: bytes) -> LessonOut:
        self.get_lesson(lesson_id)
        suffix = Path(filename).suffix.lower() or ".bin"
        src_dir = self._lesson_dir(lesson_id) / "source" / uuid.uuid4().hex
        src_dir.mkdir(parents=True, exist_ok=True)
        original = src_dir / f"original{suffix}"
        original.write_bytes(data)
        wav = audio_utils.to_wav(original, src_dir / "source_16k.wav", sample_rate=16000)
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            lesson.source_audio_path = self._rel(wav)
            lesson.hebrew_transcript = ""
            lesson.asr_segments = None
            lesson.translation_source_text = None
            self._invalidate_master(lesson)
            return LessonOut.model_validate(lesson)

    @exclusive_inference
    def transcribe(self, lesson_id: int) -> LessonOut:
        with session_scope(self.db_url) as s:
            source = self.abs_path(self._lesson(s, lesson_id).source_audio_path)
        if source is None or not source.exists():
            raise FileNotFoundError("Upload the Hebrew lecture audio first.")
        result = self.asr.transcribe_hebrew_lecture(
            source, use_context_prompt=self.settings.hebrew_asr_context_prompt
        )
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            if self.abs_path(lesson.source_audio_path) != source:
                raise WorkflowStateError("Source audio changed during transcription; retry the current recording.")
            lesson.hebrew_transcript = result["full_text"]
            lesson.asr_segments = list(result["segments"])
            self._invalidate_master(lesson)
            return LessonOut.model_validate(lesson)

    def set_transcript(self, lesson_id: int, text: str) -> LessonOut:
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            if lesson.hebrew_transcript != text:
                lesson.hebrew_transcript = text
                lesson.asr_segments = None
                self._invalidate_master(lesson)
            return LessonOut.model_validate(lesson)

    # ------------------------------------------------------------------ step 2: translation
    @exclusive_inference
    def translate(
        self,
        lesson_id: int,
        llm: LLMChoice | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> LessonOut:
        with session_scope(self.db_url) as s:
            hebrew = self._lesson(s, lesson_id).hebrew_transcript
        provider = self.make_llm_provider(llm)
        translator = TorahTranslatorEngine(
            provider=provider,
            lexicon=self.lexicon,
            system_prompt_path=self.settings.prompt_path,
            chunk_chars=self.settings.translation_chunk_chars,
            coach_guide_prompt_path=(
                self.settings.coach_guide_prompt_path if self.settings.translation_llm_coach_guide else None
            ),
        )
        try:
            translated = translator.process_transcript(hebrew, on_progress=on_progress)
        finally:
            if self.settings.ollama_unload_after_translation and isinstance(provider, OllamaProvider):
                provider.unload()

        seg_dir = self._lesson_dir(lesson_id) / "segments"
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            if lesson.hebrew_transcript != hebrew:
                raise WorkflowStateError("The transcript changed during translation; retry with the current text.")
            lesson.segments.clear()  # cascades; practice history belongs to the old segments
            self._invalidate_master(lesson)
            lesson.translation_source_text = hebrew
            for t in translated:
                lesson.segments.append(Segment(
                    position=t.segment_id,
                    hebrew_clean=t.hebrew_clean,
                    english_display=t.english_display,
                    english_tts_phonetic=t.english_tts_phonetic,
                    shadowing_coach_guide=t.shadowing_coach_guide,
                    hebrew_terms=[term.model_dump() for term in t.hebrew_terms],
                ))
            s.flush()
            out = LessonOut.model_validate(lesson)
        shutil.rmtree(seg_dir, ignore_errors=True)
        return out

    def update_segment(self, segment_id: int, update: SegmentUpdate) -> SegmentOut:
        with session_scope(self.db_url) as s:
            seg = self._segment(s, segment_id)
            changes = update.model_dump(exclude_none=True)
            display_changed = "english_display" in changes and changes["english_display"] != seg.english_display
            for field, value in changes.items():
                setattr(seg, field, value)
            if display_changed:
                terms = {t["term"]: t["phonetic"] for t in (seg.hebrew_terms or [])}
                if "english_tts_phonetic" not in changes:
                    seg.english_tts_phonetic = self.lexicon.apply(clean_for_tts(seg.english_display), extra=terms)
                if "shadowing_coach_guide" not in changes:
                    seg.shadowing_coach_guide = add_content_word_stress(auto_coach_guide(seg.english_display))
            if changes:
                self._invalidate_master(seg.lesson)
            return SegmentOut.model_validate(seg)

    def regenerate_phonetic(self, segment_id: int) -> SegmentOut:
        """Rebuild the TTS script deterministically from the (edited) display text + lexicon."""
        with session_scope(self.db_url) as s:
            seg = self._segment(s, segment_id)
            terms = {t["term"]: t["phonetic"] for t in (seg.hebrew_terms or [])}
            seg.english_tts_phonetic = self.lexicon.apply(clean_for_tts(seg.english_display), extra=terms)
            self._invalidate_master(seg.lesson)
            return SegmentOut.model_validate(seg)

    # ------------------------------------------------------------------ step 3: voice
    @exclusive_inference
    def synthesize_segment(self, segment_id: int, tts: TTSChoice | None = None) -> SegmentOut:
        with session_scope(self.db_url) as s:
            seg = self._segment(s, segment_id)
            self._require_current_translation(seg.lesson)
            text, lesson_id = seg.english_tts_phonetic, seg.lesson_id
        if not text.strip():
            raise TTSError("This segment has no TTS script.")
        engine = self.make_tts_engine(tts)
        # Unique paths preserve previous audio if synthesis fails or an edit races it.
        out = self._lesson_dir(lesson_id) / "segments" / f"seg_{segment_id}_{uuid.uuid4().hex}.wav"
        engine.synthesize(text, out)
        with session_scope(self.db_url) as s:
            seg = self._segment(s, segment_id)
            self._require_current_translation(seg.lesson)
            if seg.english_tts_phonetic != text:
                raise WorkflowStateError("The TTS script changed during synthesis; retry the segment.")
            seg.audio_path = self._rel(out)
            seg.audio_duration = audio_utils.duration_seconds(out)
            seg.audio_source_text = text
            self._invalidate_master(seg.lesson)
            return SegmentOut.model_validate(seg)

    def segment_audio(self, segment_id: int, rate: float = 1.0) -> Path:
        with session_scope(self.db_url) as s:
            path = self.abs_path(self._segment(s, segment_id).audio_path)
        if path is None or not path.exists():
            raise FileNotFoundError("No audio has been generated for this segment yet.")
        return audio_utils.tempo_variant(path, rate)

    def _export_inputs(self, lesson: Lesson) -> tuple[list[Path], list[str], str]:
        self._require_current_translation(lesson)
        missing, stale, paths, texts, fingerprints = [], [], [], [], []
        for seg in lesson.segments:
            path = self.abs_path(seg.audio_path)
            if path is None or not path.is_file():
                missing.append(seg.position)
            elif seg.audio_is_stale:
                stale.append(seg.position)
            else:
                paths.append(path)
                texts.append(seg.english_display)
                fingerprints.append([seg.id, seg.english_display, seg.english_tts_phonetic, file_digest(path)])
        if missing or stale:
            raise WorkflowStateError(f"Cannot export an incomplete lesson. Missing audio: {missing}; stale audio: {stale}.")
        signature = hashlib.sha256(json.dumps(
            [lesson.hebrew_transcript, fingerprints, self.settings.segment_pause_ms], ensure_ascii=False
        ).encode()).hexdigest()
        return paths, texts, signature

    @staticmethod
    def _master_signature(inputs: str, wav: Path) -> str:
        return hashlib.sha256((inputs + "".join(file_digest(wav.with_suffix(ext)) for ext in (".wav", ".mp3", ".srt"))).encode()).hexdigest()

    def get_master(self, lesson_id: int) -> MasterOutputs:
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            _, texts, signature = self._export_inputs(lesson)
            wav = self.abs_path(lesson.master_audio_path)
            if wav is None or not all(wav.with_suffix(ext).is_file() for ext in (".wav", ".mp3", ".srt")):
                raise WorkflowStateError("Build the complete master files first.")
            if lesson.master_signature != self._master_signature(signature, wav):
                raise WorkflowStateError("Master files are out of date or changed; rebuild them.")
        return MasterOutputs(wav_path=str(wav), mp3_path=str(wav.with_suffix(".mp3")),
                             srt_path=str(wav.with_suffix(".srt")),
                             duration_seconds=audio_utils.duration_seconds(wav), segment_count=len(texts))

    @exclusive_inference
    def build_master(self, lesson_id: int) -> MasterOutputs:
        with session_scope(self.db_url) as s:
            paths, texts, signature = self._export_inputs(self._lesson(s, lesson_id))
        master_dir = self._lesson_dir(lesson_id) / "master" / uuid.uuid4().hex
        wav = master_dir / "full_english_shiur.wav"
        spans = audio_utils.merge_segments(paths, wav, self.settings.segment_pause_ms)
        mp3 = audio_utils.wav_to_mp3(wav, master_dir / "full_english_shiur.mp3")
        srt = audio_utils.write_srt(
            [audio_utils.SubtitleCue(a, b, text) for (a, b), text in zip(spans, texts)],
            master_dir / "full_english_shiur.srt",
        )
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            if self._export_inputs(lesson)[2] != signature:
                raise WorkflowStateError("Lesson changed during export; rebuild the master.")
            lesson.master_audio_path = self._rel(wav)
            lesson.master_signature = self._master_signature(signature, wav)
        return MasterOutputs(
            wav_path=str(wav), mp3_path=str(mp3), srt_path=str(srt),
            duration_seconds=round(spans[-1][1], 2), segment_count=len(paths),
        )

    @exclusive_inference
    def preview_pronunciation(self, phonetic: str, tts: TTSChoice | None = None) -> Path:
        out = self.settings.data_dir / "previews" / f"preview_{uuid.uuid4().hex[:8]}.wav"
        for old in out.parent.glob("preview_*.wav"):
            old.unlink(missing_ok=True)
        return self.make_tts_engine(tts).synthesize(f"The term is, {phonetic}.", out)

    # ------------------------------------------------------------------ voice profile
    @exclusive_inference
    def save_reference_voice(self, filename: str, data: bytes) -> Path:
        """Store the cloning reference as 24 kHz mono WAV, whatever format it arrived in."""
        dst = self.settings.reference_voice_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f"upload{Path(filename).suffix or '.wav'}")
        tmp.write_bytes(data)
        try:
            audio_utils.to_wav(tmp, dst, sample_rate=24000)
        finally:
            tmp.unlink(missing_ok=True)
        return dst

    # ------------------------------------------------------------------ practice lab
    def _evaluator(self) -> AccentAndFluencyEvaluator:
        hebrew_words = {w for term in self.lexicon.entries() for w in normalize_text(term).split()}
        return AccentAndFluencyEvaluator(is_hebrew_term=lambda w: w in hebrew_words)

    @exclusive_inference
    def evaluate_practice(self, segment_id: int, data: bytes, suffix: str = ".wav") -> AttemptOut:
        with session_scope(self.db_url) as s:
            seg = self._segment(s, segment_id)
            target, lesson_id = seg.english_display, seg.lesson_id
        practice_dir = self._lesson_dir(lesson_id) / "practice"
        practice_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        raw = practice_dir / f"seg_{segment_id}_{stamp}_raw{suffix}"
        raw.write_bytes(data)
        wav = audio_utils.to_wav(raw, practice_dir / f"seg_{segment_id}_{stamp}.wav", sample_rate=16000)
        raw.unlink(missing_ok=True)

        heard = self.asr.transcribe_english_practice(wav)
        report = self._evaluator().evaluate_attempt(
            target_english_text=target,
            whisper_recognized_text=heard["recognized_text"],
            word_timings=list(heard["words"]),
            audio_duration_seconds=audio_utils.duration_seconds(wav),
        )
        payload = report.to_dict()
        payload["words"] = list(heard["words"])
        with session_scope(self.db_url) as s:
            attempt = PracticeAttempt(
                segment_id=segment_id,
                audio_path=self._rel(wav),
                recognized_text=heard["recognized_text"],
                clarity_score=report.clarity_score,
                strict_wer=report.strict_wer,
                wpm=report.wpm,
                report=payload,
            )
            s.add(attempt)
            s.flush()
            return AttemptOut.model_validate(attempt)

    def list_attempts(self, segment_id: int) -> list[AttemptOut]:
        with session_scope(self.db_url) as s:
            rows = s.scalars(
                select(PracticeAttempt).where(PracticeAttempt.segment_id == segment_id)
                .order_by(PracticeAttempt.created_at)
            ).all()
            return [AttemptOut.model_validate(r) for r in rows]
