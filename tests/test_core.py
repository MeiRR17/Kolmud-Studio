from __future__ import annotations

from pathlib import Path

import pytest

from core.accent_evaluator import AccentAndFluencyEvaluator, normalize_text
from core.audio_utils import SubtitleCue, write_srt
from core.lexicon import PhoneticLexicon, clean_for_tts
from core.llm_translator import TranslationError, chunk_hebrew_text, extract_json_object


# ---------------------------------------------------------------- lexicon
@pytest.fixture
def lexicon(tmp_path: Path) -> PhoneticLexicon:
    lex = PhoneticLexicon(tmp_path / "lex.json")
    lex.replace_all({
        "Halacha": "Hah-lah-khah", "Halachic": "Hah-lah-khik", "Melacha": "Meh-lah-khah",
        "Avot Melacha": "Ah-vot Meh-lah-khah", "L'chatchila": "Lekhah-t'khee-lah", "Rashi": "Rah-shee",
    })
    return lex


def test_lexicon_longest_match_and_boundaries(lexicon: PhoneticLexicon) -> None:
    text = "The Halachic view of Avot Melacha differs; halacha says L'chatchila, per Rashi's reading."
    assert lexicon.apply(text) == (
        "The Hah-lah-khik view of Ah-vot Meh-lah-khah differs; Hah-lah-khah says Lekhah-t'khee-lah, "
        "per Rah-shee's reading."
    )


def test_lexicon_does_not_touch_substrings(lexicon: PhoneticLexicon) -> None:
    assert lexicon.apply("Rashidun Melachaim") == "Rashidun Melachaim"


def test_lexicon_crud_persists(lexicon: PhoneticLexicon) -> None:
    lexicon.upsert("halacha", "HAH-lah-khah")  # case-insensitive replace, no duplicate
    lexicon.delete("Rashi")
    reloaded = PhoneticLexicon(lexicon.path)
    assert reloaded.entries()["halacha"] == "HAH-lah-khah"
    assert "Halacha" not in reloaded.entries() and "Rashi" not in reloaded.entries()


def test_clean_for_tts_strips_coaching_markup() -> None:
    assert clean_for_tts("**THINK**_about_i[d] to (tə) them") == "THINK about it to them"


