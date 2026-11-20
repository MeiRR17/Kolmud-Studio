import json
from pathlib import Path

import pytest

from core.jobs import PipelineRunError, PipelineRunner
from core.pipeline import WorkflowStateError
from core.schemas import SegmentUpdate, SourceCueUpdate
from test_dubbing import english_wav
from test_dvir_contract import parse_run_errors
from test_jobs import voice
from test_source_timing import timed_lesson


def translation_job(service, wav_bytes, monkeypatch, tmp_path):
    timed_lesson(service, wav_bytes, monkeypatch)  # install timestamped ASR fixture
    path = tmp_path / "source.wav"
    path.write_bytes(wav_bytes)
    runner = PipelineRunner(service)
    job = runner.run(path, until="translation", source_timed=True)
    return runner, job, service.get_lesson(job["lessonId"])


def test_recorded_job_requires_no_tts_license_voice_or_provider(service, wav_bytes, monkeypatch, tmp_path):
    runner, job, lesson = translation_job(service, wav_bytes, monkeypatch, tmp_path)
    service.settings.tts_engine = "elevenlabs"  # recorded mode must never invoke this cloud provider
    monkeypatch.setattr(service, "make_tts_engine", lambda *a, **k: pytest.fail("TTS was invoked for recorded mode"))
    for seg in lesson.segments:
        service.attach_segment_recording(seg.id, "take.wav", english_wav())
    completed = runner.run(job_id=job["id"], until="synchronized", voice_mode="recorded")
    assert completed["status"] == "NEEDS_HUMAN"
    assert completed["voice"] == {"engine": "human_recordings"}
    assert "master" not in completed["completedStages"]
    assert completed["completedStages"][-1] == "synchronized"
    manifest_path = completed["synchronizedOutputs"]["manifest_path"]
    monkeypatch.setattr(service, "build_synchronized_audio", lambda *a, **k: pytest.fail("Unnecessary rerender"))
    # Persistent generation reuse, including across a new runner instance.
    assert PipelineRunner(service).run(job_id=job["id"])["synchronizedOutputs"]["manifest_path"] == manifest_path
    manifest = json.loads(Path(completed["manifestPath"]).read_text())
    assert parse_run_errors(json.loads(Path(completed["reviewPath"]).read_text())) == []
    labels = {asset["label"] for asset in manifest["assets"]}
    assert "Synchronized English WAV" in labels
    assert "Recorded paragraph 1" in labels
    assert not any(label.startswith("Synthetic paragraph") for label in labels)


def test_missing_recordings_fail_with_resume_instructions_without_cloning(service, wav_bytes, monkeypatch, tmp_path):
    runner, job, lesson = translation_job(service, wav_bytes, monkeypatch, tmp_path)
    monkeypatch.setattr(service, "make_tts_engine", lambda *a, **k: pytest.fail("Unexpected synthesis"))
    with pytest.raises(PipelineRunError) as failure:
        runner.run(job_id=job["id"], until="synchronized", voice_mode="recorded")
    assert failure.value.job["stage"] == "audio"
    assert "kolmud recording" in failure.value.job["errorMessage"]
    for seg in lesson.segments:
        service.attach_segment_recording(seg.id, "take.wav", english_wav())
    assert runner.run(job_id=job["id"])["status"] == "NEEDS_HUMAN"


def test_mixed_job_preserves_recorded_paragraph_and_clones_only_missing(service, wav_bytes, monkeypatch, tmp_path):
    runner, job, lesson = translation_job(service, wav_bytes, monkeypatch, tmp_path)
    voice(service, wav_bytes)
    original = service.attach_segment_recording(lesson.segments[0].id, "take.wav", english_wav())
    calls = []
    def synthesize(text, path):
        calls.append(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(english_wav())
        return path
    monkeypatch.setattr(service.make_tts_engine(), "synthesize", synthesize)
    completed = runner.run(job_id=job["id"], until="synchronized", voice_mode="mixed")
    assert completed["status"] == "NEEDS_HUMAN" and len(calls) == 1
    current = service.get_lesson(lesson.id)
    assert current.segments[0].audio_path == original.audio_path
    assert [seg.audio_origin for seg in current.segments] == ["recorded", "synthetic"]


def test_synchronized_generation_rejects_changed_inputs_and_output_tampering(service, wav_bytes, monkeypatch, tmp_path):
    runner, job, lesson = translation_job(service, wav_bytes, monkeypatch, tmp_path)
    for seg in lesson.segments:
        service.attach_segment_recording(seg.id, "take.wav", english_wav())
    done = runner.run(job_id=job["id"], until="synchronized", voice_mode="recorded")
    manifest = Path(done["synchronizedOutputs"]["manifest_path"])
    outputs = service.get_synchronized_audio(lesson.id, manifest)
    Path(outputs.mp3_path).write_bytes(b"changed")
    with pytest.raises(WorkflowStateError, match="output files changed"):
        service.get_synchronized_audio(lesson.id, manifest)
    rebuilt = runner.run(job_id=job["id"])
    new_manifest = Path(rebuilt["synchronizedOutputs"]["manifest_path"])
    assert new_manifest != manifest
    cue = service.get_source_timeline(lesson.id).cues[0]
    service.update_source_cue(cue.id, SourceCueUpdate(expected_version=cue.edit_version, end=.39))
    with pytest.raises(WorkflowStateError, match="inputs changed"):
        service.get_synchronized_audio(lesson.id, new_manifest)
    # Timing-only edits keep paragraph recordings and rerender the new source windows.
    assert runner.run(job_id=job["id"])["status"] == "NEEDS_HUMAN"
    service.update_segment(lesson.segments[0].id, SegmentUpdate(english_display="Different text"))
    with pytest.raises(PipelineRunError, match="Attach a current English recording"):
        runner.run(job_id=job["id"])


def test_narrower_resume_does_not_handoff_previous_synchronized_generation(service, wav_bytes, monkeypatch, tmp_path):
    runner, job, lesson = translation_job(service, wav_bytes, monkeypatch, tmp_path)
    for seg in lesson.segments:
        service.attach_segment_recording(seg.id, "take.wav", english_wav())
    runner.run(job_id=job["id"], until="synchronized", voice_mode="recorded")
    result = runner.run(job_id=job["id"], until="transcript")
    manifest = json.loads(Path(result["manifestPath"]).read_text())
    assert not any(asset["label"].startswith("Synchronized") for asset in manifest["assets"])
