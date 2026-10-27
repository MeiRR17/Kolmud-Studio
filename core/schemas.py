"""Plain DTOs returned by the service layer (shared by the Streamlit UI and the FastAPI server)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SegmentOut(_ORM):
    id: int
    lesson_id: int
    position: int
    hebrew_clean: str
    english_display: str
    english_tts_phonetic: str
    shadowing_coach_guide: str
    hebrew_terms: list[dict[str, str]] = []
    audio_path: str | None
    audio_duration: float | None
    audio_is_stale: bool


class LessonSummary(_ORM):
    id: int
    title: str
    created_at: datetime


class LessonOut(LessonSummary):
    source_audio_path: str | None
    hebrew_transcript: str
    master_audio_path: str | None
    segments: list[SegmentOut]


class AttemptOut(_ORM):
    id: int
    segment_id: int
    created_at: datetime
    audio_path: str
    recognized_text: str
    clarity_score: float
    strict_wer: float
    wpm: float
    report: dict[str, Any]


class MasterOutputs(BaseModel):
    wav_path: str
    mp3_path: str
    srt_path: str
    duration_seconds: float
    segment_count: int


class SegmentUpdate(BaseModel):
    hebrew_clean: str | None = None
    english_display: str | None = None
    english_tts_phonetic: str | None = None
    shadowing_coach_guide: str | None = None
