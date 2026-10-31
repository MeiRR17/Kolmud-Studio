from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app, get_service
from core.pipeline import StudioService
from core.schemas import SegmentUpdate


def test_full_pipeline(service: StudioService, wav_bytes: bytes) -> None:
    lesson = service.create_lesson("הלכות בורר")
    service.attach_source_audio(lesson.id, "lecture.mp3", wav_bytes)
    lesson = service.transcribe(lesson.id)
    assert "בורר" in lesson.hebrew_transcript

    lesson = service.translate(lesson.id)
    assert len(lesson.segments) == 1
    seg = lesson.segments[0]
    # TTS script is built from the display text: the lexicon wins over the LLM's respellings,
    # and terms the LLM invented (Muktzeh isn't in the text) are dropped.
    assert seg.english_tts_phonetic == "On Shah-baht, the Rahm-bahm explains the Meh-lah-khah of Boh-rer."
    assert [t["term"] for t in seg.hebrew_terms] == ["Borer", "Shabbat"]

    seg = service.synthesize_segment(seg.id)
    assert seg.audio_path and not seg.audio_is_stale and seg.audio_duration == 1.0
    assert service.segment_audio(seg.id, 0.75).name.endswith("@0.75x.wav")

    seg = service.update_segment(seg.id, SegmentUpdate(english_tts_phonetic="Edited."))
    assert seg.audio_is_stale
    seg = service.regenerate_phonetic(seg.id)
    assert "Meh-lah-khah" in seg.english_tts_phonetic

    master = service.build_master(lesson.id)
    assert Path(master.mp3_path).exists() and Path(master.srt_path).read_text().strip()

    attempt = service.evaluate_practice(seg.id, wav_bytes)
    statuses = {t["word"]: t["status"] for t in attempt.report["visual_diff"]}
    assert statuses["melacha"] == "close"          # Hebrew term, lenient
    assert statuses["borer"] == "mispronounced"    # heard as "boy"
    assert attempt.report["low_confidence_words"] == ["boy"]
    assert [a.id for a in service.list_attempts(seg.id)] == [attempt.id]

    # Re-translating replaces segments (and their history) cleanly
    lesson = service.translate(lesson.id)
    assert lesson.segments[0].id != seg.id and lesson.master_audio_path is None

    service.delete_lesson(lesson.id)
    assert service.list_lessons() == []


def test_long_transcript_is_chunked(service: StudioService) -> None:
    lesson = service.create_lesson("ארוך")
    service.set_transcript(lesson.id, "משפט בשיעור על הלכות שבת. " * 200)
    lesson = service.translate(lesson.id)
    calls = service.fake_provider.calls  # type: ignore[attr-defined]
    assert len(calls) > 1 and len(lesson.segments) == len(calls)
    assert [s.position for s in lesson.segments] == list(range(1, len(calls) + 1))
    assert "CONTEXT" in calls[1]  # continuity context passed to later chunks


def test_api_endpoints(service: StudioService, wav_bytes: bytes) -> None:
    app.dependency_overrides[get_service] = lambda: service
    try:
        client = TestClient(app)
        lid = client.post("/lessons", json={"title": "API"}).json()["id"]
        assert client.post(f"/lessons/{lid}/audio", files={"file": ("a.wav", wav_bytes, "audio/wav")}).status_code == 200
        assert client.post(f"/lessons/{lid}/transcribe").status_code == 200
        lesson = client.post(f"/lessons/{lid}/translate", json={}).json()
        sid = lesson["segments"][0]["id"]
        assert client.post(f"/lessons/{lid}/master").status_code == 409  # nothing synthesized yet
        assert client.post(f"/lessons/{lid}/synthesize").json()["segments"][0]["audio_path"]
        assert client.get(f"/segments/{sid}/audio", params={"speed": 0.9}).status_code == 200
        assert client.post(f"/lessons/{lid}/master").json()["segment_count"] == 1
        assert client.get(f"/lessons/{lid}/master/srt").status_code == 200
        report = client.post(f"/segments/{sid}/practice", files={"file": ("t.wav", wav_bytes, "audio/wav")}).json()
        assert report["clarity_score"] > 0
        assert client.put("/lexicon", json={"term": "Chacham", "phonetic": "Khah-khahm"}).json()["Chacham"]
        assert client.post("/lexicon/apply", json={"text": "a Chacham"}).json()["text"] == "a Khah-khahm"
        assert client.delete("/lexicon/Chacham").status_code == 200
        assert client.get("/lessons/9999").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_ollama_model_is_unloaded_after_translation_even_on_failure(service, monkeypatch) -> None:
    from core.llm_translator import OllamaProvider, TranslationError

    unloaded: list[str] = []

    class FakeOllama(OllamaProvider):
        def __init__(self, fail: bool) -> None:
            super().__init__(model="gemma4:26b")
            self.fail = fail

        def complete_json(self, system_prompt: str, user_prompt: str) -> str:
            if self.fail:
                raise TranslationError("boom")
            return service.fake_provider.complete_json(system_prompt, user_prompt)  # type: ignore[attr-defined]

        def unload(self) -> None:
            unloaded.append(self.model)

    lesson = service.create_lesson("unload")
    service.set_transcript(lesson.id, "נחלקו הראשונים.")
    monkeypatch.setattr(service, "make_llm_provider", lambda choice=None: FakeOllama(fail=False))
    service.translate(lesson.id)
    monkeypatch.setattr(service, "make_llm_provider", lambda choice=None: FakeOllama(fail=True))
    try:
        service.translate(lesson.id)
    except TranslationError:
        pass
    assert unloaded == ["gemma4:26b", "gemma4:26b"]
