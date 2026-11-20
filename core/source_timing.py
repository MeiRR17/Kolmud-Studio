"""Source timeline validation and captions anchored to actual ASR segment times.

ASR timestamps are not forced-alignment evidence. Text corrections never acquire
new word timestamps by interpolation; flags make that limitation explicit.
"""

import math
import re
import textwrap
from pathlib import Path

from core.audio_utils import _srt_time
from core.artifacts import private_text


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def validate_interval(start: float, end: float, duration: float) -> None:
    if not all(math.isfinite(v) for v in (start, end, duration)):
        raise ValueError("Timing values must be finite.")
    if start < 0 or end <= start or end > duration + 0.05:
        raise ValueError("Source cue must lie inside the source recording.")
    if round(end * 1000) <= round(start * 1000):
        raise ValueError("Caption must span at least one rendered millisecond.")


def source_records(segments: list[dict], duration: float) -> list[dict]:
    records = []
    for segment in segments:
        text = normalized(str(segment.get("text", "")))
        if not text:
            continue
        start, end = float(segment["start"]), float(segment["end"])
        validate_interval(start, end, duration)
        words = []
        for word in segment.get("words", []):
            word_start, word_end = float(word["start"]), float(word["end"])
            if not math.isfinite(word_start) or not math.isfinite(word_end) or not start <= word_start <= word_end <= end:
                raise ValueError("ASR word timing falls outside its source cue.")
            probability = word.get("probability")
            if probability is not None and (not math.isfinite(float(probability)) or not 0 <= float(probability) <= 1):
                raise ValueError("ASR word confidence must be between zero and one.")
            words.append({"word": str(word["word"]), "start": word_start, "end": word_end,
                          "probability": probability})
        records.append({"start": start, "end": end, "text": text, "raw_text": text, "words": words})
    return records


def cue_flags(cue) -> list[str]:
    flags = []
    if not cue.reviewed:
        flags.append("unreviewed")
    if not cue.words:
        flags.append("no_word_timestamps")
    elif normalized(cue.text) != normalized(cue.raw_text) or any(w["start"] < cue.start or w["end"] > cue.end for w in cue.words):
        flags.append("word_timestamps_need_realignment")
    if any(w.get("probability") is not None and float(w["probability"]) < 0.5 for w in cue.words):
        flags.append("low_word_confidence")
    if len(normalized(cue.text)) > 168:
        flags.append("long_caption")
    if len(normalized(cue.text)) / max(cue.end - cue.start, 0.001) > 25:
        flags.append("fast_caption")
    return flags


def write_anchored_srt(cues: list[tuple[float, float, str]], destination: Path) -> Path:
    """Wrap lines without inventing sub-cue timestamps from character counts."""
    lines = []
    previous_end = 0.0
    for number, (start, end, text) in enumerate(cues, start=1):
        validate_interval(start, end, end)
        if start < previous_end - 0.0001:
            raise ValueError("Overlapping source cues need timing review before export.")
        content = normalized(text)
        if not content:
            raise ValueError("A source caption has no text.")
        wrapped = textwrap.wrap(content, width=42, break_long_words=False, break_on_hyphens=False)
        lines.extend([str(number), f"{_srt_time(start)} --> {_srt_time(end)}", *wrapped, ""])
        previous_end = end
    destination.parent.mkdir(parents=True, exist_ok=True)
    private_text(destination, "\n".join(lines))
    return destination
