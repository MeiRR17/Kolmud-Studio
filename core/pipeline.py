"""Service layer: the whole lesson workflow, persisted in SQLite, shared by both UIs.

Hebrew audio -> Whisper transcript -> LLM translation (display / TTS-phonetic / coaching)
-> cloned-voice segments -> master WAV/MP3/SRT, plus shadowing practice scoring.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from core import audio_utils
from core import __version__
from core.artifacts import private_json
from core.accent_evaluator import AccentAndFluencyEvaluator, normalize_text
from core.asr_whisper import LocalWhisperEngine
from core.config import Settings
from core.database import Lesson, PracticeAttempt, Segment, SourceCue, session_scope
from core.lexicon import PhoneticLexicon, clean_for_tts
from core.llm_translator import (
    AnthropicProvider,
    JSONCompletionProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    TorahTranslatorEngine,
)
from core.schemas import AttemptOut, LessonOut, LessonSummary, MasterOutputs, SegmentOut, SegmentUpdate
from core.schemas import SourceCaptionOutputs, SourceCueOut, SourceCueUpdate, SourceTimelineOut
from core.schemas import DubbingPlan, SynchronizedOutputs, GeneratedVideoOutputs
from core.source_timing import cue_flags, normalized, source_records, validate_interval, write_anchored_srt
from core.prosody import add_content_word_stress, auto_coach_guide
from core.runtime import exclusive_inference
from core.tts_cloner import ElevenLabsVoiceCloner, TTSEngine, TTSError, XTTSVoiceCloner


class NotFoundError(LookupError):
    pass


class WorkflowStateError(RuntimeError):
    """Inputs changed or required outputs are missing; further processing is unsafe."""


class VoiceRejectedError(WorkflowStateError):
    """The reference recording can't be used for cloning; the previous one is kept."""

    def __init__(self, check: audio_utils.VoiceCheck) -> None:
        self.check = check
        super().__init__("Reference voice rejected: " + " ".join(
            audio_utils.VOICE_CHECK_MESSAGES[code] for code in check.errors))


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
            lesson.source_asset = {"original_path": self._rel(original), "original_sha256": file_digest(original),
                                   "normalized_sha256": file_digest(wav)}
            lesson.hebrew_transcript = ""
            lesson.asr_segments = None
            lesson.timeline_revision = None
            lesson.translation_source_text = None
            self._invalidate_master(lesson)
            return LessonOut.model_validate(lesson)

    @exclusive_inference
    def transcribe(self, lesson_id: int) -> LessonOut:
        with session_scope(self.db_url) as s:
            source = self.abs_path(self._lesson(s, lesson_id).source_audio_path)
        if source is None or not source.exists():
            raise FileNotFoundError("Upload the Hebrew lecture audio first.")
        source_hash = file_digest(source)
        result = self.asr.transcribe_hebrew_lecture(
            source, use_context_prompt=self.settings.hebrew_asr_context_prompt
        )
        if file_digest(source) != source_hash:
            raise WorkflowStateError("Source audio changed during transcription; retry.")
        try:
            records = source_records(list(result["segments"]), audio_utils.duration_seconds(source))
        except (ValueError, KeyError, TypeError) as exc:
            raise WorkflowStateError("ASR returned invalid source timestamps; review or retry transcription.") from exc
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            if self.abs_path(lesson.source_audio_path) != source:
                raise WorkflowStateError("Source audio changed during transcription; retry the current recording.")
            lesson.hebrew_transcript = result["full_text"]
            lesson.asr_segments = list(result["segments"])
            lesson.timeline_revision = uuid.uuid4().hex
            for position, record in enumerate(records, start=1):
                lesson.source_cues.append(SourceCue(
                    revision=lesson.timeline_revision, position=position,
                    source_audio_path=lesson.source_audio_path, source_audio_sha256=source_hash, **record,
                ))
            s.flush()
            self._invalidate_master(lesson)
            return LessonOut.model_validate(lesson)

    def set_transcript(self, lesson_id: int, text: str) -> LessonOut:
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            if lesson.hebrew_transcript != text:
                lesson.hebrew_transcript = text
                # Keep the original ASR evidence; arbitrary full-text edits make
                # its timeline stale until the source cues are explicitly edited.
                self._invalidate_master(lesson)
            return LessonOut.model_validate(lesson)

    # --------------------------------------------------- source-time review/captions
    def _source_snapshot(self, lesson: Lesson) -> str:
        cues = lesson.current_source_cues
        if not cues or lesson.source_timeline_is_stale:
            raise WorkflowStateError("Current source cues are missing or differ from the transcript; review the source timeline first.")
        source = self.abs_path(lesson.source_audio_path)
        if source is None or not source.is_file():
            raise WorkflowStateError("The original source recording is missing.")
        digest = file_digest(source)
        if lesson.source_asset:
            if digest != lesson.source_asset["normalized_sha256"]:
                raise WorkflowStateError("The normalized source changed after import; import the intended recording again.")
            original = self.abs_path(lesson.source_asset["original_path"])
            if original is None or not original.is_file() or file_digest(original) != lesson.source_asset["original_sha256"]:
                raise WorkflowStateError("The original media file is missing or changed after import.")
        if any(cue.source_audio_path != lesson.source_audio_path or cue.source_audio_sha256 != digest for cue in cues):
            raise WorkflowStateError("The recording changed after transcription; transcribe the current source.")
        duration = audio_utils.duration_seconds(source)
        previous_end = 0.0
        for cue in cues:
            try:
                validate_interval(cue.start, cue.end, duration)
            except ValueError as exc:
                raise WorkflowStateError(str(exc)) from exc
            if cue.start < previous_end - 0.0001:
                raise WorkflowStateError("Overlapping source cues require timing correction.")
            previous_end = cue.end
        payload = [lesson.timeline_revision, digest, lesson.source_asset,
                   [[cue.id, cue.edit_version, cue.start, cue.end, cue.text] for cue in cues]]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    def get_source_timeline(self, lesson_id: int) -> SourceTimelineOut:
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            flags = []
            try:
                self._source_snapshot(lesson)
            except WorkflowStateError as exc:
                flags.append(str(exc))
            cues = []
            previous_end = 0.0
            for cue in lesson.current_source_cues:
                out = SourceCueOut.model_validate(cue)
                out.flags = cue_flags(cue)
                if cue.start < previous_end - 0.0001:
                    out.flags.append("overlapping_source_cues")
                previous_end = cue.end
                cues.append(out)
            return SourceTimelineOut(lesson_id=lesson.id, revision=lesson.timeline_revision,
                                     stale=bool(flags), flags=flags, cues=cues,
                                     pending_review=sum(not cue.reviewed for cue in cues))

    def update_source_cue(self, cue_id: int, update: SourceCueUpdate) -> SourceCueOut:
        with session_scope(self.db_url) as s:
            # Serialize cue edits before checking versions and rebuilding the
            # lesson text so two different cue edits cannot overwrite each other.
            s.connection().exec_driver_sql("BEGIN IMMEDIATE")
            cue = s.get(SourceCue, cue_id)
            if cue is None:
                raise NotFoundError(f"Source cue {cue_id} not found")
            lesson = cue.lesson
            if cue.revision != lesson.timeline_revision:
                raise WorkflowStateError("Historical source cues are read-only.")
            if lesson.source_timeline_is_stale:
                raise WorkflowStateError("Full-text edits differ from the source cues; restore the matching text or transcribe again before cue editing.")
            if cue.edit_version != update.expected_version:
                raise WorkflowStateError("This cue was edited elsewhere; reload its current version.")
            changes = update.model_dump(exclude_none=True, exclude={"expected_version"})
            start, end = changes.get("start", cue.start), changes.get("end", cue.end)
            source = self.abs_path(lesson.source_audio_path)
            if source is None or not source.is_file() or cue.source_audio_sha256 != file_digest(source):
                raise WorkflowStateError("The source recording is missing or changed.")
            try:
                validate_interval(start, end, audio_utils.duration_seconds(source))
            except ValueError as exc:
                raise WorkflowStateError(str(exc)) from exc
            cues = lesson.current_source_cues
            index = next(i for i, current in enumerate(cues) if current.id == cue_id)
            if (index and start < cues[index - 1].end - 0.0001) or (index + 1 < len(cues) and end > cues[index + 1].start + 0.0001):
                raise WorkflowStateError("Edited timing overlaps a neighboring source cue.")
            before = {"text": cue.text, "start": cue.start, "end": cue.end, "reviewed": cue.reviewed}
            content_changed = any(key in changes and changes[key] != before[key] for key in ("text", "start", "end"))
            text_changed = "text" in changes and changes["text"] != before["text"]
            for key, value in changes.items():
                setattr(cue, key, value)
            if content_changed:
                cue.timing_basis = "manual_segment"
                cue.reviewed = changes.get("reviewed", False)
                if text_changed:
                    lesson.hebrew_transcript = "\n\n".join(current.text for current in cues)
                self._invalidate_master(lesson)
            cue.edit_version += 1
            cue.edits = [*cue.edits, {"at": datetime.now(timezone.utc).isoformat(), "before": before,
                                     "after": {"text": cue.text, "start": cue.start, "end": cue.end,
                                               "reviewed": cue.reviewed}}]
            s.flush()
            out = SourceCueOut.model_validate(cue)
            out.flags = cue_flags(cue)
            return out

    def build_source_captions(self, lesson_id: int, include_english: bool = True) -> SourceCaptionOutputs:
        """Draft captions on the original timeline, distinct from dubbed-audio SRT."""
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            signature = self._source_snapshot(lesson)
            source_cues = lesson.current_source_cues
            translations = {}
            if include_english:
                self._require_current_translation(lesson)
                if len(lesson.segments) != len(source_cues) or {seg.source_cue_id for seg in lesson.segments} != {cue.id for cue in source_cues}:
                    raise WorkflowStateError("Translate with source_timed=true to obtain explicit source-to-English links.")
                translations = {seg.source_cue_id: seg.english_display for seg in lesson.segments}
                if any(not text.strip() for text in translations.values()):
                    raise WorkflowStateError("A source cue has an empty English translation.")
            snapshots = [{**SourceCueOut.model_validate(cue).model_dump(), "flags": cue_flags(cue),
                          "english_text": translations.get(cue.id)} for cue in source_cues]
            pending = sum(not cue.reviewed for cue in source_cues)
            revision = lesson.timeline_revision
            source_path = lesson.source_audio_path
        directory = self._lesson_dir(lesson_id) / "source_captions" / uuid.uuid4().hex
        he = write_anchored_srt([(cue["start"], cue["end"], cue["text"]) for cue in snapshots], directory / "source.he.srt")
        en = (write_anchored_srt([(cue["start"], cue["end"], cue["english_text"]) for cue in snapshots], directory / "source.en.srt")
              if include_english else None)
        # Recheck before handing a generation to the caller. Partial generations
        # stay unreferenced and never become a current publication candidate.
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            if self._source_snapshot(lesson) != signature or (include_english and (
                lesson.translation_is_stale or {seg.source_cue_id: seg.english_display for seg in lesson.segments} != translations
            )):
                raise WorkflowStateError("Source/translation changed during caption export; rebuild it.")
        manifest = directory / "manifest.json"
        private_json(manifest, {"schemaVersion": 1, "producer": "Kolmud Studio", "version": __version__,
                               "lessonId": lesson_id, "timelineRevision": revision, "sourceSnapshot": signature,
                               "sourceAudioPath": source_path, "timeline": "original_source",
                               "timingPrecision": "segment_anchors_not_forced_alignment", "cues": snapshots,
                               "sourcePendingReview": pending, "translationReview": "not_recorded" if en else "not_applicable",
                               "publicationReady": False,
                               "files": [{"path": str(path), "sha256": file_digest(path)} for path in (he, en) if path]})
        return SourceCaptionOutputs(hebrew_srt_path=str(he), english_srt_path=str(en) if en else None,
                                    manifest_path=str(manifest), timeline_revision=revision,
                                    source_cue_count=len(snapshots), pending_review=pending)

    # ------------------------------------------------------------------ step 2: translation
    @exclusive_inference
    def translate(
        self,
        lesson_id: int,
        llm: LLMChoice | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        source_timed: bool = False,
    ) -> LessonOut:
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            hebrew = lesson.hebrew_transcript
            source_snapshot = self._source_snapshot(lesson) if source_timed else None
            source_texts = [cue.text for cue in lesson.current_source_cues] if source_timed else []
            source_ids = [cue.id for cue in lesson.current_source_cues] if source_timed else []
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
            translated = (translator.process_source_cues(source_texts, on_progress=on_progress) if source_timed
                          else translator.process_transcript(hebrew, on_progress=on_progress))
        finally:
            if self.settings.ollama_unload_after_translation and isinstance(provider, OllamaProvider):
                provider.unload()

        seg_dir = self._lesson_dir(lesson_id) / "segments"
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            if lesson.hebrew_transcript != hebrew:
                raise WorkflowStateError("The transcript changed during translation; retry with the current text.")
            if source_timed and self._source_snapshot(lesson) != source_snapshot:
                raise WorkflowStateError("Source timing changed during translation; retry.")
            lesson.segments.clear()  # cascades; practice history belongs to the old segments
            self._invalidate_master(lesson)
            lesson.translation_source_text = hebrew
            for index, t in enumerate(translated):
                lesson.segments.append(Segment(
                    position=t.segment_id,
                    hebrew_clean=t.hebrew_clean,
                    english_display=t.english_display,
                    english_tts_phonetic=t.english_tts_phonetic,
                    shadowing_coach_guide=t.shadowing_coach_guide,
                    hebrew_terms=[term.model_dump() for term in t.hebrew_terms],
                    source_cue_id=source_ids[index] if source_timed else None,
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
            seg.audio_origin = "synthetic"
            self._invalidate_master(seg.lesson)
            return SegmentOut.model_validate(seg)

    @exclusive_inference
    def attach_segment_recording(self, segment_id: int, filename: str, data: bytes) -> SegmentOut:
        with session_scope(self.db_url) as s:
            seg = self._segment(s, segment_id)
            self._require_current_translation(seg.lesson)
            text, lesson_id = seg.english_display, seg.lesson_id
        directory = self._lesson_dir(lesson_id) / "segments" / f"recorded_{segment_id}_{uuid.uuid4().hex}"
        directory.mkdir(parents=True, exist_ok=True)
        original = directory / f"original{Path(filename).suffix.lower() or '.bin'}"
        original.write_bytes(data)
        audio = audio_utils.to_wav(original, directory / "english.wav", sample_rate=24000)
        duration = audio_utils.duration_seconds(audio)
        if duration <= 0:
            raise WorkflowStateError("The English recording is empty.")
        import numpy as np
        import soundfile as sf
        samples, _ = sf.read(audio, dtype="float32")
        if not np.any(np.abs(samples) > 1e-5):
            raise WorkflowStateError("The English recording contains no audible signal.")
        with session_scope(self.db_url) as s:
            seg = self._segment(s, segment_id)
            self._require_current_translation(seg.lesson)
            if seg.english_display != text:
                raise WorkflowStateError("The English text changed during upload; check and upload again.")
            seg.audio_path, seg.audio_duration = self._rel(audio), duration
            seg.audio_source_text, seg.audio_origin = text, "recorded"
            self._invalidate_master(seg.lesson)
            return SegmentOut.model_validate(seg)

    def get_dubbing_plan(self, lesson_id: int, max_speed: float = 1.15) -> DubbingPlan:
        if not math.isfinite(max_speed) or not 1 <= max_speed <= 1.25:
            raise WorkflowStateError("Maximum dubbing speed must be between 1.0 and 1.25.")
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            snapshot = self._source_snapshot(lesson)
            self._require_current_translation(lesson)
            cues = lesson.current_source_cues
            source_duration = audio_utils.duration_seconds(self.abs_path(lesson.source_audio_path))
            by_cue = {seg.source_cue_id: seg for seg in lesson.segments}
            if len(lesson.segments) != len(cues) or set(by_cue) != {cue.id for cue in cues}:
                raise WorkflowStateError("Source-timed translation is required before synchronized dubbing.")
            segments = []
            for cue in cues:
                seg = by_cue[cue.id]
                path = self.abs_path(seg.audio_path)
                status, duration, digest = "ready", None, None
                tempo = 1.0
                if path is None or not path.is_file():
                    status = "missing_audio"
                elif seg.audio_is_stale:
                    status = "stale_audio"
                else:
                    duration, digest = audio_utils.duration_seconds(path), file_digest(path)
                    window = cue.end - cue.start
                    if duration <= 0:
                        status = "empty_audio"
                    elif duration > window:
                        # Small headroom absorbs the atempo filter's rounding;
                        # the renderer still refuses to trim any overrun.
                        required_speed = duration / window
                        tempo = min(max_speed, required_speed * 1.01)
                        if required_speed > max_speed:
                            status = "too_long"
                if cue.end > source_duration:
                    status = "source_window_out_of_range"
                segments.append({"segment_id": seg.id, "source_cue_id": cue.id, "position": cue.position,
                                 "start": cue.start, "end": cue.end, "english_text": seg.english_display,
                                 "audio_path": str(path) if path else None, "audio_sha256": digest,
                                 "audio_origin": seg.audio_origin or "synthetic", "audio_duration": duration,
                                 "tempo": tempo, "status": status})
            duration = source_duration
            original = self.abs_path(lesson.source_asset["original_path"]) if lesson.source_asset else None
            video = str(original) if original and original.suffix.lower() in {".mp4", ".mov", ".m4v"} else None
            return DubbingPlan(lesson_id=lesson_id, source_snapshot=snapshot, duration_seconds=duration,
                               max_speed=max_speed, ready=all(seg["status"] == "ready" for seg in segments),
                               segments=segments, original_video_path=video)

    @exclusive_inference
    def build_synchronized_audio(self, lesson_id: int, max_speed: float = 1.15) -> SynchronizedOutputs:
        from core.dubbing import render_track

        plan = self.get_dubbing_plan(lesson_id, max_speed)
        if not plan.ready:
            blocked = [(seg["position"], seg["status"]) for seg in plan.segments if seg["status"] != "ready"]
            raise WorkflowStateError(f"Dubbing needs correction before rendering: {blocked}.")
        directory = self._lesson_dir(lesson_id) / "synchronized" / uuid.uuid4().hex
        wav = directory / "source_synced.en.wav"
        try:
            placements = render_track(plan.segments, plan.duration_seconds, wav)
        except ValueError as exc:
            raise WorkflowStateError(str(exc)) from exc
        mp3 = audio_utils.wav_to_mp3(wav, directory / "source_synced.en.mp3")
        captions = write_anchored_srt([(seg["start"], seg["end"], seg["english_text"]) for seg in plan.segments],
                                      directory / "source_synced.en.srt")
        video = None
        if plan.original_video_path:
            video = directory / "source_synced.en.mp4"
            # Copy the original video frames unchanged. Do not use -shortest,
            # which could silently discard a silent video tail.
            audio_utils._run([audio_utils._ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                              "-i", plan.original_video_path, "-i", str(wav), "-map", "0:v:0", "-map", "1:a:0",
                              "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(video)])
        if self.get_dubbing_plan(lesson_id, max_speed) != plan:
            raise WorkflowStateError("Source or English audio changed during synchronized rendering; rebuild it.")
        manifest = directory / "manifest.json"
        private_json(manifest, {"schemaVersion": 1, "lessonId": lesson_id, "timeline": "original_source",
                               "sourceSnapshot": plan.source_snapshot, "maxSpeed": max_speed,
                               "durationSeconds": plan.duration_seconds, "placements": placements,
                               "inputSegments": plan.segments, "publicationReady": False,
                               "qualityReview": "required", "fullRecordingForcedAlignment": "not_implemented",
                               "files": [{"path": str(path), "sha256": file_digest(path)} for path in (wav, mp3, captions, video) if path]})
        return SynchronizedOutputs(wav_path=str(wav), mp3_path=str(mp3), english_srt_path=str(captions),
                                   manifest_path=str(manifest), duration_seconds=plan.duration_seconds,
                                   segment_count=len(plan.segments), video_path=str(video) if video else None)

    def segment_audio(self, segment_id: int, rate: float = 1.0) -> Path:
        with session_scope(self.db_url) as s:
            path = self.abs_path(self._segment(s, segment_id).audio_path)
        if path is None or not path.exists():
            raise FileNotFoundError("No audio has been generated for this segment yet.")
        return audio_utils.tempo_variant(path, rate)

    def _video_inputs(self, lesson_id: int) -> dict:
        from core.video_renderer import find_font, RENDERER_VERSION

        font = find_font(self.settings.video_font_path)
        with session_scope(self.db_url) as s:
            lesson = self._lesson(s, lesson_id)
            paths, texts, signature = self._export_inputs(lesson)
            return {"lessonId": lesson_id, "title": lesson.title, "inputSignature": signature,
                    "rendererVersion": RENDERER_VERSION, "fontPath": str(font), "fontSha256": file_digest(font),
                    "pauseMs": self.settings.segment_pause_ms,
                    "segments": [{"id": seg.id, "position": seg.position, "sourceCueId": seg.source_cue_id,
                                  "text": text, "audioPath": str(path.resolve()), "audioSha256": file_digest(path),
                                  "audioOrigin": seg.audio_origin or "synthetic"}
                                 for seg, path, text in zip(lesson.segments, paths, texts)]}

    @exclusive_inference
    def build_generated_video(self, lesson_id: int) -> GeneratedVideoOutputs:
        """Generate an original text-slide video; no filmed source/video project required."""
        from core.video_renderer import render

        inputs = self._video_inputs(lesson_id)
        directory = self._lesson_dir(lesson_id) / "generated_video" / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        audio = directory / "narration.en.wav"
        spans = audio_utils.merge_segments([Path(seg["audioPath"]) for seg in inputs["segments"]], audio, inputs["pauseMs"])
        audio.chmod(0o600)
        scenes = [{"start": a, "end": b, "text": seg["text"], "paragraph": seg["id"],
                   "sourceCueId": seg["sourceCueId"], "audioOrigin": seg["audioOrigin"]}
                  for (a, b), seg in zip(spans, inputs["segments"])]
        captions = write_anchored_srt([(scene["start"], scene["end"], scene["text"]) for scene in scenes],
                                      directory / "narration.en.srt")
        try:
            storyboard = render(inputs["title"], scenes, audio, directory, Path(inputs["fontPath"]))
        except ValueError as exc:
            raise WorkflowStateError(str(exc)) from exc
        poster = directory / "poster.png"
        shutil.copyfile(directory / storyboard["scenes"][0]["image"], poster)
        poster.chmod(0o600)
        storyboard_path = directory / "storyboard.json"
        private_json(storyboard_path, {**storyboard, "timeline": "english_narration",
                                       "timingPrecision": "measured_paragraph_audio_not_word_alignment"})
        if self._video_inputs(lesson_id) != inputs:
            raise WorkflowStateError("Lesson/audio/font changed during video generation; rebuild it.")
        manifest = directory / "manifest.json"
        files = [directory / "lesson.en.mp4", audio, captions, poster, storyboard_path]
        files += [directory / scene["image"] for scene in storyboard["scenes"]]
        files.append(directory / "slides.ffconcat")
        private_json(manifest, {"schemaVersion": 1, "producer": "Kolmud Studio", "version": __version__,
                               "lessonId": lesson_id, "kind": "generated_text_slides", "inputs": inputs,
                               "timeline": "english_narration", "durationSeconds": storyboard["durationSeconds"],
                               "sceneCount": len(scenes), "publicationReady": False, "qualityReview": "required",
                               "files": [{"path": str(path.resolve()), "sha256": file_digest(path)} for path in files]})
        return self.get_generated_video(lesson_id, manifest)

    def get_generated_video(self, lesson_id: int, manifest_path: Path) -> GeneratedVideoOutputs:
        directory = manifest_path.expanduser().resolve().parent
        if directory.parent != (self._lesson_dir(lesson_id) / "generated_video").resolve() or manifest_path.name != "manifest.json":
            raise WorkflowStateError("Generated video does not belong to this lesson.")
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            if manifest["lessonId"] != lesson_id or manifest["inputs"] != self._video_inputs(lesson_id):
                raise WorkflowStateError("Video inputs changed; generate the video again.")
            required = {"lesson.en.mp4", "narration.en.wav", "narration.en.srt", "poster.png", "storyboard.json", "slides.ffconcat"}
            found = set()
            for item in manifest["files"]:
                path = Path(item["path"]).resolve()
                if path.parent != directory or file_digest(path) != item["sha256"]:
                    raise WorkflowStateError("Generated video files changed; generate the video again.")
                found.add(path.name)
            if not required <= found:
                raise WorkflowStateError("Generated video is incomplete.")
            if manifest["inputs"] != self._video_inputs(lesson_id):
                raise WorkflowStateError("Video inputs changed during verification.")
            return GeneratedVideoOutputs(video_path=str(directory / "lesson.en.mp4"), audio_path=str(directory / "narration.en.wav"),
                                         english_srt_path=str(directory / "narration.en.srt"), poster_path=str(directory / "poster.png"),
                                         storyboard_path=str(directory / "storyboard.json"), manifest_path=str(directory / "manifest.json"),
                                         duration_seconds=manifest["durationSeconds"], scene_count=manifest["sceneCount"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WorkflowStateError("Generated video is missing or invalid; generate it again.") from exc

    def get_synchronized_audio(self, lesson_id: int, manifest_path: Path) -> SynchronizedOutputs:
        """Reuse a generation only while all source, paragraph and output hashes agree."""
        directory = manifest_path.expanduser().resolve().parent
        expected_root = (self._lesson_dir(lesson_id) / "synchronized").resolve()
        if directory.parent != expected_root or manifest_path.name != "manifest.json":
            raise WorkflowStateError("Synchronized generation does not belong to this lesson.")
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            plan = self.get_dubbing_plan(lesson_id, manifest["maxSpeed"])
            if not plan.ready or manifest["lessonId"] != lesson_id or manifest["sourceSnapshot"] != plan.source_snapshot or manifest["inputSegments"] != plan.segments or manifest["durationSeconds"] != plan.duration_seconds:
                raise WorkflowStateError("Synchronized inputs changed; rebuild the generation.")
            names = {"source_synced.en.wav", "source_synced.en.mp3", "source_synced.en.srt"}
            if plan.original_video_path:
                names.add("source_synced.en.mp4")
            actual = set()
            for item in manifest["files"]:
                path = Path(item["path"]).resolve()
                if path.parent != directory or path.name not in names or file_digest(path) != item["sha256"]:
                    raise WorkflowStateError("Synchronized output files changed; rebuild the generation.")
                actual.add(path.name)
            if actual != names:
                raise WorkflowStateError("Synchronized output generation is incomplete.")
            if self.get_dubbing_plan(lesson_id, manifest["maxSpeed"]) != plan:
                raise WorkflowStateError("Synchronized inputs changed during verification.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WorkflowStateError("Synchronized generation is missing or invalid; rebuild it.") from exc
        return SynchronizedOutputs(wav_path=str(directory / "source_synced.en.wav"),
                                   mp3_path=str(directory / "source_synced.en.mp3"),
                                   english_srt_path=str(directory / "source_synced.en.srt"),
                                   video_path=str(directory / "source_synced.en.mp4") if plan.original_video_path else None,
                                   manifest_path=str(directory / "manifest.json"),
                                   duration_seconds=plan.duration_seconds, segment_count=len(plan.segments))

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
    def save_reference_voice(self, filename: str, data: bytes) -> audio_utils.VoiceCheck:
        """Check and store the cloning reference as 24 kHz mono WAV, outer silence trimmed.

        An unusable recording (silent, < 6 s of speech) is rejected and the current
        reference voice is kept; a usable one with issues is saved with warnings.
        """
        dst = self.settings.reference_voice_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        stem = uuid.uuid4().hex
        upload = dst.with_name(f".upload-{stem}{Path(filename).suffix or '.wav'}")
        converted, trimmed = dst.with_name(f".converted-{stem}.wav"), dst.with_name(f".trimmed-{stem}.wav")
        upload.write_bytes(data)
        try:
            audio_utils.to_wav(upload, converted, sample_rate=24000)
            check = audio_utils.check_and_trim_voice(converted, trimmed)
            if not check.ok:
                raise VoiceRejectedError(check)
            trimmed.replace(dst)
            return check
        finally:
            for tmp in (upload, converted, trimmed):
                tmp.unlink(missing_ok=True)

    def reference_voice_check(self) -> audio_utils.VoiceCheck | None:
        path = self.settings.reference_voice_path
        return audio_utils.check_and_trim_voice(path) if path.is_file() else None

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
