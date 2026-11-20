"""Bounded tempo fitting and sample-accurate placement on a source timeline."""

from pathlib import Path

import numpy as np
import soundfile as sf

from core.audio_utils import _ffmpeg, _run

SAMPLE_RATE = 24000


def render_track(segments: list[dict], duration: float, destination: Path) -> list[dict]:
    """Keep every spoken sample; pad gaps, fail if tempo fitting still overruns.

    No speech is cut to force a fit. Original source audio is never overwritten.
    This places cue audio; it is not word-level alignment or lip synchronization.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    total_frames = round(duration * SAMPLE_RATE)
    details = []
    with sf.SoundFile(destination, "w", samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16") as track:
        position = 0

        def silence(frames: int) -> None:
            while frames > 0:
                count = min(frames, SAMPLE_RATE * 5)
                track.write(np.zeros(count, dtype=np.float32))
                frames -= count

        for segment in segments:
            start, end = round(segment["start"] * SAMPLE_RATE), round(segment["end"] * SAMPLE_RATE)
            if start < position or end > total_frames:
                raise ValueError("Dubbing cues overlap or exceed the source duration.")
            silence(start - position)
            fitted = destination.parent / f"fitted_{segment['segment_id']}.wav"
            _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", segment["audio_path"],
                  "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                  "-af", f"atempo={segment['tempo']:.8f}", "-c:a", "pcm_s16le", str(fitted)])
            info = sf.info(fitted)
            if info.frames > end - start:
                raise ValueError(f"Paragraph {segment['position']} still exceeds its source window after tempo fitting; re-record or shorten its translation.")
            if info.frames <= 0:
                raise ValueError("An English audio segment is empty.")
            with sf.SoundFile(fitted) as audio:
                for block in audio.blocks(blocksize=SAMPLE_RATE * 5, dtype="float32"):
                    track.write(block)
            silence(end - start - info.frames)
            position = end
            details.append({"segmentId": segment["segment_id"], "sourceCueId": segment["source_cue_id"],
                            "startFrame": start, "endFrame": end, "speechFrames": info.frames,
                            "paddingFrames": end - start - info.frames, "tempo": segment["tempo"],
                            "audioOrigin": segment["audio_origin"]})
        silence(total_frames - position)
    return details