# ---------------------------------------------------------------- translator helpers
def test_chunking_respects_limit_and_keeps_all_text() -> None:
    sentence = "זהו משפט ארוך בשיעור על הלכות שבת ומלאכת בורר. "
    text = sentence * 60 + "\n\n" + "פסקה שנייה בלי נקודה " * 80
    chunks = chunk_hebrew_text(text, 500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join("".join(c.split()) for c in chunks) == "".join(text.split())


def test_extract_json_tolerates_think_and_fences() -> None:
    raw = '<think>hmm</think>\nHere you go:\n```json\n{"segments": [{"english_display": "x"}]}\n```'
    assert extract_json_object(raw)["segments"][0]["english_display"] == "x"
    assert extract_json_object('[{"english_display": "y"}]')["segments"][0]["english_display"] == "y"
    with pytest.raises(TranslationError):
        extract_json_object("no json here")


# ---------------------------------------------------------------- evaluator
def test_normalize_keeps_glosses_drops_ipa() -> None:
    assert normalize_text("The **RAMIFICATION** (nafka minah) to (tə) L'chatchila — 3 times") == (
        "the ramification nafka minah to lchatchila three times"
    )


def test_evaluator_statuses_and_scores() -> None:
    ev = AccentAndFluencyEvaluator(is_hebrew_term=lambda w: w in {"machloket", "rishonim"})
    report = ev.evaluate_attempt(
        target_english_text="There is a fundamental machloket among the Rishonim.",
        whisper_recognized_text="There is a fun mental machlokes among the Rishonim today",
        word_timings=[{"word": "There", "start": 0.5, "end": 0.7, "probability": 0.99},
                      {"word": "today", "start": 3.3, "end": 3.5, "probability": 0.5}],
        audio_duration_seconds=6.0,
    )
    tokens = {t.word: t for t in report.visual_diff}
    assert tokens["there"].status == "correct"
    assert tokens["machloket"].status == "close"
    assert (tokens["fundamental"].status, tokens["fundamental"].heard_as) == ("close", "fun mental")
    assert tokens["today"].status == "extra"
    assert report.speech_seconds == 3.0
    assert report.wpm == round(10 / 3.0 * 60, 1)
    assert report.low_confidence_words == ["today"]
    # Near misses count as intelligible; the strict WER still records every deviation.
    assert report.clarity_score == 100.0 and report.strict_wer > 0.3


def test_evaluator_empty_recognition_is_zero() -> None:
    report = AccentAndFluencyEvaluator().evaluate_attempt("Hello world", "", [], 2.0)
    assert report.clarity_score == 0.0 and report.strict_wer == 1.0
    assert [t.status for t in report.visual_diff] == ["missed", "missed"]


# ---------------------------------------------------------------- subtitles
def test_srt_splits_long_paragraphs(tmp_path: Path) -> None:
    text = "First sentence here. " + "word " * 40 + "end."
    out = write_srt([SubtitleCue(0.0, 10.0, text)], tmp_path / "x.srt", max_chars=60)
    body = out.read_text(encoding="utf-8")
    assert body.startswith("1\n00:00:00,000 --> ")
    assert body.count("-->") >= 3
    assert "00:00:10,000" in body


def test_evaluator_handles_linked_speech() -> None:
    """Real mlx-whisper output for macOS 'Samantha' reading the target sentence."""
    hebrew = {"machloket", "rishonim", "melacha", "borer", "shabbat"}
    report = AccentAndFluencyEvaluator(is_hebrew_term=lambda w: w in hebrew).evaluate_attempt(
        "There is a fundamental machloket among the Rishonim regarding the Melacha of Borer on Shabbat.",
        "There is a fundamental maklakut among the Rishanim regarding the Melacha of Boron Shabbat.",
        [], 5.0,
    )
    tokens = {t.word: (t.status, t.heard_as) for t in report.visual_diff}
    assert tokens["borer"] == ("close", "boron") and tokens["on"] == ("close", "boron")
    assert tokens["rishonim"][0] == "close"
    assert report.mispronounced_words == [("machloket", "maklakut")]


# ---------------------------------------------------------------- prosody
from core.prosody import auto_coach_guide, coach_guide_is_valid  # noqa: E402


def test_auto_coach_guide_links_and_flaps() -> None:
    assert auto_coach_guide("Think about it, it is better.") == "Think_abou[d]_it, i[d]_is be[d]er."
    assert auto_coach_guide("Make it so.") == "Make_it so."


def test_coach_guide_validation_rejects_respelling_and_caps() -> None:
    display = "There is a fundamental machloket among the Rishonim."
    assert coach_guide_is_valid("There is a **FUN**damental **MACHLOKET** among_the **RISHONIM**.", display)
    assert not coach_guide_is_valid("There's a **FUN**-dah-**MEN**-tal **MAKH**-loh-ket ah-mong", display)
    assert not coach_guide_is_valid("THERE IS A FUNDAMENTAL MACHLOKET AMONG THE RISHONIM.", display)


def test_lexicon_wins_over_llm_terms(lexicon: PhoneticLexicon) -> None:
    out = lexicon.apply("Halacha and Chacham", extra={"Halacha": "WRONG", "Chacham": "Khah-khahm"})
    assert out == "Hah-lah-khah and Khah-khahm"


def test_clean_for_tts_turns_glosses_into_pauses() -> None:
    assert clean_for_tts("an activity (Melacha). Rabbinic (Mid'rabbanan), ok") == (
        "an activity, Melacha. Rabbinic, Mid'rabbanan, ok"
    )


def test_content_word_stress_keeps_markup() -> None:
    from core.prosody import add_content_word_stress

    out = add_content_word_stress("It's the machloke[d]_among the Rishonim, to (tə) say (chiluk).")
    assert out == "It's the **MACHLOKE[d]**_among the **RISHONIM**, to (tə) **SAY** (**CHILUK**)."


def test_split_for_tts_respects_xtts_limit() -> None:
    from core.tts_cloner import split_for_tts

    long_sentence = "This is a clause, " * 30 + "and it ends here."
    parts = split_for_tts("Short one. " + long_sentence + " Last!", limit=100)
    assert parts[0] == "Short one." and parts[-1] == "Last!"
    assert all(len(p) <= 100 for p in parts)
    assert " ".join(parts).split() == ("Short one. " + long_sentence + " Last!").split()
