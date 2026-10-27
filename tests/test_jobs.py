from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from core.jobs import PipelineRunError, PipelineRunner, loopback_url
from core.pipeline import StudioService, WorkflowStateError, file_digest
from core.runtime import PipelineBusyError, file_lock


def recording(tmp_path, wav_bytes):
    path = tmp_path / "lesson.wav"
    path.write_bytes(wav_bytes)
    return path


def voice(service, wav_bytes):
    service.settings.xtts_accept_cpml = True
    service.settings.reference_voice_path.parent.mkdir(parents=True, exist_ok=True)
    service.settings.reference_voice_path.write_bytes(wav_bytes)


def test_transcript_only_handoff_and_resume(service, wav_bytes, tmp_path, monkeypatch):
    runner = PipelineRunner(service)
    job = runner.run(recording(tmp_path, wav_bytes), goal_ids=["learning-goal"])
    assert job["status"] == "NEEDS_HUMAN"
    assert job["completedStages"] == ["source", "transcript"]
    assert not service.fake_provider.calls
    report = json.loads(Path(job["reviewPath"]).read_text())
    assert report["goalIds"] == ["learning-goal"]
    assert report["checks"][-1]["status"] == "skipped"
    assert stat.S_IMODE(Path(job["reviewPath"]).stat().st_mode) == 0o600
    monkeypatch.setattr(service.asr, "transcribe_hebrew_lecture", lambda *a, **k: pytest.fail("Transcription repeated"))
    resumed = PipelineRunner(service).run(job_id=job["id"], until="translation")
    assert resumed["lessonId"] == job["lessonId"]
    assert resumed["completedStages"][-1] == "translation"
    assert len(service.fake_provider.calls) == 1
    runner.run(job_id=job["id"])
    assert len(service.fake_provider.calls) == 1


def test_failed_audio_resumes_in_new_process_without_repeating_completed_paragraphs(service, wav_bytes, tmp_path, monkeypatch):
    voice(service, wav_bytes)
    monkeypatch.setattr(service.asr, "transcribe_hebrew_lecture", lambda *a, **k: {
        "full_text": "משפט בשיעור על הלכות שבת. " * 100, "segments": [],
    })
    original_engine = service.make_tts_engine()
    calls = []

    def flaky(text, path):
        calls.append(path)
        if len(calls) == 2:
            raise RuntimeError("Simulated engine interruption")
        return original_engine.synthesize(text, path)

    class Flaky:
        synthesize = staticmethod(flaky)

    monkeypatch.setattr(service, "make_tts_engine", lambda choice=None: Flaky())
    with pytest.raises(PipelineRunError) as failure:
        PipelineRunner(service).run(recording(tmp_path, wav_bytes), until="master")
    job = failure.value.job
    assert job["status"] == "FAILED" and job["stage"] == "audio"
    assert json.loads(Path(job["reviewPath"]).read_text())["checks"][0]["status"] == "fail"
    first = service.get_lesson(job["lessonId"]).segments[0]
    first_hash = file_digest(service.abs_path(first.audio_path))

    # New service/runner objects simulate restart; no in-memory progress is reused.
    restarted = StudioService(service.settings)
    monkeypatch.setattr(restarted.asr, "transcribe_hebrew_lecture", lambda *a, **k: pytest.fail("Repeated ASR"))
    monkeypatch.setattr(restarted, "make_llm_provider", lambda *a: pytest.fail("Repeated translation"))
    monkeypatch.setattr(restarted, "make_tts_engine", lambda *a: original_engine)
    finished = PipelineRunner(restarted).run(job_id=job["id"])
    assert finished["status"] == "NEEDS_HUMAN"
    assert finished["attempts"] == 2
    assert file_digest(restarted.abs_path(restarted.get_lesson(job["lessonId"]).segments[0].audio_path)) == first_hash
    assert restarted.get_lesson(job["lessonId"]).segments[0].audio_path == first.audio_path
    manifest = json.loads(Path(finished["manifestPath"]).read_text())
    assert {"MP3", "WAV", "SRT"} <= {a["label"] for a in manifest["assets"]}
    assert all(file_digest(Path(a["path"])) == a["sha256"] for a in manifest["assets"])


