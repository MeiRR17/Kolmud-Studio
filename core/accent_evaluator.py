"""Scores an English shadowing attempt: intelligibility (WER-based), pace and per-word diff."""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

import jiwer

WordStatus = Literal["correct", "close", "mispronounced", "missed", "extra"]

# A word "heard" with at least this similarity counts as a near miss rather than an error —
# e.g. Whisper writing "machlokes" for "machloket", or "lechatchila" for "l'chatchila".
CLOSE_SIMILARITY = 0.72
# Transliterated Hebrew/Aramaic has many legitimate spellings, so it gets a looser bar.
HEBREW_TERM_SIMILARITY = 0.6
LOW_CONFIDENCE = 0.75
IDEAL_WPM = (110.0, 165.0)


@dataclass
class DiffToken:
    word: str
    status: WordStatus
    heard_as: str = ""


@dataclass
class PracticeReport:
    clarity_score: float          # % of target words recognised (near misses count)
    strict_wer: float             # raw word error rate, 0..1+
    wpm: float
    pacing_feedback: str
    speech_seconds: float
    mispronounced_words: list[tuple[str, str]] = field(default_factory=list)
    low_confidence_words: list[str] = field(default_factory=list)
    visual_diff: list[DiffToken] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _number_to_words(n: int) -> str:
    try:
        from num2words import num2words

        return num2words(n).replace("-", " ").replace(",", "")
    except ImportError:
        if n < 20:
            return _ONES[n]
        if n < 100:
            return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
        return str(n)


def normalize_text(text: str) -> str:
    text = text.lower()
    # Coaching markup, if a guide was passed in. Only IPA parentheticals like "(tə)" are dropped —
    # glosses such as "(nafka minah)" are part of what the speaker reads aloud.
    text = re.sub(r"\([^)]*[əɪʊæɑɔʌθðŋʃʒ][^)]*\)", " ", text)
    text = text.replace("**", " ").replace("[d]", "t")
    text = text.replace("_", " ").replace("’", "'").replace("-", " ").replace("—", " ")
    text = re.sub(r"\d+", lambda m: f" {_number_to_words(int(m.group(0)))} ", text)
    text = text.replace("'", "")  # l'chatchila == lchatchila; don't == dont
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _align_span(target: list[str], heard: list[str]) -> list[tuple[list[str], str]]:
    """Max-similarity alignment of a mismatched span of target vs heard words.

    Moves: one target word -> 0..3 heard words ("fundamental" ~ "fun mental"), two target
    words -> one heard word (linked speech: "borer on" ~ "boron"), or an extra heard word.
    Returns (target_words, heard_text) pairs in order; extra heard words have no target words.
    """
    n, m = len(target), len(heard)
    neg = float("-inf")
    best = [[neg] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    best[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if best[i][j] == neg:
                continue
            moves: list[tuple[int, int, float]] = []
            if j < m:
                moves.append((0, 1, 0.0))
            if i < n:
                moves.append((1, 0, 0.0))
                moves += [(1, k, _similar(target[i], "".join(heard[j:j + k]))) for k in (1, 2, 3) if j + k <= m]
                if i + 1 < n and j < m:
                    moves.append((2, 1, 2 * _similar(target[i] + target[i + 1], heard[j])))
            for di, dj, score in moves:
                if best[i][j] + score > best[i + di][j + dj]:
                    best[i + di][j + dj] = best[i][j] + score
                    back[i + di][j + dj] = (di, dj)
    pairs: list[tuple[list[str], str]] = []
    i, j = n, m
    while i or j:
        di, dj = back[i][j]  # type: ignore[misc]
        i, j = i - di, j - dj
        pairs.append((target[i:i + di], " ".join(heard[j:j + dj])))
    return pairs[::-1]


class AccentAndFluencyEvaluator:
    def __init__(self, is_hebrew_term: Callable[[str], bool] | None = None) -> None:
        self.is_hebrew_term = is_hebrew_term or (lambda _w: False)

    def evaluate_attempt(
        self,
        target_english_text: str,
        whisper_recognized_text: str,
        word_timings: list[dict[str, Any]],
        audio_duration_seconds: float,
    ) -> PracticeReport:
        target = normalize_text(target_english_text).split()
        heard = normalize_text(whisper_recognized_text).split()
        if not target:
            raise ValueError("Target text is empty")

        strict_wer = float(jiwer.wer(" ".join(target), " ".join(heard))) if heard else 1.0

        diff: list[DiffToken] = []
        mispronounced: list[tuple[str, str]] = []
        matcher = difflib.SequenceMatcher(None, target, heard, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                diff.extend(DiffToken(w, "correct") for w in target[i1:i2])
            elif tag == "delete":
                for w in target[i1:i2]:
                    diff.append(DiffToken(w, "missed"))
                    mispronounced.append((w, ""))
            elif tag == "insert":
                diff.extend(DiffToken(w, "extra") for w in heard[j1:j2])
            else:  # replace
                for words, h in _align_span(target[i1:i2], heard[j1:j2]):
                    if not words:
                        diff.append(DiffToken(h, "extra"))
                        continue
                    if not h:
                        for w in words:
                            diff.append(DiffToken(w, "missed"))
                            mispronounced.append((w, ""))
                        continue
                    hebrew = any(self.is_hebrew_term(w) for w in words)
                    threshold = HEBREW_TERM_SIMILARITY if hebrew else CLOSE_SIMILARITY
                    status: WordStatus = (
                        "close" if _similar("".join(words), h.replace(" ", "")) >= threshold else "mispronounced"
                    )
                    for w in words:
                        diff.append(DiffToken(w, status, h))
                        if status == "mispronounced":
                            mispronounced.append((w, h))

        recognised = sum(1 for t in diff if t.status in ("correct", "close"))
        clarity = round(100.0 * recognised / len(target), 1)

        # Pace from the first to the last recognised word, so mic lead-in/trailing silence is ignored.
        if word_timings:
            speech_seconds = max(0.0, float(word_timings[-1]["end"]) - float(word_timings[0]["start"]))
        else:
            speech_seconds = audio_duration_seconds
        speech_seconds = max(speech_seconds, 1.0)
        wpm = round(len(heard) / speech_seconds * 60.0, 1)

        low_conf = [
            str(w["word"]).strip(" .,!?;:") for w in word_timings
            if float(w.get("probability", 1.0)) < LOW_CONFIDENCE
        ]

        return PracticeReport(
            clarity_score=clarity,
            strict_wer=round(strict_wer, 3),
            wpm=wpm,
            pacing_feedback=pacing_feedback(wpm),
            speech_seconds=round(speech_seconds, 2),
            mispronounced_words=mispronounced,
            low_confidence_words=[w for w in low_conf if w],
            visual_diff=diff,
        )


def pacing_feedback(wpm: float) -> str:
    lo, hi = IDEAL_WPM
    if wpm < 105:
        return "איטי מדי (מתחת ל-105 מילים לדקה). נסה לחבר מילים (Linking) ולהימנע מעצירות בין מילה למילה."
    if wpm > 175:
        return "מהיר מדי (מעל 175 מילים לדקה). בשיעור עיוני באנגלית חשוב לתת משקל ואורך למילות התוכן המודגשות."
    if lo <= wpm <= hi:
        return f"קצב אידיאלי להרצאה/שיעור באנגלית ({lo:.0f}–{hi:.0f} מילים לדקה)."
    return "קצב סביר — קרוב לטווח האידיאלי של 110–165 מילים לדקה."
