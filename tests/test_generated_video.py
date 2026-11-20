import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api.main import app, get_service
from core import audio_utils
from core.jobs import PipelineRunError, PipelineRunner
from core.pipeline import WorkflowStateError
from core.schemas import SegmentUpdate
from core.video_renderer import FPS, find_font, render
from test_dvir_contract import parse_run_errors
from test_dubbing import english_wav
from test_jobs import voice


def lesson_with_audio(service):
    lesson = service.create_lesson("שיעור לדוגמה — הלכות שבת")
    service.set_transcript(lesson.id, "הלכות שבת. מבררים את פרטי הדין.")
    lesson = service.translate(lesson.id)
    for segment in lesson.segments:
        service.attach_segment_recording(segment.id, "take.wav", english_wav(.6))
    return service.get_lesson(lesson.id)


def test_real_mp4_from_text_and_narration_without_source_video(service):
    lesson = lesson_with_audio(service)
    assert lesson.source_audio_path is None
    outputs = service.build_generated_video(lesson.id)
    assert outputs.duration_seconds == pytest.approx(.6)
    assert outputs.scene_count == 1
    storyboard = json.loads(Path(outputs.storyboard_path).read_text())
    assert storyboard["timeline"] == "english_narration"
    assert storyboard["totalFrames"] == 18
    command = [audio_utils._ffmpeg(), "-v", "error", "-i", outputs.video_path, "-map", "0:v:0", "-f", "framemd5", "-"]
    decoded = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    assert "1920x1080" in decoded
    assert len([line for line in decoded.splitlines() if line and not line.startswith("#")]) == 18
    audio = subprocess.run([audio_utils._ffmpeg(), "-v", "error", "-i", outputs.video_path, "-vn", "-ar", "24000", "-ac", "1", "-f", "f32le", "-"],
                           check=True, capture_output=True).stdout
    samples = np.frombuffer(audio, dtype=np.float32)
    assert 14400 <= len(samples) <= 16448  # AAC padding is allowed; spoken audio is retained
    assert np.max(np.abs(samples[12000:14000])) > .05
    assert service.get_generated_video(lesson.id, Path(outputs.manifest_path)) == outputs
    assert "00:00:00,000 --> 00:00:00,600" in Path(outputs.english_srt_path).read_text()
    assert Path(outputs.video_path).stat().st_mode & 0o077 == 0
    manifest = json.loads(Path(outputs.manifest_path).read_text())
    assert manifest["publicationReady"] is False
    assert manifest["inputs"]["segments"][0]["audioOrigin"] == "recorded"


def test_generation_invalidates_on_text_audio_title_and_output_changes(service):
    lesson = lesson_with_audio(service)
    output = service.build_generated_video(lesson.id)
    manifest = Path(output.manifest_path)
    service.rename_lesson(lesson.id, "A new title")
    with pytest.raises(WorkflowStateError, match="inputs changed"):
        service.get_generated_video(lesson.id, manifest)
    service.rename_lesson(lesson.id, lesson.title)
    Path(output.poster_path).write_bytes(b"corrupted image")
    with pytest.raises(WorkflowStateError, match="files changed"):
        service.get_generated_video(lesson.id, manifest)
    fresh = service.build_generated_video(lesson.id)
    service.update_segment(lesson.segments[0].id, SegmentUpdate(english_display="An edited English script."))
    with pytest.raises(WorkflowStateError):
        service.get_generated_video(lesson.id, Path(fresh.manifest_path))


def test_concurrent_edit_never_returns_a_current_generation(service, monkeypatch):
    from core import video_renderer
    lesson = lesson_with_audio(service)
    original = video_renderer.render
    def editing_render(*args, **kwargs):
        result = original(*args, **kwargs)
        service.rename_lesson(lesson.id, "Edited concurrently")
        return result
    monkeypatch.setattr(video_renderer, "render", editing_render)
    with pytest.raises(WorkflowStateError, match="changed during video generation"):
        service.build_generated_video(lesson.id)
    assert not list((service._lesson_dir(lesson.id) / "generated_video").glob("*/manifest.json"))


