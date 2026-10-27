"""Local SQLite persistence: lessons, translated segments and shadowing practice history."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Lesson(Base):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    source_audio_path: Mapped[str | None] = mapped_column(String(1000))
    hebrew_transcript: Mapped[str] = mapped_column(Text, default="")
    asr_segments: Mapped[list | None] = mapped_column(JSON)
    master_audio_path: Mapped[str | None] = mapped_column(String(1000))

    segments: Mapped[list[Segment]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan", order_by="Segment.position"
    )


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    hebrew_clean: Mapped[str] = mapped_column(Text, default="")
    english_display: Mapped[str] = mapped_column(Text, default="")
    english_tts_phonetic: Mapped[str] = mapped_column(Text, default="")
    shadowing_coach_guide: Mapped[str] = mapped_column(Text, default="")
    # Hebrew/Aramaic terms the LLM found in this paragraph: [{"term": ..., "phonetic": ...}]
    hebrew_terms: Mapped[list] = mapped_column(JSON, default=list)
    audio_path: Mapped[str | None] = mapped_column(String(1000))
    audio_duration: Mapped[float | None] = mapped_column(Float)
    # The TTS text the current audio was generated from, to flag audio that's out of date.
    audio_source_text: Mapped[str | None] = mapped_column(Text)

    lesson: Mapped[Lesson] = relationship(back_populates="segments")
    attempts: Mapped[list[PracticeAttempt]] = relationship(
        back_populates="segment", cascade="all, delete-orphan", order_by="PracticeAttempt.created_at"
    )

    @property
    def audio_is_stale(self) -> bool:
        return bool(self.audio_path) and self.audio_source_text != self.english_tts_phonetic


class PracticeAttempt(Base):
    __tablename__ = "practice_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("segments.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    audio_path: Mapped[str] = mapped_column(String(1000))
    recognized_text: Mapped[str] = mapped_column(Text, default="")
    clarity_score: Mapped[float] = mapped_column(Float)
    strict_wer: Mapped[float] = mapped_column(Float)
    wpm: Mapped[float] = mapped_column(Float)
    report: Mapped[dict] = mapped_column(JSON)

    segment: Mapped[Segment] = relationship(back_populates="attempts")


@lru_cache(maxsize=4)
def get_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")  # Streamlit and FastAPI can share the file safely
        cur.close()

    Base.metadata.create_all(engine)
    return engine


@lru_cache(maxsize=4)
def _session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(database_url), expire_on_commit=False)


@contextmanager
def session_scope(database_url: str) -> Iterator[Session]:
    session = _session_factory(database_url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
