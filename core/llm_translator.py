"""Hebrew Torah lecture -> Academic/Orthodox English, TTS phonetic script and shadowing guide."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from core.lexicon import PhoneticLexicon, clean_for_tts
from core.prosody import add_content_word_stress, auto_coach_guide, coach_guide_is_valid


class TranslationError(RuntimeError):
    pass


class TermRespelling(BaseModel):
    term: str
    phonetic: str


class TranslatedSegment(BaseModel):
    segment_id: int = 0
    hebrew_clean: str = ""
    english_display: str = Field(min_length=1)
    hebrew_terms: list[TermRespelling] = Field(default_factory=list)
    shadowing_coach_guide: str = ""
    # Built deterministically from english_display + lexicon + hebrew_terms, never by the LLM:
    # models asked to write it also respell ordinary English words, which ruins the audio.
    english_tts_phonetic: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


SEGMENTS_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "segment_id": {"type": "integer"},
                    "hebrew_clean": {"type": "string"},
                    "english_display": {"type": "string"},
                    "hebrew_terms": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"term": {"type": "string"}, "phonetic": {"type": "string"}},
                            "required": ["term", "phonetic"],
                        },
                    },
                    "shadowing_coach_guide": {"type": "string"},
                },
                "required": [
                    "segment_id", "hebrew_clean", "english_display",
                    "hebrew_terms", "shadowing_coach_guide",
                ],
            },
        }
    },
    "required": ["segments"],
}


# --------------------------------------------------------------------------- providers
def _is_local(url: str) -> bool:
    return httpx.URL(url).host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _local_aware_client(base_url: str, timeout: float) -> httpx.Client:
    # httpx picks up the macOS system proxy and ignores its localhost bypass list, which
    # breaks requests to a local Ollama. Never route loopback traffic through a proxy.
    return httpx.Client(timeout=timeout, trust_env=not _is_local(base_url))


class JSONCompletionProvider(Protocol):
    name: str

    def complete_json(self, system_prompt: str, user_prompt: str) -> str: ...


@dataclass
class OllamaProvider:
    """Native Ollama API: lets us raise num_ctx, enforce the JSON schema and disable 'thinking'."""

    model: str
    base_url: str = "http://localhost:11434"
    num_ctx: int = 16384
    temperature: float = 0.2
    timeout: float = 900.0
    name: str = "ollama"

    def _supports_thinking(self, client: httpx.Client) -> bool:
        try:
            resp = client.post(f"{self.base_url}/api/show", json={"model": self.model})
            resp.raise_for_status()
            return "thinking" in resp.json().get("capabilities", [])
        except httpx.HTTPError:
            return False

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        with _local_aware_client(self.base_url, self.timeout) as client:
            body: dict = {
                "model": self.model,
                "stream": False,
                "format": SEGMENTS_JSON_SCHEMA,
                "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if self._supports_thinking(client):
                body["think"] = False
            try:
                resp = client.post(f"{self.base_url}/api/chat", json=body)
            except httpx.ConnectError as exc:
                raise TranslationError(
                    f"Cannot reach Ollama at {self.base_url}. Start it with `ollama serve`."
                ) from exc
            if resp.status_code >= 400:
                raise TranslationError(f"Ollama error {resp.status_code}: {resp.text[:500]}")
            return resp.json().get("message", {}).get("content", "")


def list_ollama_models(base_url: str, timeout: float = 3.0) -> list[str]:
    try:
        with _local_aware_client(base_url, timeout) as client:
            resp = client.get(f"{base_url}/api/tags")
        resp.raise_for_status()
        return sorted(m["name"] for m in resp.json().get("models", []))
    except (httpx.HTTPError, KeyError, ValueError):
        return []


@dataclass
class OpenAICompatibleProvider:
    """OpenAI, or any OpenAI-compatible endpoint (Gemini, LM Studio, vLLM, OpenRouter...)."""

    model: str
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.2
    timeout: float = 900.0
    name: str = "openai"

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        from openai import BadRequestError, OpenAI

        if not self.api_key:
            raise TranslationError("An API key is required for the OpenAI-compatible provider.")
        client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
        }
        try:
            response = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
        except BadRequestError:
            # Some compatible servers reject response_format; the parser tolerates free text.
            response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""


@dataclass
class AnthropicProvider:
    model: str
    api_key: str
    max_tokens: int = 16000
    timeout: float = 900.0
    name: str = "anthropic"

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise TranslationError("ANTHROPIC_API_KEY is required for the Anthropic provider.")
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise TranslationError(f"Anthropic error {resp.status_code}: {resp.text[:500]}")
        return "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")


# --------------------------------------------------------------------------- helpers
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.?!:׃])\s+")


def chunk_hebrew_text(text: str, max_chars: int) -> list[str]:
    """Pack paragraphs/sentences into chunks of at most ~max_chars, never splitting mid-word."""
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = " ".join(paragraph.split())
        for sentence in _SENTENCE_SPLIT_RE.split(paragraph):
            if len(sentence) <= max_chars:
                pieces.append(sentence)
                continue
            words, current = sentence.split(), ""
            for word in words:
                if current and len(current) + 1 + len(word) > max_chars:
                    pieces.append(current)
                    current = word
                else:
                    current = f"{current} {word}".strip()
            if current:
                pieces.append(current)
        pieces.append("\n\n")  # paragraph marker, kept so the LLM sees the speaker's structure

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if piece == "\n\n":
            current = current.rstrip() + "\n\n" if current else current
            continue
        if current and len(current) + len(piece) + 1 > max_chars:
            chunks.append(current.strip())
            current = ""
        current += (" " if current and not current.endswith("\n\n") else "") + piece
    if current.strip():
        chunks.append(current.strip())
    return chunks


def extract_json_object(raw: str) -> dict:
    """Recover the JSON object from a model reply (tolerates <think> blocks and code fences)."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise TranslationError(f"Model did not return JSON. Reply began with: {text[:200]!r}")
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise TranslationError(f"Model returned malformed JSON: {exc}") from exc
    if isinstance(parsed, list):
        parsed = {"segments": parsed}
    if not isinstance(parsed, dict):
        raise TranslationError("Model JSON was not an object")
    return parsed


