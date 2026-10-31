"""Local, Metal-accelerated Whisper (mlx-whisper): Hebrew lecture transcription and English practice scoring."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, TypedDict

TORAH_CONTEXT_PROMPT = (
    "שיעור תורה והלכה בעברית. מונחים נפוצים: שבת, הלכה, שולחן ערוך, "
    'רמב"ם, ראשונים, אחרונים, חז"ל, גמרא, משנה, מדאורייתא, מדרבנן, '
    "לכתחילה, בדיעבד, מלאכת מחשבת, מוקצה, פסיק רישיה, סברא, נפקא מינה."
)


class TimedSegment(TypedDict):
    start: float
    end: float
    text: str


class TimedWord(TypedDict):
    word: str
    start: float
    end: float
    probability: float


class HebrewTranscript(TypedDict):
    full_text: str
    segments: list[TimedSegment]


class EnglishPracticeTranscript(TypedDict):
    recognized_text: str
    words: list[TimedWord]


# MLX keeps a single Whisper model resident; serialise calls so the Streamlit app and the
# FastAPI server (or two browser tabs) never swap models underneath each other mid-decode.
_MLX_LOCK = threading.Lock()
_RESOLVED_MODELS: dict[str, str] = {}


def resolve_model_path(repo_or_path: str) -> str:
    """Local folder for a Whisper model, downloading only if it isn't cached yet.

    mlx-whisper would otherwise query the Hugging Face Hub on every call, so a flaky or
    absent network breaks transcription even when the model is already on disk.
    """
    if Path(repo_or_path).exists():
        return repo_or_path
    if repo_or_path not in _RESOLVED_MODELS:
        from huggingface_hub import snapshot_download

        try:
            local = snapshot_download(repo_or_path, local_files_only=True)
        except Exception:  # noqa: BLE001 - not cached (or partially cached): fetch it
            local = snapshot_download(repo_or_path)
        _RESOLVED_MODELS[repo_or_path] = local
    return _RESOLVED_MODELS[repo_or_path]


SAMPLE_RATE = 16000
# Whisper decodes 30 s windows. When a window's last segment ends early, the rest of that
# window is skipped — in the pilot a whole halachic condition vanished at the 30.0 s mark.
# Transcribing pause-aligned chunks shorter than one window avoids those boundary losses.
MAX_CHUNK_SECONDS = 25.0


def speech_chunks(audio: Any, sr: int = SAMPLE_RATE, max_seconds: float = MAX_CHUNK_SECONDS) -> list[tuple[int, int]]:
    """(start, end) sample ranges of at most `max_seconds`, cut in the middle of pauses."""
    import librosa

    n, max_len = len(audio), int(max_seconds * sr)
    if n <= max_len:
        return [(0, n)] if n else []

    def split(lo: int, hi: int, top_dbs: list[int]) -> list[tuple[int, int]]:
        if hi - lo <= max_len:
            return [(lo, hi)]
        if not top_dbs:  # no pause at all: continuous speech, cut hard
            return [(s, min(s + max_len, hi)) for s in range(lo, hi, max_len)]
        intervals = librosa.effects.split(audio[lo:hi], top_db=top_dbs[0], frame_length=1024, hop_length=256)
        cuts = [lo + int(intervals[i][1] + intervals[i + 1][0]) // 2 for i in range(len(intervals) - 1)]
        out: list[tuple[int, int]] = []
        start = lo
        while hi - start > max_len:
            within = [c for c in cuts if start < c <= start + max_len]
            if within:
                out.append((start, within[-1]))
                start = within[-1]
            else:  # no pause this loud within reach: look for quieter pauses in this stretch
                nxt = next((c for c in cuts if c > start), hi)
                out.extend(split(start, nxt, top_dbs[1:]))
                start = nxt
        out.append((start, hi))
        return out

    return [c for c in split(0, n, [40, 30, 20]) if c[1] - c[0] >= int(0.2 * sr)]


class LocalWhisperEngine:
    def __init__(self, hebrew_model_repo: str, english_model_repo: str) -> None:
        self.hebrew_model_repo = hebrew_model_repo
        self.english_model_repo = english_model_repo

    @staticmethod
    def _transcribe(audio_path: Path, **kwargs: Any) -> dict[str, Any]:
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        import mlx_whisper
        from mlx_whisper.audio import load_audio

        kwargs["path_or_hf_repo"] = resolve_model_path(kwargs["path_or_hf_repo"])
        import numpy as np

        audio = np.asarray(load_audio(str(audio_path)), dtype=np.float32)
        segments: list[dict[str, Any]] = []
        with _MLX_LOCK:
            for start, end in speech_chunks(audio):
                offset = start / SAMPLE_RATE
                result = mlx_whisper.transcribe(audio[start:end], verbose=None, **kwargs)
                for seg in result.get("segments", []):
                    seg = dict(seg, start=seg["start"] + offset, end=seg["end"] + offset)
                    seg["words"] = [dict(w, start=w["start"] + offset, end=w["end"] + offset)
                                    for w in seg.get("words", [])]
                    segments.append(seg)
        text = " ".join(seg["text"].strip() for seg in segments if seg.get("text", "").strip())
        return {"text": text, "segments": segments}

    def transcribe_hebrew_lecture(self, audio_path: Path, use_context_prompt: bool = False) -> HebrewTranscript:
        result = self._transcribe(
            audio_path,
            path_or_hf_repo=self.hebrew_model_repo,
            language="he",
            task="transcribe",
            initial_prompt=TORAH_CONTEXT_PROMPT if use_context_prompt else None,
            # Conditioning on previous text makes large-v3 prone to repetition loops on hour-long
            # lectures; chunks are independent anyway.
            condition_on_previous_text=False,
        )
        segments: list[TimedSegment] = [
            {"start": float(seg["start"]), "end": float(seg["end"]), "text": seg["text"].strip()}
            for seg in result.get("segments", [])
            if seg.get("text", "").strip()
        ]
        return {"full_text": join_hebrew_segments(segments), "segments": segments}

    def transcribe_english_practice(self, audio_path: Path) -> EnglishPracticeTranscript:
        result = self._transcribe(
            audio_path,
            path_or_hf_repo=self.english_model_repo,
            language="en",
            task="transcribe",
            word_timestamps=True,
            # No conditioning and greedy decoding: we want what was *heard*, not what the
            # language model would guess the speaker meant.
            condition_on_previous_text=False,
            temperature=0.0,
        )
        words: list[TimedWord] = [
            {
                "word": w["word"].strip(),
                "start": float(w["start"]),
                "end": float(w["end"]),
                "probability": round(float(w.get("probability", 1.0)), 3),
            }
            for seg in result.get("segments", [])
            for w in seg.get("words", [])
            if w.get("word", "").strip()
        ]
        return {"recognized_text": result.get("text", "").strip(), "words": words}


def join_hebrew_segments(segments: list[TimedSegment], paragraph_gap_seconds: float = 1.5) -> str:
    """Join Whisper segments into text, starting a new paragraph after a long pause."""
    parts: list[str] = []
    prev_end: float | None = None
    for seg in segments:
        if prev_end is not None:
            parts.append("\n\n" if seg["start"] - prev_end >= paragraph_gap_seconds else " ")
        parts.append(seg["text"])
        prev_end = seg["end"]
    return "".join(parts).strip()
