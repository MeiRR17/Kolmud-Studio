"""ffmpeg-backed audio helpers: normalisation, duration, tempo variants, merging and subtitles."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import soundfile as sf


def non_silent_intervals(audio: Any, top_db: float, frame: int = 1024, hop: int = 256) -> list[tuple[int, int]]:
    """Sample ranges louder than `top_db` below the peak frame energy (like librosa.effects.split)."""
    import numpy as np

    if len(audio) < frame:
        return [(0, len(audio))] if len(audio) else []
    frames = np.lib.stride_tricks.sliding_window_view(audio, frame)[::hop]
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-10) / max(float(rms.max()), 1e-10))
    loud = np.concatenate([[False], db > -top_db, [False]])
    edges = np.flatnonzero(np.diff(loud.astype(np.int8)))
    return [(int(a) * hop, min(int(b) * hop + frame, len(audio))) for a, b in zip(edges[::2], edges[1::2])]


class AudioToolError(RuntimeError):
    pass


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise AudioToolError("ffmpeg was not found on PATH. Install it with: brew install ffmpeg")
    return binary


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AudioToolError(f"ffmpeg failed: {proc.stderr.strip()[-800:]}")


def to_wav(src: Path, dst: Path, sample_rate: int = 16000, mono: bool = True) -> Path:
    """Decode any audio/video container ffmpeg understands into PCM WAV."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn"]
    if mono:
        args += ["-ac", "1"]
    args += ["-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)]
    _run(args)
    return dst


def duration_seconds(path: Path) -> float:
    info = sf.info(str(path))
    return float(info.frames) / float(info.samplerate) if info.samplerate else 0.0


def tempo_variant(src: Path, rate: float) -> Path:
    """Pitch-preserving slow-down/speed-up (ffmpeg atempo), cached next to the source file."""
    if abs(rate - 1.0) < 1e-3:
        return src
    if not 0.5 <= rate <= 2.0:
        raise ValueError("Playback rate must be between 0.5 and 2.0")
    dst = src.with_name(f"{src.stem}@{rate:.2f}x.wav")
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
          "-filter:a", f"atempo={rate:.3f}", str(dst)])
    return dst


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


def merge_segments(paths: list[Path], dst_wav: Path, pause_ms: int = 600) -> list[tuple[float, float]]:
    """Concatenate segment WAVs with breathing pauses. Returns the (start, end) of each input."""
    from pydub import AudioSegment

    combined = AudioSegment.empty()
    silence = AudioSegment.silent(duration=pause_ms)
    spans: list[tuple[float, float]] = []
    for idx, path in enumerate(paths):
        seg = AudioSegment.from_file(str(path))
        start = len(combined) / 1000.0
        combined += seg
        spans.append((start, len(combined) / 1000.0))
        if idx < len(paths) - 1:
            combined += silence
    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    combined.export(str(dst_wav), format="wav")
    return spans


def wav_to_mp3(src: Path, dst: Path, bitrate: str = "160k") -> Path:
    _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
          "-codec:a", "libmp3lame", "-b:a", bitrate, str(dst)])
    return dst


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[SubtitleCue], dst: Path, max_chars: int = 84) -> Path:
    """Write SRT, splitting long paragraphs into sentence-sized cues timed by character share."""
    import re

    lines: list[str] = []
    index = 1
    for cue in cues:
        sentences = [s for s in re.split(r"(?<=[.!?;:])\s+", cue.text.strip()) if s]
        chunks: list[str] = []
        for sentence in sentences:
            words, current = sentence.split(), ""
            for word in words:
                if current and len(current) + 1 + len(word) > max_chars:
                    chunks.append(current)
                    current = word
                else:
                    current = f"{current} {word}".strip()
            if current:
                chunks.append(current)
        total_chars = sum(len(c) for c in chunks) or 1
        t = cue.start
        for chunk in chunks:
            span = (cue.end - cue.start) * len(chunk) / total_chars
            lines += [str(index), f"{_srt_time(t)} --> {_srt_time(t + span)}", chunk, ""]
            index += 1
            t += span
    dst.write_text("\n".join(lines), encoding="utf-8")
    return dst


# ---------------------------------------------------------------------- reference voice check
@dataclass
class VoiceCheck:
    """Quality report for a voice-cloning reference recording."""

    duration_seconds: float
    speech_seconds: float
    loudness_dbfs: float
    clipped_percent: float
    errors: list[str]    # codes: the recording can't be used
    warnings: list[str]  # codes: usable, but cloning quality may suffer

    @property
    def ok(self) -> bool:
        return not self.errors


VOICE_CHECK_MESSAGES = {
    "silent": "The recording is silent.",
    "too_short": "Less than 6 seconds of speech; XTTS needs at least 6 (15-30 recommended).",
    "short": "Under 12 seconds of speech; 15-30 seconds clones the voice more faithfully.",
    "long": "Over 60 seconds; only the first 60 seconds are used.",
    "pauses": "More than half of the recording is silence; speak continuously.",
    "quiet": "Very quiet recording; move closer to the microphone.",
    "clipping": "Distorted (clipped) audio; lower the input volume or move back a little.",
}


def check_and_trim_voice(src: Path, dst: Path | None = None, pad_seconds: float = 0.25) -> VoiceCheck:
    """Measure a reference recording; if `dst` is given, write it there with outer silence trimmed."""
    import numpy as np

    audio, sr = sf.read(str(src), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    duration = len(audio) / sr if sr else 0.0
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak < 1e-4:
        return VoiceCheck(round(duration, 2), 0.0, -120.0, 0.0, ["silent"], [])
    intervals = non_silent_intervals(audio, top_db=35)
    speech = sum(b - a for a, b in intervals) / sr
    voiced = np.concatenate([audio[a:b] for a, b in intervals])
    loudness = float(20 * np.log10(max(float(np.sqrt(np.mean(np.square(voiced, dtype=np.float64)))), 1e-10)))
    clipped = 100.0 * float(np.mean(np.abs(audio) >= 0.99))

    errors, warnings = [], []
    if speech < 6:
        errors.append("too_short")
    elif speech < 12:
        warnings.append("short")
    if duration > 60:
        warnings.append("long")
    if speech < 0.5 * duration:
        warnings.append("pauses")
    if loudness < -35:
        warnings.append("quiet")
    if clipped > 0.1:
        warnings.append("clipping")

    if dst is not None:
        pad = int(pad_seconds * sr)
        start, end = max(intervals[0][0] - pad, 0), min(intervals[-1][1] + pad, len(audio))
        dst.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(dst), audio[start:end], sr, subtype="PCM_16")
    return VoiceCheck(round(duration, 2), round(speech, 2), round(loudness, 1), round(clipped, 3), errors, warnings)