@pytest.mark.parametrize("provider,url,tts,target", [
    ("openai", "http://localhost:11434", "xtts", "translation"),
    ("anthropic", "http://localhost:11434", "xtts", "translation"),
    ("ollama", "https://example.com", "xtts", "translation"),
    ("ollama", "http://127.0.0.1:11434", "elevenlabs", "master"),
])
def test_metered_or_remote_providers_fail_before_models_or_private_input_attach(service, wav_bytes, tmp_path, provider, url, tts, target):
    service.settings.llm_provider = provider
    service.settings.ollama_base_url = url
    service.settings.tts_engine = tts
    with pytest.raises(WorkflowStateError):
        PipelineRunner(service).run(recording(tmp_path, wav_bytes), until=target)
    assert service.list_lessons() == []
    assert not service.fake_provider.calls
    assert PipelineRunner(service).list_jobs() == []


@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434", True), ("http://127.0.0.1:11434", True), ("http://[::1]:11434", True),
    ("http://localhost.example.org", False), ("file:///tmp/model", False), ("http://user:secret@localhost", False),
])
def test_loopback_policy(url, expected):
    assert loopback_url(url) == expected


def test_changed_source_fails_closed_on_resume(service, wav_bytes, tmp_path):
    runner = PipelineRunner(service)
    job = runner.run(recording(tmp_path, wav_bytes))
    lesson = service.get_lesson(job["lessonId"])
    service.abs_path(lesson.source_audio_path).write_bytes(b"Changed recording")
    with pytest.raises(PipelineRunError):
        runner.run(job_id=job["id"], until="translation")
    assert not service.fake_provider.calls


def test_changed_voice_does_not_mix_profiles_on_resume(service, wav_bytes, tmp_path):
    voice(service, wav_bytes)
    runner = PipelineRunner(service)
    job = runner.run(recording(tmp_path, wav_bytes), until="audio")
    service.settings.xtts_speed = 1.2
    with pytest.raises(PipelineRunError):
        runner.run(job_id=job["id"], until="master")
    assert service.get_lesson(job["lessonId"]).master_audio_path is None


def test_worker_and_inference_capacity_guards(service, wav_bytes, tmp_path):
    with file_lock(service.settings.data_dir / ".jobs.lock"):
        with pytest.raises(PipelineBusyError):
            PipelineRunner(service).run(recording(tmp_path, wav_bytes))
    lesson = service.create_lesson("Busy")
    service.set_transcript(lesson.id, "שבת")
    with file_lock(service.settings.data_dir / ".inference.lock"):
        with pytest.raises(PipelineBusyError):
            service.translate(lesson.id)
    assert not service.fake_provider.calls


def test_invalid_job_id_cannot_read_arbitrary_file(service):
    with pytest.raises(ValueError):
        PipelineRunner(service).read("../../.env")


def test_cli_dispatch_uses_shared_workflow(service, wav_bytes, tmp_path, monkeypatch, capsys):
    from kolmud.cli import main
    monkeypatch.setattr("core.config.Settings", lambda **kw: service.settings)
    monkeypatch.setattr("core.pipeline.StudioService", lambda settings: service)
    original_asr = service.asr.transcribe_hebrew_lecture
    def noisy_asr(*args, **kwargs):
        print("Model progress chatter")
        return original_asr(*args, **kwargs)
    monkeypatch.setattr(service.asr, "transcribe_hebrew_lecture", noisy_asr)
    assert main(["run", str(recording(tmp_path, wav_bytes))]) == 0
    streams = capsys.readouterr()
    job = json.loads(streams.out)
    assert "Model progress chatter" in streams.err and job["id"] in streams.err
    assert job["status"] == "NEEDS_HUMAN" and Path(job["reviewPath"]).exists()
    assert main(["status", job["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["lessonId"] == job["lessonId"]


def test_raw_engine_errors_do_not_leak_into_checkpoints_or_review_files(service, wav_bytes, tmp_path, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("Model response with secret=fake-sensitive-value")
    monkeypatch.setattr(service.asr, "transcribe_hebrew_lecture", crash)
    runner = PipelineRunner(service)
    with pytest.raises(PipelineRunError) as failure:
        runner.run(recording(tmp_path, wav_bytes))
    job = failure.value.job
    assert job["errorType"] == "RuntimeError"
    assert "fake-sensitive-value" not in json.dumps(job)
    assert "fake-sensitive-value" not in Path(job["reviewPath"]).read_text()


def test_synthesis_requires_existing_voice_and_explicit_license(service, wav_bytes, tmp_path):
    source = recording(tmp_path, wav_bytes)
    with pytest.raises(WorkflowStateError, match="license"):
        PipelineRunner(service).run(source, until="master")
    service.settings.xtts_accept_cpml = True
    with pytest.raises(WorkflowStateError, match="reference voice"):
        PipelineRunner(service).run(source, until="master")
    assert service.list_lessons() == []
