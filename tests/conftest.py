from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.config import PROJECT_ROOT, Settings
from core.pipeline import StudioService


class FakeProvider:
    """Echoes a deterministic 'translation' so the pipeline can be tested without an LLM."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append(user_prompt)
        chunk = user_prompt.split("HEBREW TRANSCRIPT TO PROCESS:\n", 1)[1]
        return json.dumps({"segments": [{
            "segment_id": 1,
            "hebrew_clean": chunk,
            "english_display": "On Shabbat, the Rambam explains the Melacha of Borer.",
            "hebrew_terms": [{"term": "Borer", "phonetic": "Boh-rehr"}, {"term": "Shabbat", "phonetic": "Shab-bat"},
                             {"term": "Muktzeh", "phonetic": "Mook-tzeh"}],
            "shadowing_coach_guide": "On **SHABBAT**, the **RAMBAM** explains_the **MELACHA** of (əv) **BORER**.",
        }]})


class FakeTTS:
    name = "fake"

    def synthesize(self, text: str, output_wav: Path) -> Path:
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        t = np.linspace(0, 1.0, 24000, endpoint=False)
        sf.write(str(output_wav), (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 24000)
        return output_wav


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StudioService:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(PROJECT_ROOT / "data" / "lexicon.json", data / "lexicon.json")
    settings = Settings(_env_file=None, data_dir=data, reference_voice_path=data / "voice_profiles" / "ref.wav")
    settings.ensure_dirs()
    svc = StudioService(settings)
    provider, tts = FakeProvider(), FakeTTS()
    monkeypatch.setattr(svc, "make_llm_provider", lambda choice=None: provider)
    monkeypatch.setattr(svc, "make_tts_engine", lambda choice=None: tts)
    monkeypatch.setattr(svc.asr, "transcribe_hebrew_lecture", lambda path, **kw: {
        "full_text": "נחלקו הראשונים בגדר מלאכת בורר בשבת.",
        "segments": [{"start": 0.0, "end": 2.0, "text": "נחלקו הראשונים בגדר מלאכת בורר בשבת."}],
    })
    monkeypatch.setattr(svc.asr, "transcribe_english_practice", lambda path: {
        "recognized_text": "On Shabbat the Rambam explains the melakha of boy",
        "words": [{"word": w, "start": i * 0.4, "end": i * 0.4 + 0.35, "probability": 0.9 if w != "boy" else 0.4}
                  for i, w in enumerate("On Shabbat the Rambam explains the melakha of boy".split())],
    })
    svc.fake_provider = provider  # type: ignore[attr-defined]
    return svc


@pytest.fixture
def wav_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "tone.wav"
    t = np.linspace(0, 1.0, 16000, endpoint=False)
    sf.write(str(path), (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), 16000)
    return path.read_bytes()
