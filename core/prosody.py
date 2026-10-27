"""Shadowing-guide validation and a rule-based fallback (linking + flap-T) for General American."""

from __future__ import annotations

import difflib
import re

from core.accent_evaluator import normalize_text

_WORD_RE = re.compile(r"[A-Za-z']+")
_VOWELS = set("aeiouAEIOU")
_CONSONANTS = set("bcdfghjklmnpqrstvxzBCDFGHJKLMNPQRSTVXZ")


def _ends_in_consonant_sound(word: str) -> bool:
    w = word.rstrip("'")
    if not w:
        return False
    if w[-1] in _CONSONANTS:
        return True
    # Silent final e after a consonant ("made", "time") still ends in a consonant sound.
    return len(w) > 2 and w[-1] in "eE" and w[-2] in _CONSONANTS and w.lower() not in {"the", "be", "he", "me", "we", "she"}


def auto_coach_guide(text: str) -> str:
    """Mark consonant->vowel linking with `_` and flap-T with `[d]`, from spelling alone.

    Stress needs real prosodic analysis, so this fallback doesn't mark it; it is used only
    when the LLM's guide fails validation.
    """
    tokens = re.split(r"(\s+)", text.strip())
    out: list[str] = []
    for idx, tok in enumerate(tokens):
        if tok.isspace() or not tok:
            out.append(tok)
            continue
        nxt = tokens[idx + 2] if idx + 2 < len(tokens) else ""
        word_match = _WORD_RE.fullmatch(tok)
        next_starts_vowel = bool(nxt) and nxt[0] in _VOWELS
        # Intra-word flap: vowel + tt + vowel (better, matter, getting).
        tok = re.sub(r"(?<=[aeiouy])tt(?=[aeiouy])", "[d]", tok)
        if word_match and next_starts_vowel:
            # Cross-word flap: "about it" -> "abou[d]_it", "get up" -> "ge[d]_up".
            if re.search(r"[aeiou]t$", tok):
                tok = tok[:-1] + "[d]"
                out.append(tok)
                out.append("_")
                continue
            if _ends_in_consonant_sound(tok):
                out.append(tok)
                out.append("_")
                continue
        out.append(tok)
    # Join "word" + "_" + " " + "next" into "word_next".
    return re.sub(r"_\s+", "_", "".join(out))


def coach_guide_is_valid(guide: str, display: str, min_similarity: float = 0.85) -> bool:
    """A usable guide keeps the display's words (no phonetic respelling) and isn't all caps."""
    if not guide.strip():
        return False
    plain = re.sub(r"\*\*|\[d\]", "", guide)
    letters = [c for c in plain if c.isalpha()]
    upper_ratio = sum(c.isupper() for c in letters) / max(len(letters), 1)
    if upper_ratio > 0.6:
        return False
    guide_words = normalize_text(guide.replace("**", "")).split()
    display_words = normalize_text(display).split()
    ratio = difflib.SequenceMatcher(None, guide_words, display_words, autojunk=False).ratio()
    return ratio >= min_similarity


# Function words are unstressed (and often reduced) in General American; content words carry the beat.
FUNCTION_WORDS = frozenset("""
a an the and or but nor so yet if as than that which who whom whose what when where while because
of to in on at by for from with about into onto over under upon within without between among through
during before after against toward towards via per
i me my mine we us our you your he him his she her it its they them their this these those there here
is are was were be been being am do does did have has had will would shall should can could may might must
not no just also then too very one's one
it's that's there's here's what's who's i'm you're we're they're he's she's i've we've you've they've
i'll we'll you'll he'll she'll they'll i'd we'd you'd he'd she'd they'd
""".split())

_STRESS_WORD_RE = re.compile(r"[A-Za-z'\x00]+")
_IPA_GROUP_RE = re.compile(r"\([^)]*[əɪʊæɑɔʌθðŋʃʒ][^)]*\)")


def add_content_word_stress(guide: str) -> str:
    """Bold-caps every content word (keeps `_` links, `[d]` flaps and IPA reductions intact)."""
    protected: list[str] = []

    def _protect(m: re.Match[str]) -> str:
        protected.append(m.group(0))
        return f"\x01{len(protected) - 1}\x01"

    text = _IPA_GROUP_RE.sub(_protect, guide).replace("[d]", "\x00")

    def _stress(m: re.Match[str]) -> str:
        word = m.group(0)
        bare = word.replace("\x00", "t").strip("'").lower()
        if not bare or bare in FUNCTION_WORDS or bare.isdigit():
            return word
        return f"**{word.upper()}**"

    text = re.sub(r"\x01\d+\x01|[A-Za-z'\x00]+", lambda m: m.group(0) if m.group(0).startswith("\x01") else _stress(m), text)
    text = text.replace("\x00", "[d]")
    return re.sub(r"\x01(\d+)\x01", lambda m: protected[int(m.group(1))], text)
