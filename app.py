"""Kolmud Studio — TorahVoice production studio & American-accent shadowing lab (Streamlit UI).

Run:  streamlit run app.py
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pandas as pd
import streamlit as st

from core import __version__
from core.config import detect_hardware, get_settings
from core.llm_translator import list_ollama_models
from core.pipeline import LLMChoice, NotFoundError, StudioService, TTSChoice
from core.schemas import AttemptOut, LessonOut, SegmentOut, SegmentUpdate

st.set_page_config(page_title=f"Kolmud Studio v{__version__}", page_icon="🎙️", layout="wide", initial_sidebar_state="expanded")

st.markdown(
    """
<style>
.stApp, [data-testid="stSidebar"] { direction: rtl; }
.stApp h1, .stApp h2, .stApp h3, .stApp p, .stApp label, .stApp li { text-align: right; }
textarea, input { unicode-bidi: plaintext; text-align: start; }
[class*="st-key-en_"] textarea, [class*="st-key-en_"] input { direction: ltr; text-align: left; }
[data-testid="stAudio"], [data-testid="stAudioInput"], [data-testid="stDataFrame"],
[data-testid="stDataEditor"], [data-testid="stVegaLiteChart"], [data-testid="stArrowVegaLiteChart"] { direction: ltr; }
.ltr { direction: ltr; text-align: left; unicode-bidi: plaintext; }
.coach { direction: ltr; text-align: left; font-size: 1.25rem; line-height: 2.3rem;
         padding: 1rem 1.25rem; border-radius: .6rem; border: 1px solid rgba(128,128,128,.35); }
