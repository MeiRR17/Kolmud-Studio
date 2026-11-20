from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.main import app, get_service
from core.database import SourceCue, session_scope
from core.jobs import PipelineRunner
from core.pipeline import WorkflowStateError
from core.schemas import SourceCueUpdate
from core.source_timing import source_records, write_anchored_srt


def timed_lesson(service, wav_bytes, monkeypatch):
    cues = [
        {"start": 0.0, "end": 0.4, "text": "הלכות שבת.", "words": [
            {"word": "הלכות", "start": 0.02, "end": 0.15, "probability": 0.95},
            {"word": "שבת.", "start": 0.18, "end": 0.35, "probability": 0.93}]},
        {"start": 0.6, "end": 0.95, "text": "יש לברר.", "words": [
            {"word": "יש", "start": 0.61, "end": 0.7, "probability": 0.9},
            {"word": "לברר.", "start": 0.72, "end": 0.94, "probability": 0.4}]},
    ]
    monkeypatch.setattr(service.asr, "transcribe_hebrew_lecture", lambda *a, **k: {
        "full_text": " ".join(c["text"] for c in cues), "segments": cues,
    })
    lesson = service.create_lesson("Source-time fixture")
    service.attach_source_audio(lesson.id, "a.wav", wav_bytes)
    return service.transcribe(lesson.id)


def test_source_time_translation_and_bilingual_export(service, wav_bytes, monkeypatch):
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    timeline = service.get_source_timeline(lesson.id)
    assert not timeline.stale and timeline.pending_review == 2
    assert timeline.cues[0].words[0]["start"] == 0.02
    assert "low_word_confidence" in timeline.cues[1].flags
    translated = service.translate(lesson.id, source_timed=True)
    assert [s.source_cue_id for s in translated.segments] == [c.id for c in timeline.cues]
    assert [s.hebrew_clean for s in translated.segments] == [c.text for c in timeline.cues]
    assert len(service.fake_provider.calls) == 2
    output = service.build_source_captions(lesson.id)
    he, en = Path(output.hebrew_srt_path), Path(output.english_srt_path)
    for path in (he, en):
        text = path.read_text()
        assert "00:00:00,000 --> 00:00:00,400" in text
        assert "00:00:00,600 --> 00:00:00,950" in text
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    manifest = json.loads(Path(output.manifest_path).read_text())
    assert manifest["timeline"] == "original_source"
    assert manifest["publicationReady"] is False
    assert manifest["cues"][0]["id"] == timeline.cues[0].id
    assert all(s.audio_path is None for s in translated.segments)  # no TTS required


def test_model_segmentation_cannot_invent_source_links(service, wav_bytes, monkeypatch):
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    provider = service.fake_provider
    def completion(system, prompt):
        provider.calls.append(prompt)
        return json.dumps({"segments": [
            {"hebrew_clean": "invented source", "english_display": "Part one.", "hebrew_terms": []},
            {"hebrew_clean": "another invented source", "english_display": "Part two.", "hebrew_terms": []},
        ]})
    monkeypatch.setattr(provider, "complete_json", completion)
    result = service.translate(lesson.id, source_timed=True)
    assert len(result.segments) == 2
    assert result.segments[0].hebrew_clean == "הלכות שבת."
    assert result.segments[0].english_display == "Part one. Part two."


def test_text_edit_preserves_original_evidence_and_invalidates_translation(service, wav_bytes, monkeypatch):
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    service.translate(lesson.id, source_timed=True)
    cue = service.get_source_timeline(lesson.id).cues[0]
    edited = service.update_source_cue(cue.id, SourceCueUpdate(expected_version=cue.edit_version, text="דיני שבת.", reviewed=True))
    assert edited.raw_text == "הלכות שבת."
    assert edited.edits[0]["before"]["text"] == edited.raw_text
    assert edited.reviewed and edited.edit_version == cue.edit_version + 1
    assert "word_timestamps_need_realignment" in edited.flags
    assert service.get_lesson(lesson.id).translation_is_stale
    assert not service.get_source_timeline(lesson.id).stale
    service.build_source_captions(lesson.id, include_english=False)
    with pytest.raises(WorkflowStateError):
        service.build_source_captions(lesson.id)
    with pytest.raises(WorkflowStateError, match="edited elsewhere"):
        service.update_source_cue(cue.id, SourceCueUpdate(expected_version=cue.edit_version, text="Stale write"))


def test_timing_only_edit_reuses_translation_and_keeps_prior_caption_generation(service, wav_bytes, monkeypatch):
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    service.translate(lesson.id, source_timed=True)
    old = service.build_source_captions(lesson.id)
    cue = service.get_source_timeline(lesson.id).cues[0]
    service.update_source_cue(cue.id, SourceCueUpdate(expected_version=cue.edit_version, start=0.01, end=0.39))
    assert not service.get_lesson(lesson.id).translation_is_stale
    current = service.build_source_captions(lesson.id)
    assert "00:00:00,010 --> 00:00:00,390" in Path(current.english_srt_path).read_text()
    assert "00:00:00,000 --> 00:00:00,400" in Path(old.english_srt_path).read_text()
    assert current.english_srt_path != old.english_srt_path


