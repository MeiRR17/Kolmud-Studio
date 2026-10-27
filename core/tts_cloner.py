"""Cross-lingual voice cloning: local Coqui XTTS-v2 (Metal/CPU) or the optional ElevenLabs API."""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Any, Protocol

import re

import httpx
import numpy as np
import soundfile as sf

from core.audio_utils import to_wav

log = logging.getLogger(__name__)

XTTS_MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"


class TTSError(RuntimeError):
    pass


def split_for_tts(text: str, limit: int = 240) -> list[str]:
    """Split into sentences no longer than `limit` chars (XTTS-v2 caps English at 250).

    Long sentences are broken at clause punctuation, then at word boundaries.
    (coqui's own splitter needs spaCy, which this project doesn't otherwise require.)
    """
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.!?;])\s+", " ".join(text.split())):
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        current = ""
        for clause in re.split(r"(?<=[,:—–])\s+", sentence):
            if len(clause) > limit:
                words = clause.split()
                clause_parts, part = [], ""
                for word in words:
                    if part and len(part) + 1 + len(word) > limit:
                        clause_parts.append(part)
                        part = word
                    else:
                        part = f"{part} {word}".strip()
                clause_parts.append(part)
            else:
                clause_parts = [clause]
            for part in clause_parts:
                if current and len(current) + 1 + len(part) > limit:
                    pieces.append(current)
                    current = part
                else:
                    current = f"{current} {part}".strip()
        if current:
            pieces.append(current)
    return [p for p in pieces if p.strip()]


class TTSEngine(Protocol):
    name: str

    def synthesize(self, text: str, output_wav: Path) -> Path: ...


def resolve_torch_device(preference: str) -> str:
    import torch

    if preference == "cpu":
        return "cpu"
    if preference == "mps" or preference == "auto":
        if torch.backends.mps.is_available():
            return "mps"
        if preference == "mps":
            log.warning("MPS requested but unavailable; using CPU")
    return "cuda" if torch.cuda.is_available() else "cpu"


class XTTSVoiceCloner:
    """Coqui XTTS-v2 zero-shot cloning from a 10-30 s reference WAV of the user's voice.

    The model is loaded lazily, speaker latents are cached per reference file, and if a
    Metal (MPS) kernel fails at inference time the model transparently moves to CPU.
    """

    name = "xtts"

    def __init__(
        self,
        reference_wav: Path,
        device_preference: str = "auto",
        temperature: float = 0.65,
        speed: float = 1.0,
        sentence_pause_ms: int = 140,
    ) -> None:
        self.reference_wav = Path(reference_wav)
        self.device_preference = device_preference
        self.temperature = temperature
        self.speed = speed
        self.sentence_pause_ms = sentence_pause_ms
        self.device: str | None = None
        self._tts: Any = None
        self._latents_key: tuple[str, float, int, str] | None = None
        self._latents: tuple[Any, Any] | None = None
        self._lock = threading.RLock()

    # ----- model lifecycle -----
    def _ensure_model(self) -> Any:
        if self._tts is None:
            import os

            if os.environ.get("COQUI_TOS_AGREED") != "1":
                raise TTSError(
                    "XTTS-v2 is distributed under the Coqui Public Model License (non-commercial). "
                    "Read https://coqui.ai/cpml and, if you agree, set XTTS_ACCEPT_CPML=true in .env."
                )
            from TTS.api import TTS

            self.device = resolve_torch_device(self.device_preference)
            log.info("Loading XTTS-v2 on %s", self.device)
            self._tts = TTS(XTTS_MODEL_NAME, progress_bar=False).to(self.device)
        return self._tts.synthesizer.tts_model

    def _move_to_cpu(self) -> None:
        log.warning("XTTS inference failed on %s; falling back to CPU", self.device)
        self._tts.to("cpu")
        self.device = "cpu"
        self._latents_key = None

    def _speaker_latents(self, model: Any) -> tuple[Any, Any]:
        if not self.reference_wav.exists():
            raise FileNotFoundError(
                f"Reference voice missing at {self.reference_wav}. Record or upload a clean "
                "15-30 second WAV of your voice first."
            )
        stat = self.reference_wav.stat()
        key = (str(self.reference_wav), stat.st_mtime, stat.st_size, self.device or "")
        if self._latents_key != key or self._latents is None:
            self._latents = model.get_conditioning_latents(
                audio_path=[str(self.reference_wav)],
                gpt_cond_len=30,
                gpt_cond_chunk_len=4,
                max_ref_length=60,
            )
            self._latents_key = key
        return self._latents

    def _infer_sentence(self, model: Any, sentence: str) -> np.ndarray:
        gpt_cond_latent, speaker_embedding = self._speaker_latents(model)
        out = model.inference(
            sentence,
            "en",
            gpt_cond_latent,
            speaker_embedding,
            temperature=self.temperature,
            speed=self.speed,
            enable_text_splitting=False,
        )
        wav = out["wav"]
        return wav.cpu().numpy() if hasattr(wav, "cpu") else np.asarray(wav, dtype=np.float32)

    # ----- public -----
    def synthesize(self, text: str, output_wav: Path) -> Path:
        text = text.strip()
        if not text:
            raise TTSError("Cannot synthesize empty text")
        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            model = self._ensure_model()
            sample_rate = int(model.config.audio.output_sample_rate)
            gap = np.zeros(int(sample_rate * self.sentence_pause_ms / 1000), dtype=np.float32)
            pieces: list[np.ndarray] = []
            for sentence in split_for_tts(text, model.tokenizer.char_limits["en"] - 10):
                try:
                    wav = self._infer_sentence(model, sentence)
                except (RuntimeError, NotImplementedError, TypeError) as exc:
                    if self.device != "mps":
                        raise TTSError(f"XTTS synthesis failed: {exc}") from exc
                    self._move_to_cpu()
                    wav = self._infer_sentence(model, sentence)
                if pieces:
                    pieces.append(gap)
                pieces.append(wav.astype(np.float32).reshape(-1))
            if not pieces:
                raise TTSError("Nothing to synthesize after sentence splitting")
            sf.write(str(output_wav), np.concatenate(pieces), sample_rate, subtype="PCM_16")
        return output_wav


class ElevenLabsVoiceCloner:
    name = "elevenlabs"

    def __init__(self, api_key: str, voice_id: str, model_id: str = "eleven_multilingual_v2") -> None:
        if not api_key or not voice_id:
            raise TTSError("ElevenLabs needs both an API key and a cloned Voice ID.")
        self.api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id

    def synthesize(self, text: str, output_wav: Path) -> Path:
        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        resp = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": self.model_id,
                "voice_settings": {
                    "stability": 0.55,
                    "similarity_boost": 0.85,
                    "style": 0.15,
                    "use_speaker_boost": True,
                },
            },
            timeout=180.0,
        )
        if resp.status_code >= 400:
            raise TTSError(f"ElevenLabs error {resp.status_code}: {resp.text[:400]}")
        # The API returns MP3; store WAV so every engine produces the same format downstream.
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = Path(tmp.name)
        try:
            to_wav(tmp_path, output_wav, sample_rate=24000)
        finally:
            tmp_path.unlink(missing_ok=True)
        return output_wav