.coach .stress { font-weight: 800; color: #d6336c; letter-spacing: .02em; }
.coach .link { color: #1c7ed6; font-weight: 700; margin: 0 -.12em; }
.coach .flap { color: #e8590c; font-weight: 700; border-bottom: 2px dotted #e8590c; }
.coach .reduce { color: #868e96; font-size: .85em; font-style: italic; }
.diff { direction: ltr; text-align: left; font-size: 1.15rem; line-height: 2.2rem; }
.diff span { padding: .1rem .3rem; margin: 0 .1rem; border-radius: .3rem; }
.diff .correct { background: rgba(47,158,68,.18); }
.diff .close { background: rgba(250,176,5,.28); }
.diff .mispronounced { background: rgba(224,49,49,.28); text-decoration: underline wavy #e03131; }
.diff .missed { background: rgba(224,49,49,.12); text-decoration: line-through; color: #868e96; }
.diff .extra { color: #868e96; font-style: italic; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_service() -> StudioService:
    return StudioService(get_settings())


@st.cache_data(ttl=3600)
def hardware() -> dict:
    return detect_hardware()


@st.cache_data(ttl=30)
def ollama_models(base_url: str) -> list[str]:
    return list_ollama_models(base_url)


svc = get_service()
settings = svc.settings


# ======================================================================== rendering helpers
_IPA_PAREN = re.compile(r"\(([^)]*[əɪʊæɑɔʌθðŋʃʒ][^)]*)\)")


def coach_guide_html(text: str) -> str:
    """Turn the LLM's prosody markup into a readable, colour-coded shadowing script."""
    out = html.escape(text)
    out = _IPA_PAREN.sub(r'<span class="reduce">(\1)</span>', out)
    out = re.sub(r"\*\*(.+?)\*\*", r'<span class="stress">\1</span>', out)
    out = out.replace("[d]", '<span class="flap">d</span>')
    out = out.replace("_", '<span class="link">‿</span>')
    return f'<div class="coach">{out}</div>'


def diff_html(tokens: list[dict]) -> str:
    spans = []
    for t in tokens:
        title = f' title="heard: {html.escape(t["heard_as"])}"' if t.get("heard_as") else ""
        spans.append(f'<span class="{t["status"]}"{title}>{html.escape(t["word"])}</span>')
    return f'<div class="diff">{" ".join(spans)}</div>'


def show_error(exc: Exception) -> None:
    st.error(f"{type(exc).__name__}: {exc}")


def abs_path(stored: str | None) -> Path | None:
    p = svc.abs_path(stored)
    return p if p is not None and p.exists() else None


# ======================================================================== sidebar
with st.sidebar:
    st.title("🎙️ Kolmud Studio")
    st.caption(f"v{__version__}")
    hw = hardware()
    st.caption(
        f"{hw.get('chip') or 'Apple Silicon'} · MLX Metal: {'✅' if hw['mlx_metal'] else '❌'}"
        f" · Torch MPS: {'✅' if hw['torch_mps'] else '❌'}"
    )

    st.subheader("📚 שיעורים")
    lessons = svc.list_lessons()
    if lessons:
        ids = [lesson.id for lesson in lessons]
        current = st.session_state.get("lesson_id")
        index = ids.index(current) if current in ids else 0
        st.session_state.lesson_id = st.selectbox(
            "שיעור פעיל:", ids, index=index,
            format_func=lambda i: next(f"#{x.id} · {x.title}" for x in lessons if x.id == i),
        )
    else:
        st.session_state.lesson_id = None
    with st.popover("➕ שיעור חדש", width="stretch"):
        new_title = st.text_input("שם השיעור:", key="new_lesson_title")
        if st.button("צור שיעור", type="primary"):
            created = svc.create_lesson(new_title)
            st.session_state.lesson_id = created.id
            st.rerun()

    st.divider()
    st.subheader("🧠 מנוע תרגום (LLM)")
    provider_labels = {"ollama": "Ollama (מקומי וחינמי)", "openai": "OpenAI / API תואם", "anthropic": "Anthropic Claude"}
    provider = st.selectbox(
        "ספק:", list(provider_labels), format_func=provider_labels.get,
        index=list(provider_labels).index(settings.llm_provider),
    )
    if provider == "ollama":
        base_url = st.text_input("Ollama URL:", value=settings.ollama_base_url, key="en_ollama_url")
        models = ollama_models(base_url)
        if models:
            default = settings.ollama_model if settings.ollama_model in models else models[0]
            model = st.selectbox("מודל מקומי:", models, index=models.index(default))
        else:
            st.warning("Ollama לא זמין — הפעל `ollama serve`.")
            model = st.text_input("שם המודל:", value=settings.ollama_model, key="en_ollama_model")
        llm_choice = LLMChoice("ollama", model, base_url=base_url)
    elif provider == "openai":
        base_url = st.text_input("API Base URL:", value=settings.openai_base_url, key="en_openai_url")
        api_key = st.text_input("API Key:", value=settings.openai_api_key, type="password", key="en_openai_key")
        model = st.text_input("מודל:", value=settings.openai_model, key="en_openai_model")
        llm_choice = LLMChoice("openai", model, api_key, base_url)
    else:
        api_key = st.text_input("API Key:", value=settings.anthropic_api_key, type="password", key="en_anth_key")
        model = st.text_input("מודל:", value=settings.anthropic_model, key="en_anth_model")
        llm_choice = LLMChoice("anthropic", model, api_key)

    st.divider()
    st.subheader("🗣️ שיבוט קול (TTS)")
    tts_labels = {"xtts": "XTTS-v2 (מקומי וחינמי)", "elevenlabs": "ElevenLabs (ענן, בתשלום)"}
    tts_engine = st.radio("מנוע:", list(tts_labels), format_func=tts_labels.get,
                          index=list(tts_labels).index(settings.tts_engine))
    if tts_engine == "elevenlabs":
        el_key = st.text_input("ElevenLabs API Key:", value=settings.elevenlabs_api_key, type="password", key="en_el_key")
        el_voice = st.text_input("Voice ID:", value=settings.elevenlabs_voice_id, key="en_el_voice")
        tts_choice = TTSChoice("elevenlabs", el_key, el_voice)
    else:
        tts_choice = TTSChoice("xtts")
        if not settings.xtts_accept_cpml:
            st.warning("XTTS-v2 דורש אישור רישיון CPML (לא-מסחרי): הגדר `XTTS_ACCEPT_CPML=true` בקובץ `.env`.")
        ref = settings.reference_voice_path
        if ref.exists():
            st.success("דגימת קול ייחוס קיימת ✅")
            st.audio(str(ref))
        else:
            st.info("נדרשת דגימת קול ייחוס (15–30 שניות, חדר שקט).")
        st.caption("💡 טיפ: הקלט את הדגימה **באנגלית**, במבטא האמריקאי הטוב ביותר שלך — המודל מעתיק גם את ההגייה.")
        ref_upload = st.file_uploader("העלה דגימה (WAV/MP3/M4A):", type=["wav", "mp3", "m4a", "flac"], key="ref_upload")
        ref_record = st.audio_input("או הקלט כאן:", key="ref_record")
        if st.button("שמור כדגימת ייחוס", disabled=not (ref_upload or ref_record)):
            src = ref_upload or ref_record
            try:
                svc.save_reference_voice(getattr(src, "name", "voice.wav"), src.getvalue())
                st.success("דגימת הקול נשמרה.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                show_error(exc)


lesson: LessonOut | None = None
if st.session_state.lesson_id is not None:
    try:
        lesson = svc.get_lesson(st.session_state.lesson_id)
    except NotFoundError:
        st.session_state.lesson_id = None

tab_studio, tab_lab, tab_lexicon = st.tabs([
    "🎬 אולפן תמלול, תרגום ושיבוט קול",
    "🎧 מעבדת מבטא ו-Shadowing",
    "📖 מילון מונחים הלכתי-פונטי",
])


# ======================================================================== tab 1: studio
def _save_segment_field(segment_id: int, field: str, key: str) -> None:
    svc.update_segment(segment_id, SegmentUpdate(**{field: st.session_state[key]}))


def _regenerate_phonetic(segment_id: int) -> None:
    seg = svc.regenerate_phonetic(segment_id)
    st.session_state[f"en_phon_{segment_id}"] = seg.english_tts_phonetic


def _add_terms_to_lexicon(terms: list[dict[str, str]]) -> None:
    for t in terms:
        svc.lexicon.upsert(t["term"], t["phonetic"])
    st.toast(f"נוספו {len(terms)} מונחים למילון")


def _init_state(key: str, value: str) -> None:
    if key not in st.session_state:
        st.session_state[key] = value


def render_segment_editor(seg: SegmentOut) -> None:
    status = "🔊" if seg.audio_path and not seg.audio_is_stale else ("⚠️" if seg.audio_is_stale else "⏳")
    with st.expander(f"{status} פסקה {seg.position} · {seg.english_display[:70]}…", expanded=False):
        he_key, disp_key = f"he_seg_{seg.id}", f"en_disp_{seg.id}"
        phon_key, coach_key = f"en_phon_{seg.id}", f"en_coach_{seg.id}"
        _init_state(he_key, seg.hebrew_clean)
        _init_state(disp_key, seg.english_display)
        _init_state(phon_key, seg.english_tts_phonetic)
        _init_state(coach_key, seg.shadowing_coach_guide)

        st.text_area("מקור בעברית:", key=he_key, height=100,
                     on_change=_save_segment_field, args=(seg.id, "hebrew_clean", he_key))
        c1, c2 = st.columns(2)
        with c1:
            st.text_area("אנגלית לתצוגה וכתוביות (english_display):", key=disp_key, height=150,
                         on_change=_save_segment_field, args=(seg.id, "english_display", disp_key))
        with c2:
            st.text_area("סקריפט פונטי למנוע הקול (english_tts_phonetic):", key=phon_key, height=150,
                         on_change=_save_segment_field, args=(seg.id, "english_tts_phonetic", phon_key))
        st.text_area("מדריך Shadowing (shadowing_coach_guide):", key=coach_key, height=110,
                     on_change=_save_segment_field, args=(seg.id, "shadowing_coach_guide", coach_key))

        new_terms = [t for t in seg.hebrew_terms if not svc.lexicon.is_term(t["term"])]
        if new_terms:
            st.caption("מונחים חדשים שזיהה מודל השפה (לא במילון): " +
                       " · ".join(f"`{t['term']}` → `{t['phonetic']}`" for t in new_terms))
            st.button("➕ הוסף למילון", key=f"addterms_{seg.id}", on_click=_add_terms_to_lexicon, args=(new_terms,))

        b1, b2, _ = st.columns([1, 1, 2])
        b1.button("🔁 בנה סקריפט פונטי מהתצוגה + המילון", key=f"regen_{seg.id}",
                  on_click=_regenerate_phonetic, args=(seg.id,))
        if b2.button("🎙️ הפק קול משובט", key=f"tts_{seg.id}", type="primary"):
            with st.spinner("מייצר דיבור באנגלית בקולך…"):
                try:
                    svc.synthesize_segment(seg.id, tts_choice)
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)
        if seg.audio_is_stale:
            st.warning("הסקריפט הפונטי נערך אחרי הפקת האודיו — הפק מחדש.")
        if (audio := abs_path(seg.audio_path)) is not None:
            st.audio(str(audio))


with tab_studio:
    if lesson is None:
        st.info("צור שיעור חדש בסרגל הצד כדי להתחיל.")
    else:
        st.header(f"🎬 {lesson.title}")
        transcript_key = f"he_transcript_{lesson.id}"

        st.subheader("שלב א׳: הקלטת השיעור בעברית")
        col_a, col_b = st.columns([3, 2])
        with col_a:
            lecture = st.file_uploader("העלה הקלטת שיעור (WAV/MP3/M4A/MP4):",
                                       type=["wav", "mp3", "m4a", "mp4", "flac", "ogg", "aac"], key=f"lecture_{lesson.id}")
            if lecture is not None and st.button("⬆️ העלה ותמלל ב-Whisper (מקומי)", type="primary"):
                try:
                    with st.spinner("ממיר אודיו…"):
                        svc.attach_source_audio(lesson.id, lecture.name, lecture.getvalue())
                    with st.spinner("מתמלל עם ivrit-ai Whisper על ה-GPU של ה-Mac… (שיעור של שעה ≈ כמה דקות)"):
                        lesson = svc.transcribe(lesson.id)
                    st.session_state[transcript_key] = lesson.hebrew_transcript
                    st.success("התמלול הושלם!")
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)
        with col_b:
            if (src := abs_path(lesson.source_audio_path)) is not None:
                st.caption("הקלטת המקור:")
                st.audio(str(src))
                if st.button("🔄 תמלל מחדש"):
                    try:
                        with st.spinner("מתמלל…"):
                            lesson = svc.transcribe(lesson.id)
                        st.session_state[transcript_key] = lesson.hebrew_transcript
                    except Exception as exc:  # noqa: BLE001
                        show_error(exc)

        st.subheader("שלב ב׳: עריכת התמלול ותרגום")
        _init_state(transcript_key, lesson.hebrew_transcript)
        st.text_area("טקסט השיעור בעברית (אפשר גם להדביק ישירות):", key=transcript_key, height=260)
        t1, t2, _ = st.columns([1, 2, 2])
        if t1.button("💾 שמור תמלול"):
            svc.set_transcript(lesson.id, st.session_state[transcript_key])
            st.toast("נשמר")
        if t2.button("🌐 תרגם לאנגלית תורנית + הזרקה פונטית + מדריך מבטא", type="primary"):
            text = st.session_state[transcript_key]
            if not text.strip():
                st.warning("נא להזין או לתמלל טקסט בעברית תחילה.")
            else:
                svc.set_transcript(lesson.id, text)
                bar = st.progress(0.0, text="מתרגם…")
                try:
                    lesson = svc.translate(
                        lesson.id, llm_choice,
                        on_progress=lambda done, total: bar.progress(
                            done / max(total, 1), text=f"מתרגם קטע {min(done + 1, total)} מתוך {total}…"),
                    )
                    bar.empty()
                    st.success(f"השיעור פוצל ותורגם ל-{len(lesson.segments)} פסקאות.")
                except Exception as exc:  # noqa: BLE001
                    bar.empty()
                    show_error(exc)
        if lesson.segments:
            st.caption("⚠️ תרגום מחדש מחליף את כל הפסקאות הקיימות (כולל האודיו והיסטוריית האימונים שלהן).")

        if lesson.segments:
            st.divider()
            st.subheader("שלב ג׳: בקרת תסריט והפקת אודיו בקולך")
            ready = sum(1 for s in lesson.segments if s.audio_path and not s.audio_is_stale)
            st.progress(ready / len(lesson.segments), text=f"{ready}/{len(lesson.segments)} פסקאות עם אודיו עדכני")

            for seg in lesson.segments:
                render_segment_editor(seg)

            g1, g2 = st.columns(2)
            if g1.button("🎙️ הפק את כל הפסקאות החסרות/שעודכנו", type="primary"):
                todo = [s for s in lesson.segments if not s.audio_path or s.audio_is_stale]
                bar = st.progress(0.0)
                try:
                    for i, seg in enumerate(todo):
                        bar.progress(i / max(len(todo), 1), text=f"מפיק פסקה {seg.position} ({i + 1}/{len(todo)})…")
                        svc.synthesize_segment(seg.id, tts_choice)
                    bar.empty()
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    bar.empty()
                    show_error(exc)
            if g2.button("🧩 אחד לקובץ שיעור מלא (WAV + MP3 + כתוביות SRT)", disabled=ready == 0):
                try:
                    with st.spinner("מאחד…"):
                        master = svc.build_master(lesson.id)
                    st.session_state[f"master_{lesson.id}"] = master.model_dump()
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)

            if (master := st.session_state.get(f"master_{lesson.id}")) and Path(master["mp3_path"]).exists():
                st.success(f"השיעור המלא מוכן: {master['segment_count']} פסקאות · {master['duration_seconds'] / 60:.1f} דקות")
                st.audio(master["mp3_path"])
                d1, d2, d3 = st.columns(3)
                d1.download_button("⬇️ MP3", Path(master["mp3_path"]).read_bytes(), f"{lesson.title}.mp3", "audio/mpeg")
                d2.download_button("⬇️ WAV", Path(master["wav_path"]).read_bytes(), f"{lesson.title}.wav", "audio/wav")
                d3.download_button("⬇️ כתוביות SRT", Path(master["srt_path"]).read_bytes(), f"{lesson.title}.srt", "text/plain")

        with st.expander("⚙️ ניהול שיעור"):
            new_name = st.text_input("שינוי שם:", value=lesson.title, key=f"rename_{lesson.id}")
            r1, r2 = st.columns(2)
            if r1.button("שמור שם"):
                svc.rename_lesson(lesson.id, new_name)
                st.rerun()
            if r2.checkbox("אני בטוח שאני רוצה למחוק", key=f"confirm_del_{lesson.id}") and r2.button("🗑️ מחק שיעור"):
                svc.delete_lesson(lesson.id)
                st.session_state.lesson_id = None
                st.rerun()


# ======================================================================== tab 2: accent lab
def render_report(attempt: AttemptOut) -> None:
    r = attempt.report
    m1, m2, m3 = st.columns(3)
    m1.metric("ציון בהירות הגייה", f"{attempt.clarity_score:.0f}%")
    m2.metric("קצב (מילים לדקה)", f"{attempt.wpm:.0f}")
    m3.metric("WER גולמי", f"{attempt.strict_wer:.2f}")
    st.caption(r.get("pacing_feedback", ""))
    st.markdown("**מה ש-Whisper שמע בפועל:**")
    st.markdown(f'<div class="ltr"><em>“{html.escape(attempt.recognized_text)}”</em></div>', unsafe_allow_html=True)
    st.markdown("**השוואה מילה-מילה** (ירוק = מדויק · צהוב = קרוב · אדום = שגוי · מחוק = נבלע · אפור = מילה עודפת):")
    st.markdown(diff_html(r.get("visual_diff", [])), unsafe_allow_html=True)
    if r.get("mispronounced_words"):
        st.error("מילים לתרגול חוזר:")
        for target_w, heard_w in r["mispronounced_words"]:
            st.markdown(f"- **`{target_w}`** ← נשמע כ: `{heard_w or 'לא נשמע'}`")
    else:
        st.success("הגייה מצוינת — כל המילים זוהו!")
    if r.get("low_confidence_words"):
        st.warning("מילים שזוהו בביטחון אקוסטי נמוך (סימן למבטא כבד): " + ", ".join(f"`{w}`" for w in r["low_confidence_words"]))


with tab_lab:
    if lesson is None or not lesson.segments:
        st.info("כדי להתאמן, תרגם תחילה שיעור בלשונית האולפן.")
    else:
        st.header("🎧 מעבדת Shadowing מול קול ה-AI שלך")
        segs = {s.id: s for s in lesson.segments}
        seg_id = st.selectbox("פסקה לאימון:", list(segs),
                              format_func=lambda i: f"פסקה {segs[i].position} · {segs[i].english_display[:60]}…",
                              key=f"lab_seg_{lesson.id}")
        seg = segs[seg_id]

        st.subheader("1. מפת הטעמה וחיבורים")
        st.markdown(
            "מילים <span style='color:#d6336c;font-weight:800'>מודגשות</span> הן מילות התוכן שעליהן יושב המקצב · "
            "<span style='color:#1c7ed6;font-weight:700'>‿</span> חיבור רציף ללא עצירה · "
            "<span style='color:#e8590c;font-weight:700'>d</span> = Flap T (T רכה) · "
            "<span style='color:#868e96'><i>(tə)</i></span> = מילת תפקוד מוחלשת",
            unsafe_allow_html=True,
        )
        st.markdown(coach_guide_html(seg.shadowing_coach_guide or seg.english_display), unsafe_allow_html=True)
        with st.expander("הטקסט הנקי (טקסט היעד לבדיקה)"):
            st.markdown(f'<div class="ltr">{html.escape(seg.english_display)}</div>', unsafe_allow_html=True)

        st.subheader("2. האזנה לקול היעד")
        if seg.audio_path:
            rate = st.radio("מהירות:", [0.75, 0.9, 1.0], index=2, horizontal=True,
                            format_func=lambda r: f"{r}x", key=f"rate_{seg.id}")
            try:
                st.audio(str(svc.segment_audio(seg.id, rate)))
            except Exception as exc:  # noqa: BLE001
                show_error(exc)
        else:
            st.warning("טרם הופק אודיו לפסקה זו — הפק אותו בלשונית האולפן.")

        st.subheader("3. הקלט את עצמך ובדוק")
        take_key = f"take_{seg.id}"
        st.session_state.setdefault(take_key, 0)
        attempt_audio = st.audio_input("הקלט את הניסיון שלך באנגלית:", key=f"practice_{seg.id}_{st.session_state[take_key]}")
        if attempt_audio is not None and st.button("🔍 נתח הגייה ומבטא", type="primary"):
            with st.spinner("Whisper Large מאזין להגייה שלך…"):
                try:
                    svc.evaluate_practice(seg.id, attempt_audio.getvalue(), ".wav")
                    st.session_state[take_key] += 1  # fresh recorder for the next take
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    show_error(exc)

        history = svc.list_attempts(seg.id)
        if history:
            st.divider()
            st.subheader("תוצאת הניסיון האחרון")
            render_report(history[-1])
            if (last_audio := abs_path(history[-1].audio_path)) is not None:
                st.audio(str(last_audio))
            if len(history) > 1:
                st.subheader("📈 התקדמות בפסקה זו")
                df = pd.DataFrame({
                    "ניסיון": range(1, len(history) + 1),
                    "בהירות %": [a.clarity_score for a in history],
                    "WPM": [a.wpm for a in history],
                }).set_index("ניסיון")
                st.line_chart(df[["בהירות %"]])
                st.dataframe(df, width="stretch")


# ======================================================================== tab 3: lexicon
with tab_lexicon:
    st.header("📖 מילון ההגייה ההלכתי-פונטי")
    st.markdown("כל מונח כאן מוחלף אוטומטית בכתיב הפונטי שלו לפני שמנוע הקול מקריא, וגם מוזן למודל השפה כהנחיה.")

    entries = svc.lexicon.entries()
    df = pd.DataFrame(sorted(entries.items(), key=lambda kv: kv[0].lower()), columns=["term", "phonetic"])
    edited = st.data_editor(
        df, num_rows="dynamic", width="stretch", hide_index=True, key="lexicon_editor",
        column_config={
            "term": st.column_config.TextColumn("מונח בתעתיק (כפי שמופיע בתרגום)", required=True),
            "phonetic": st.column_config.TextColumn("כתיב פונטי למנוע הקול", required=True),
        },
    )
    if st.button("💾 שמור מילון", type="primary"):
        rows = edited.dropna()
        svc.lexicon.replace_all(dict(zip(rows["term"].astype(str), rows["phonetic"].astype(str))))
        st.success(f"נשמרו {len(svc.lexicon.entries())} מונחים.")

    st.divider()
    st.subheader("🧪 בדיקת הגייה")
    sample = st.text_input("משפט לבדיקה:", value="According to the Rambam, this Melacha is forbidden on Shabbat.", key="en_lex_sample")
    st.markdown(f'<div class="ltr"><code>{html.escape(svc.lexicon.apply(sample))}</code></div>', unsafe_allow_html=True)
    p1, p2 = st.columns([3, 1])
    phonetic_try = p1.text_input("כתיב פונטי להשמעה (למשל Khah-khahm):", key="en_lex_try")
    if p2.button("▶️ השמע בקולך", disabled=not phonetic_try.strip()):
        with st.spinner("מייצר…"):
            try:
                st.audio(str(svc.preview_pronunciation(phonetic_try.strip(), tts_choice)))
            except Exception as exc:  # noqa: BLE001
                show_error(exc)