def test_overlaps_fulltext_edits_and_source_drift_fail_closed(service, wav_bytes, monkeypatch):
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    cue = service.get_source_timeline(lesson.id).cues[0]
    with pytest.raises(WorkflowStateError, match="overlaps"):
        service.update_source_cue(cue.id, SourceCueUpdate(expected_version=0, end=0.8))
    assert service.get_source_timeline(lesson.id).cues[0].end == cue.end
    service.set_transcript(lesson.id, "A full-text replacement with no anchors.")
    assert service.get_source_timeline(lesson.id).stale
    with session_scope(service.db_url) as session:
        assert session.get(SourceCue, cue.id).words
    with pytest.raises(WorkflowStateError):
        service.translate(lesson.id, source_timed=True)
    service.set_transcript(lesson.id, lesson.hebrew_transcript)
    source = service.abs_path(lesson.source_audio_path)
    source.write_bytes(b"tampered")
    assert service.get_source_timeline(lesson.id).stale
    with pytest.raises(WorkflowStateError, match="changed"):
        service.build_source_captions(lesson.id, include_english=False)


def test_historical_cues_are_preserved_and_readonly(service, wav_bytes, monkeypatch):
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    old = service.get_source_timeline(lesson.id)
    service.transcribe(lesson.id)
    current = service.get_source_timeline(lesson.id)
    assert current.revision != old.revision
    assert current.cues[0].id != old.cues[0].id
    with session_scope(service.db_url) as session:
        assert len(session.scalars(select(SourceCue).where(SourceCue.lesson_id == lesson.id)).all()) == 4
    with pytest.raises(WorkflowStateError, match="Historical"):
        service.update_source_cue(old.cues[0].id, SourceCueUpdate(expected_version=0, reviewed=True))
    service.translate(lesson.id, source_timed=True)
    service.delete_lesson(lesson.id)
    with session_scope(service.db_url) as session:
        assert session.scalars(select(SourceCue)).all() == []


def test_translation_rejects_timing_edit_during_inference(service, wav_bytes, monkeypatch):
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    cue = service.get_source_timeline(lesson.id).cues[0]
    original = service.fake_provider.complete_json
    def edit_once(system, prompt):
        if not service.fake_provider.calls:
            service.update_source_cue(cue.id, SourceCueUpdate(expected_version=cue.edit_version, start=0.01))
        return original(system, prompt)
    monkeypatch.setattr(service.fake_provider, "complete_json", edit_once)
    with pytest.raises(WorkflowStateError, match="timing changed"):
        service.translate(lesson.id, source_timed=True)
    assert service.get_lesson(lesson.id).segments == []


def test_caption_export_rejects_a_concurrent_edit(service, wav_bytes, monkeypatch):
    from core import pipeline
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    service.translate(lesson.id, source_timed=True)
    cue = service.get_source_timeline(lesson.id).cues[0]
    original = pipeline.write_anchored_srt
    changed = False
    def write_then_edit(cues, destination):
        nonlocal changed
        result = original(cues, destination)
        if not changed:
            changed = True
            service.update_source_cue(cue.id, SourceCueUpdate(expected_version=cue.edit_version, start=0.01))
        return result
    monkeypatch.setattr(pipeline, "write_anchored_srt", write_then_edit)
    with pytest.raises(WorkflowStateError, match="changed during caption"):
        service.build_source_captions(lesson.id)


def test_source_api_and_cli_job_resume(service, wav_bytes, monkeypatch, tmp_path):
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as client:
            timeline = client.get(f"/lessons/{lesson.id}/source-timeline").json()
            cue = timeline["cues"][0]
            assert client.patch(f"/source-cues/{cue['id']}", json={"expected_version": 0, "reviewed": True}).status_code == 200
            assert client.patch(f"/source-cues/{cue['id']}", json={"expected_version": 0, "text": "late"}).status_code == 409
            assert client.get(f"/lessons/{lesson.id}/source-captions/en").status_code == 409
            assert client.post(f"/lessons/{lesson.id}/translate", json={"source_timed": True}).status_code == 200
            assert client.get(f"/lessons/{lesson.id}/source-captions/en").status_code == 200
            assert client.get(f"/lessons/{lesson.id}/source-captions/he").status_code == 200
    finally:
        app.dependency_overrides.clear()
    source = tmp_path / "job.wav"
    source.write_bytes(wav_bytes)
    runner = PipelineRunner(service)
    job = runner.run(source, until="translation", source_timed=True)
    assert job["translationMode"] == "source_timed"
    assert Path(job["sourceCaptions"]["english_srt_path"]).is_file()
    calls = len(service.fake_provider.calls)
    runner.run(job_id=job["id"])
    assert len(service.fake_provider.calls) == calls
    narrower = runner.run(job_id=job["id"], until="transcript")
    assert narrower["status"] == "NEEDS_HUMAN"
    assert "sourceCaptions" not in narrower


@pytest.mark.parametrize("start,end", [(float("nan"), 1), (0, float("inf")), (-1, 1), (0, 2), (0.2, 0.2)])
def test_invalid_source_times_rejected(start, end):
    with pytest.raises(ValueError):
        source_records([{"start": start, "end": end, "text": "מילה"}], duration=1)


def test_writer_does_not_interpolate_times_from_text_length(tmp_path):
    path = write_anchored_srt([(1.25, 3.5, "A long sentence. A much longer second sentence.")], tmp_path / "test.srt")
    text = path.read_text()
    assert text.count("-->") == 1
    assert "00:00:01,250 --> 00:00:03,500" in text
