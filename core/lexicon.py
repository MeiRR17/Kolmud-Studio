"""Halachic-phonetic lexicon: transliterated term -> TTS respelling, persisted as JSON."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path


class PhoneticLexicon:
    """Deterministic last-pass phonetic override applied to every TTS script.

    Even when the LLM forgets to respell a term, this guarantees that every known
    Hebrew/Aramaic term reaches the TTS engine in its phonetic form.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: dict[str, str] = {}
        self._pattern: re.Pattern[str] | None = None
        self._lookup: dict[str, str] = {}
        self.reload()

    # ----- persistence -----
    def reload(self) -> None:
        with self._lock:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError(f"Lexicon at {self.path} must be a JSON object of term -> phonetic")
                self._entries = {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip()}
            else:
                self._entries = {}
            self._compile()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = dict(sorted(self._entries.items(), key=lambda kv: kv[0].lower()))
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _compile(self) -> None:
        self._pattern, self._lookup = _compile_mapping(self._entries)

    # ----- CRUD -----
    def entries(self) -> dict[str, str]:
        with self._lock:
            return dict(self._entries)

    def upsert(self, term: str, phonetic: str) -> None:
        term, phonetic = term.strip(), phonetic.strip()
        if not term or not phonetic:
            raise ValueError("Both term and phonetic spelling are required")
        with self._lock:
            # Replace case-insensitively so "shabbat" and "Shabbat" don't coexist.
            for existing in [k for k in self._entries if k.lower() == term.lower()]:
                del self._entries[existing]
            self._entries[term] = phonetic
            self._save()
            self._compile()

    def delete(self, term: str) -> bool:
        with self._lock:
            matches = [k for k in self._entries if k.lower() == term.strip().lower()]
            for k in matches:
                del self._entries[k]
            if matches:
                self._save()
                self._compile()
            return bool(matches)

    def replace_all(self, entries: dict[str, str]) -> None:
        cleaned = {k.strip(): v.strip() for k, v in entries.items() if k and k.strip() and v and v.strip()}
        with self._lock:
            self._entries = cleaned
            self._save()
            self._compile()

    # ----- application -----
    def apply(self, text: str, extra: dict[str, str] | None = None) -> str:
        """Respell known terms. `extra` (e.g. LLM-suggested terms) applies where the lexicon is silent."""
        with self._lock:
            pattern, lookup = self._pattern, self._lookup
            if extra:
                merged = {k.strip(): v.strip() for k, v in extra.items() if k.strip() and v.strip()}
                merged = {k: v for k, v in merged.items() if k.lower() not in self._lookup}
                merged.update(self._entries)
                pattern, lookup = _compile_mapping(merged)
        if pattern is None or not text:
            return text
        return pattern.sub(lambda m: lookup[m.group(0).lower()], text)

    def is_term(self, word: str) -> bool:
        with self._lock:
            return word.strip().lower() in self._lookup


def _compile_mapping(entries: dict[str, str]) -> tuple[re.Pattern[str] | None, dict[str, str]]:
    if not entries:
        return None, {}
    # Longest first so multi-word terms ("Avot Melacha") win over their parts ("Melacha").
    terms = sorted(entries, key=len, reverse=True)
    alternation = "|".join(re.escape(t) for t in terms)
    # One combined pass, so a replacement's output is never re-scanned by a shorter term.
    pattern = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)
    return pattern, {t.lower(): v for t, v in entries.items()}


_MARKUP_RE = re.compile(r"[*`#]")
_PAREN_IPA_RE = re.compile(r"\s*\([^)]*[əɪʊæɑɔʌθðŋʃʒ][^)]*\)")


def clean_for_tts(text: str) -> str:
    """Strip any coaching markup/IPA that leaked into a TTS script (it would be read aloud)."""
    text = _PAREN_IPA_RE.sub("", text)
    # Spoken glosses: "activity (Melacha) is" -> "activity, Melacha, is" for a natural pause.
    text = re.sub(r"\s*\(([^)]+)\)", r", \1,", text)
    text = re.sub(r",\s*([.,;:!?—])", r"\1", text)
    text = text.replace("[d]", "t").replace("_", " ")
    text = _MARKUP_RE.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()