ProgressCallback = Callable[[int, int], None]


# --------------------------------------------------------------------------- engine
class TorahTranslatorEngine:
    def __init__(
        self,
        provider: JSONCompletionProvider,
        lexicon: PhoneticLexicon,
        system_prompt_path: Path,
        chunk_chars: int = 1400,
        max_attempts: int = 2,
    ) -> None:
        self.provider = provider
        self.lexicon = lexicon
        self.system_prompt_path = Path(system_prompt_path)
        self.chunk_chars = chunk_chars
        self.max_attempts = max_attempts

    def _system_prompt(self) -> str:
        base = self.system_prompt_path.read_text(encoding="utf-8")
        entries = self.lexicon.entries()
        if not entries:
            return base
        glossary = "\n".join(f"  * {term} -> {phon}" for term, phon in sorted(entries.items()))
        return (
            f"{base}\n\nPROJECT LEXICON — use exactly these transliterations in \"english_display\" "
            f"and exactly these respellings in \"hebrew_terms\":\n{glossary}\n"
        )

    def build_tts_script(self, display: str, terms: dict[str, str] | None = None) -> str:
        return self.lexicon.apply(clean_for_tts(display), extra=terms)

    def _translate_chunk(self, system_prompt: str, chunk: str, previous_english: str) -> list[TranslatedSegment]:
        context = (
            f"CONTEXT — the end of the previous part, already translated (do NOT translate it again):\n"
            f"{previous_english}\n\n"
            if previous_english
            else ""
        )
        user_prompt = f"{context}HEBREW TRANSCRIPT TO PROCESS:\n{chunk}"
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            prompt = user_prompt if attempt == 0 else (
                f"{user_prompt}\n\nYour previous reply was not valid JSON matching the schema. "
                "Reply with ONLY the JSON object."
            )
            try:
                parsed = extract_json_object(self.provider.complete_json(system_prompt, prompt))
                segments = [TranslatedSegment.model_validate(s) for s in parsed.get("segments", [])]
                if segments:
                    return segments
                last_error = TranslationError("Model returned no segments")
            except (TranslationError, ValidationError) as exc:
                last_error = exc
        raise TranslationError(f"Translation failed after {self.max_attempts} attempts: {last_error}")

    def process_transcript(self, hebrew_text: str, on_progress: ProgressCallback | None = None) -> list[TranslatedSegment]:
        if not hebrew_text.strip():
            raise TranslationError("The Hebrew transcript is empty.")
        system_prompt = self._system_prompt()
        chunks = chunk_hebrew_text(hebrew_text, self.chunk_chars)
        results: list[TranslatedSegment] = []
        for idx, chunk in enumerate(chunks):
            if on_progress:
                on_progress(idx, len(chunks))
            previous = results[-1].english_display if results else ""
            results.extend(self._translate_chunk(system_prompt, chunk, previous))
        if on_progress:
            on_progress(len(chunks), len(chunks))

        for number, seg in enumerate(results, start=1):
            seg.segment_id = number
            # Keep only terms that really occur in the text (models sometimes invent extras).
            display_lower = seg.english_display.lower()
            seg.hebrew_terms = [
                t for t in seg.hebrew_terms if t.term and t.phonetic and t.term.lower() in display_lower
            ]
            seg.english_tts_phonetic = self.build_tts_script(
                seg.english_display, {t.term: t.phonetic for t in seg.hebrew_terms}
            )
            if not coach_guide_is_valid(seg.shadowing_coach_guide, seg.english_display):
                seg.shadowing_coach_guide = auto_coach_guide(seg.english_display)
            if "**" not in seg.shadowing_coach_guide:
                seg.shadowing_coach_guide = add_content_word_stress(seg.shadowing_coach_guide)
        return results
