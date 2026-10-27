"""Central configuration (read from `.env`) and Apple Silicon hardware detection."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

LLMProvider = Literal["ollama", "openai", "anthropic"]
TTSEngineName = Literal["xtts", "elevenlabs"]
TorchDevice = Literal["auto", "mps", "cpu"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage ---
    data_dir: Path = PROJECT_ROOT / "data"
    prompt_path: Path = PROJECT_ROOT / "prompts" / "halachic_translator.txt"

    # --- ASR (mlx-whisper) ---
    hebrew_asr_model: str = "mlx-community/ivrit-ai-whisper-large-v3-turbo-mlx"
    english_asr_model: str = "mlx-community/whisper-large-v3-mlx"
    # Torah-vocabulary initial prompt. Off by default: the ivrit-ai model already spells the
    # terms correctly, and in testing the prompt made it drop the recording's first word.
    hebrew_asr_context_prompt: bool = False

    # --- LLM translation ---
    llm_provider: LLMProvider = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma4:26b"
    ollama_num_ctx: int = 16384
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    translation_chunk_chars: int = 1400
    llm_temperature: float = 0.2
    llm_timeout_seconds: float = 900.0

    # --- TTS / voice cloning ---
    tts_engine: TTSEngineName = "xtts"
    tts_device: TorchDevice = "auto"
    # XTTS-v2 weights are released under the Coqui Public Model License (non-commercial).
    # Set to true only after reading https://coqui.ai/cpml — it lets the model download non-interactively.
    xtts_accept_cpml: bool = False
    xtts_temperature: float = 0.65
    xtts_speed: float = 1.0
    reference_voice_path: Path = PROJECT_ROOT / "data" / "voice_profiles" / "my_reference_voice.wav"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_multilingual_v2"
    segment_pause_ms: int = 600

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def voice_profiles_dir(self) -> Path:
        return self.data_dir / "voice_profiles"

    @property
    def lexicon_path(self) -> Path:
        return self.data_dir / "lexicon.json"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'kolmud.db'}"

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.projects_dir, self.voice_profiles_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def configure_runtime(self) -> None:
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        if self.xtts_accept_cpml:
            os.environ["COQUI_TOS_AGREED"] = "1"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    # Coqui XTTS fails on a handful of ops under Metal unless CPU fallback is allowed.
    settings.configure_runtime()
    return settings


def detect_hardware() -> dict[str, object]:
    """Report which accelerators are usable, without importing heavy libraries unless needed."""
    info: dict[str, object] = {"mlx_metal": False, "torch_mps": False, "chip": ""}
    try:
        import mlx.core as mx

        info["mlx_metal"] = bool(mx.metal.is_available())
    except Exception:  # noqa: BLE001 - any import/runtime failure means "not available"
        pass
    try:
        import torch

        info["torch_mps"] = bool(torch.backends.mps.is_available())
    except Exception:  # noqa: BLE001
        pass
    try:
        import subprocess

        info["chip"] = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return info
