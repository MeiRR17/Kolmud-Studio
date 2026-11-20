import io
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from api.main import app, get_service
from core.pipeline import WorkflowStateError
from core.schemas import SegmentUpdate
from test_source_timing import timed_lesson


def english_wav(seconds=0.2, silent=False):
    buffer = io.BytesIO()
    time = np.arange(round(seconds * 24000)) / 24000
    samples = np.zeros(len(time)) if silent else 0.1 * np.sin(2 * np.pi * 440 * time)
    sf.write(buffer, samples, 24000, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def translated(service, wav_bytes, monkeypatch):
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    return service.translate(lesson.id, source_timed=True)


def attach_all(service, lesson, duration=0.2):
    for seg in lesson.segments:
        service.attach_segment_recording(seg.id, "english.wav", english_wav(duration))


def test_recorded_audio_is_preserved_and_invalidated_by_display_text(service, wav_bytes, monkeypatch):
    lesson = translated(service, wav_bytes, monkeypatch)
    seg = service.attach_segment_recording(lesson.segments[0].id, "english.wav", english_wav())
    assert seg.audio_origin == "recorded" and not seg.audio_is_stale
    old_path = service.abs_path(seg.audio_path)
    assert (old_path.parent / "original.wav").is_file()
    seg = service.update_segment(seg.id, SegmentUpdate(english_tts_phonetic="Different pronunciation only"))
    assert not seg.audio_is_stale
    seg = service.update_segment(seg.id, SegmentUpdate(english_display="A different English sentence."))
    assert seg.audio_is_stale and old_path.is_file()
    seg = service.synthesize_segment(seg.id)
    assert seg.audio_origin == "synthetic" and not seg.audio_is_stale
    assert old_path.is_file()


def test_empty_signal_recording_does_not_replace_existing_audio(service, wav_bytes, monkeypatch):
    lesson = translated(service, wav_bytes, monkeypatch)
    original = service.attach_segment_recording(lesson.segments[0].id, "english.wav", english_wav())
    with pytest.raises(WorkflowStateError, match="no audible"):
        service.attach_segment_recording(original.id, "silent.wav", english_wav(silent=True))
    assert service.get_lesson(lesson.id).segments[0].audio_path == original.audio_path


def test_sample_accurate_source_track_with_gaps_and_two_voice_origins(service, wav_bytes, monkeypatch):
    lesson = translated(service, wav_bytes, monkeypatch)
    service.attach_segment_recording(lesson.segments[0].id, "english.wav", english_wav())
    engine = service.make_tts_engine()
    def short_synthesis(text, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(english_wav())
        return output
    monkeypatch.setattr(engine, "synthesize", short_synthesis)
    service.synthesize_segment(lesson.segments[1].id)
    plan = service.get_dubbing_plan(lesson.id)
    assert plan.ready
    assert [row["audio_origin"] for row in plan.segments] == ["recorded", "synthetic"]
    outputs = service.build_synchronized_audio(lesson.id)
    samples, rate = sf.read(outputs.wav_path)
    assert rate == 24000 and len(samples) == 24000
    assert np.max(np.abs(samples[1000:3000])) > 0.05
    assert np.max(np.abs(samples[10800:13200])) == 0  # source gap 0.45–0.55 s
    assert np.max(np.abs(samples[15000:17000])) > 0.05
    assert np.max(np.abs(samples[23000:])) == 0
    manifest = json.loads(Path(outputs.manifest_path).read_text())
    assert manifest["publicationReady"] is False
    assert [row["startFrame"] for row in manifest["placements"]] == [0, 14400]
    assert "00:00:00,600 --> 00:00:00,950" in Path(outputs.english_srt_path).read_text()


def test_small_duration_overrun_uses_bounded_tempo(service, wav_bytes, monkeypatch):
    lesson = translated(service, wav_bytes, monkeypatch)
    service.attach_segment_recording(lesson.segments[0].id, "english.wav", english_wav(0.44))
    service.attach_segment_recording(lesson.segments[1].id, "english.wav", english_wav(0.2))
    plan = service.get_dubbing_plan(lesson.id)
    assert plan.ready and 1 < plan.segments[0]["tempo"] <= 1.15
    result = service.build_synchronized_audio(lesson.id)
    assert sf.info(result.wav_path).frames == 24000


def test_long_or_missing_audio_blocks_render_without_trimming(service, wav_bytes, monkeypatch):
    lesson = translated(service, wav_bytes, monkeypatch)
    service.attach_segment_recording(lesson.segments[0].id, "english.wav", english_wav(0.8))
    plan = service.get_dubbing_plan(lesson.id)
    assert not plan.ready
    assert [row["status"] for row in plan.segments] == ["too_long", "missing_audio"]
    with pytest.raises(WorkflowStateError, match="needs correction"):
        service.build_synchronized_audio(lesson.id)
    assert not (service._lesson_dir(lesson.id) / "synchronized").exists()


@pytest.mark.parametrize("speed", [float("nan"), float("inf"), 0.9, 1.5])
def test_tempo_budget_is_bounded(service, speed):
    with pytest.raises(WorkflowStateError):
        service.get_dubbing_plan(1, speed)


def test_source_edit_during_render_rejects_generation(service, wav_bytes, monkeypatch):
    from core import dubbing
    from core.schemas import SourceCueUpdate
    lesson = translated(service, wav_bytes, monkeypatch)
    attach_all(service, lesson)
    cue = service.get_source_timeline(lesson.id).cues[0]
    original = dubbing.render_track
    def render_then_edit(*args):
        result = original(*args)
        service.update_source_cue(cue.id, SourceCueUpdate(expected_version=cue.edit_version, start=0.01))
        return result
    monkeypatch.setattr(dubbing, "render_track", render_then_edit)
    with pytest.raises(WorkflowStateError, match="changed during synchronized"):
        service.build_synchronized_audio(lesson.id)


def test_recording_and_sync_api(service, wav_bytes, monkeypatch):
    lesson = translated(service, wav_bytes, monkeypatch)
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as client:
            for seg in lesson.segments:
                response = client.put(f"/segments/{seg.id}/recording", files={"file": ("english.wav", english_wav(), "audio/wav")})
                assert response.status_code == 200 and response.json()["audio_origin"] == "recorded"
            assert client.get(f"/lessons/{lesson.id}/dubbing-plan").json()["ready"]
            assert client.post(f"/lessons/{lesson.id}/synchronized-audio", json={"max_speed": 1.15}).status_code == 200
            assert client.post(f"/lessons/{lesson.id}/synchronized-audio", json={"max_speed": 5}).status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_mp4_keeps_original_video_packets_and_replaces_audio(service, wav_bytes, monkeypatch, tmp_path):
    from core import audio_utils
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    source = tmp_path / "source.mp4"
    tone = tmp_path / "original.wav"
    tone.write_bytes(english_wav(1.0))
    audio_utils._run([audio_utils._ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                      "color=c=blue:s=160x90:r=25:d=1", "-i", str(tone), "-c:v", "mpeg4", "-c:a", "aac", str(source)])
    service.attach_source_audio(lesson.id, source.name, source.read_bytes())
    service.transcribe(lesson.id)
    lesson = service.translate(lesson.id, source_timed=True)
    attach_all(service, lesson)
    result = service.build_synchronized_audio(lesson.id)
    assert result.video_path and Path(result.video_path).is_file()
    def video_hash(path):
        return subprocess.check_output([audio_utils._ffmpeg(), "-v", "error", "-i", str(path), "-map", "0:v:0",
                                        "-c:v", "copy", "-f", "hash", "-hash", "sha256", "-"], text=True).strip()
    assert video_hash(source) == video_hash(result.video_path)
    assert json.loads(Path(result.manifest_path).read_text())["timeline"] == "original_source"
