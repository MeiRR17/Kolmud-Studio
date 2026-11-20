"""Plain DTOs returned by the service layer (shared by the Streamlit UI and the FastAPI server)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    source_cue_id: int | None = None
    audio_origin: str | None = None


class LessonSummary(_ORM):
    id: int
    title: str
    created_at: datetime


class LessonOut(LessonSummary):
    source_audio_path: str | None
    hebrew_transcript: str
    master_audio_path: str | None
    translation_is_stale: bool
    timeline_revision: str | None = None
    source_timeline_is_stale: bool = False
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


class SourceCueOut(_ORM):
    id: int
    lesson_id: int
    revision: str
    position: int
    start: float
    end: float
    raw_text: str
    text: str
    words: list[dict[str, Any]]
    reviewed: bool
    timing_basis: str
    edit_version: int
    edits: list[dict[str, Any]]
    flags: list[str] = Field(default_factory=list)


class SourceCueUpdate(BaseModel):
    expected_version: int = Field(ge=0)
    text: str | None = Field(default=None, min_length=1)
    start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    reviewed: bool | None = None

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("Source text must not be empty.")
        return value


class SourceTimelineOut(BaseModel):
    lesson_id: int
    revision: str | None
    stale: bool
    cues: list[SourceCueOut]
    pending_review: int
    flags: list[str] = Field(default_factory=list)


class SourceCaptionOutputs(BaseModel):
    hebrew_srt_path: str
    english_srt_path: str | None
    manifest_path: str
    timeline_revision: str
    source_cue_count: int
    pending_review: int


class DubbingPlan(BaseModel):
    lesson_id: int
    source_snapshot: str
    duration_seconds: float
    max_speed: float
    ready: bool
    segments: list[dict[str, Any]]
    original_video_path: str | None = None


class SynchronizedOutputs(BaseModel):
    wav_path: str
    mp3_path: str
    english_srt_path: str
    manifest_path: str
    duration_seconds: float
    segment_count: int
    video_path: str | None = None


class GeneratedVideoOutputs(BaseModel):
    video_path: str
    audio_path: str
    english_srt_path: str
    poster_path: str
    storyboard_path: str
    manifest_path: str
    duration_seconds: float
    scene_count: int
