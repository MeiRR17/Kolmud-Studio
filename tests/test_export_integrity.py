import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app, get_service
from core.config import Settings
from core.pipeline import StudioService, WorkflowStateError
from core.schemas import SegmentUpdate


def translated(service, *, long=False):
    lesson = service.create_lesson("Integrity")
    service.set_transcript(lesson.id, "משפט בשיעור על הלכות שבת. " * (100 if long else 1))
    return service.translate(lesson.id)


def test_export_requires_every_paragraph(service):
    lesson = translated(service, long=True)
    assert len(lesson.segments) >= 2
    service.synthesize_segment(lesson.segments[0].id)
    with pytest.raises(WorkflowStateError, match="Missing audio"):
        service.build_master(lesson.id)
    assert service.get_lesson(lesson.id).master_audio_path is None


def test_edits_rebuild_dependent_text_and_invalidate_audio_and_master(service):
    lesson = translated(service)
    seg = service.synthesize_segment(lesson.segments[0].id)
    old = service.build_master(lesson.id)
    assert service.get_master(lesson.id).mp3_path == old.mp3_path
    seg = service.update_segment(seg.id, SegmentUpdate(english_display="A new lesson on Shabbat."))
    assert "Shah-baht" in seg.english_tts_phonetic
    assert "new" in seg.shadowing_coach_guide.lower()
    assert seg.audio_is_stale
    assert service.get_lesson(lesson.id).master_audio_path is None
    with pytest.raises(WorkflowStateError):
        service.get_master(lesson.id)
    with pytest.raises(WorkflowStateError, match="stale audio"):
        service.build_master(lesson.id)
    service.synthesize_segment(seg.id)
    new = service.build_master(lesson.id)
    assert new.wav_path != old.wav_path
    assert "A new lesson" in Path(new.srt_path).read_text()
    assert Path(old.wav_path).is_file()  # prior exports preserved


def test_transcript_changes_block_old_translation(service):
    lesson = translated(service)
    service.synthesize_segment(lesson.segments[0].id)
    service.build_master(lesson.id)
    changed = service.set_transcript(lesson.id, "תמלול מתוקן")
    assert changed.translation_is_stale and changed.master_audio_path is None
    with pytest.raises(WorkflowStateError):
        service.synthesize_segment(lesson.segments[0].id)
    with pytest.raises(WorkflowStateError):
        service.build_master(lesson.id)


def test_tampered_or_missing_exports_are_not_downloadable(service):
    lesson = translated(service)
    service.synthesize_segment(lesson.segments[0].id)
    master = service.build_master(lesson.id)
    Path(master.srt_path).write_text("Tampered subtitles")
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as client:
            assert client.get(f"/lessons/{lesson.id}/master/mp3").status_code == 409
            master = service.build_master(lesson.id)
            Path(master.wav_path).unlink()
            assert client.get(f"/lessons/{lesson.id}/master/mp3").status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_api_recreates_deleted_paragraph_audio(service):
    lesson = translated(service)
    seg = service.synthesize_segment(lesson.segments[0].id)
    service.abs_path(seg.audio_path).unlink()
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as client:
            assert client.post(f"/lessons/{lesson.id}/synthesize").status_code == 200
            assert service.abs_path(service.get_lesson(lesson.id).segments[0].audio_path).is_file()
    finally:
        app.dependency_overrides.clear()


def test_legacy_database_upgrade_preserves_existing_lesson(tmp_path):
    database = tmp_path / "kolmud.db"
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE lessons (id INTEGER PRIMARY KEY, title VARCHAR(300),
            created_at DATETIME, source_audio_path VARCHAR(1000), hebrew_transcript TEXT,
            asr_segments JSON, master_audio_path VARCHAR(1000))""")
        connection.execute("INSERT INTO lessons (id, title, created_at, hebrew_transcript) VALUES (1, 'Existing', '2026-10-21 00:00:00', 'שבת')")
        connection.execute("""CREATE TABLE segments (id INTEGER PRIMARY KEY, lesson_id INTEGER, position INTEGER,
            hebrew_clean TEXT, english_display TEXT, english_tts_phonetic TEXT, shadowing_coach_guide TEXT,
            hebrew_terms JSON, audio_path VARCHAR(1000), audio_duration FLOAT, audio_source_text TEXT)""")
        connection.execute("""INSERT INTO segments (id, lesson_id, position, hebrew_clean, english_display,
            english_tts_phonetic, shadowing_coach_guide, hebrew_terms)
            VALUES (1, 1, 1, 'שבת', 'Shabbat', 'Shabbat', 'Shabbat', '[]')""")
    service = StudioService(Settings(_env_file=None, data_dir=tmp_path))
    assert service.get_lesson(1).title == "Existing"
    assert service.get_lesson(1).segments[0].english_display == "Shabbat"
    assert service.get_lesson(1).translation_is_stale
    with pytest.raises(WorkflowStateError):
        service.synthesize_segment(1)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(lessons)")}
        assert {"translation_source_text", "master_signature"} <= columns


def test_edit_during_synthesis_does_not_publish_mismatched_audio(service, monkeypatch):
    lesson = translated(service)
    seg = lesson.segments[0]
    engine = service.make_tts_engine()

    class EditingEngine:
        def synthesize(self, text, path):
            engine.synthesize(text, path)
            service.update_segment(seg.id, SegmentUpdate(english_display="Changed during inference."))

    monkeypatch.setattr(service, "make_tts_engine", lambda choice=None: EditingEngine())
    with pytest.raises(WorkflowStateError, match="changed during synthesis"):
        service.synthesize_segment(seg.id)
    assert service.get_lesson(lesson.id).segments[0].audio_path is None