def test_script_job_generates_video_and_resumes_without_asr_or_rerender(service, wav_bytes, tmp_path, monkeypatch):
    voice(service, wav_bytes)
    script = tmp_path / "script.txt"
    script.write_text("שיעור קצר בהלכות שבת.", encoding="utf-8")
    monkeypatch.setattr(service.asr, "transcribe_hebrew_lecture", lambda *a, **k: pytest.fail("ASR invoked for text input"))
    runner = PipelineRunner(service)
    job = runner.run(script, input_kind="text", until="video")
    assert job["status"] == "NEEDS_HUMAN" and job["completedStages"][-1] == "video"
    assert job["inputKind"] == "text" and "transcription" not in job["models"]
    assert service.get_lesson(job["lessonId"]).source_audio_path is None
    assert parse_run_errors(json.loads(Path(job["reviewPath"]).read_text())) == []
    assets = json.loads(Path(job["manifestPath"]).read_text())["assets"]
    assert any(asset["label"] == "Generated English lesson video" for asset in assets)
    monkeypatch.setattr(service, "build_generated_video", lambda *a, **k: pytest.fail("Video rerendered on unchanged resume"))
    script.unlink()  # private imported script survives loss of the selected input file
    assert PipelineRunner(service).run(job_id=job["id"])["generatedVideo"] == job["generatedVideo"]
    narrower = runner.run(job_id=job["id"], until="translation")
    assert not any(a["label"].startswith("Generated video") for a in json.loads(Path(narrower["manifestPath"]).read_text())["assets"])


def test_recorded_video_job_never_requires_a_filmed_source_or_tts(service, tmp_path, monkeypatch):
    source = tmp_path / "script.txt"
    source.write_text("מלאכת בורר בשבת.")
    runner = PipelineRunner(service)
    job = runner.run(source, input_kind="text", until="translation")
    monkeypatch.setattr(service, "make_tts_engine", lambda *a, **k: pytest.fail("TTS invoked"))
    for segment in service.get_lesson(job["lessonId"]).segments:
        service.attach_segment_recording(segment.id, "take.wav", english_wav(.4))
    assert runner.run(job_id=job["id"], until="video", voice_mode="recorded")["status"] == "NEEDS_HUMAN"


def test_text_snapshot_tampering_and_impossible_source_timing(service, tmp_path):
    source = tmp_path / "script.txt"
    source.write_text("הלכות שבת.")
    runner = PipelineRunner(service)
    with pytest.raises(WorkflowStateError, match="no source recording timestamps"):
        runner.run(source, input_kind="text", until="translation", source_timed=True)
    job = runner.run(source, input_kind="text")
    Path(job["attachedTextPath"]).write_text("changed")
    with pytest.raises(PipelineRunError, match="Attached source changed"):
        runner.run(job_id=job["id"])


def test_generated_video_api_and_missing_audio_guard(service):
    lesson = service.create_lesson("No audio yet")
    service.set_transcript(lesson.id, "הלכות שבת.")
    lesson = service.translate(lesson.id)
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as client:
            assert client.post(f"/lessons/{lesson.id}/generated-video").status_code == 409
            service.attach_segment_recording(lesson.segments[0].id, "take.wav", english_wav(.2))
            result = client.post(f"/lessons/{lesson.id}/generated-video")
            assert result.status_code == 200
            assert Path(result.json()["video_path"]).is_file()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("scenes", [[], [{"start": .3, "end": .2, "text": "bad"}],
                                    [{"start": 0, "end": 3, "text": "beyond audio"}],
                                    [{"start": 0, "end": .001, "text": "too short"}]])
def test_renderer_refuses_invalid_schedules(tmp_path, scenes):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(english_wav(.5))
    with pytest.raises(ValueError):
        render("Title", scenes, audio, tmp_path / "out", find_font())


def test_overflow_is_rejected_without_losing_words_or_starting_ffmpeg(tmp_path, monkeypatch):
    from core import video_renderer
    audio = tmp_path / "audio.wav"
    audio.write_bytes(english_wav(.3))
    monkeypatch.setattr(video_renderer.subprocess, "run", lambda *a, **k: pytest.fail("Render started before layout validation"))
    with pytest.raises(ValueError, match="No text was truncated"):
        render("Title", [{"start": 0, "end": .3, "text": "A long paragraph. " * 1000}], audio, tmp_path / "out", find_font())
