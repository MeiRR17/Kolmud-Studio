# Kolmud Studio — TorahVoice Studio & Accent Lab

Local-first app that turns a recorded Hebrew Torah/Halacha shiur into an English shiur in **your own cloned voice**, and doubles as a **shadowing lab** for training a General American accent against that voice.

```
Hebrew audio ──► mlx-whisper (ivrit-ai, Metal) ──► Hebrew transcript (editable)
            ──► LLM (Ollama local / OpenAI-compatible / Claude)
                  ├─ english_display         – academic/Orthodox English (subtitles, reading)
                  ├─ hebrew_terms            – Hebrew/Aramaic terms found + phonetic respellings
                  └─ shadowing_coach_guide   – stress / linking / flap-T / reductions (validated)
            ──► english_tts_phonetic = display text + lexicon + hebrew_terms (deterministic)
            ──► XTTS-v2 voice clone (MPS → CPU fallback) or ElevenLabs ──► per-paragraph WAV
            ──► master WAV + MP3 + SRT subtitles

Practice: you record a paragraph ──► English Whisper ──► clarity %, WER, WPM, word-level diff, history
```

Everything except the optional API providers runs offline on the Mac.

## Setup (Apple Silicon, Python 3.12)

```bash
brew install ffmpeg ollama          # if not installed
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                # then edit — see below
```

**Required decision — XTTS-v2 license.** The XTTS-v2 weights are under the [Coqui Public Model License](https://coqui.ai/cpml), which allows **non-commercial use only**. If you accept it, set `XTTS_ACCEPT_CPML=true` in `.env`; the ~1.8 GB model then downloads on first synthesis. If you need commercial use, use the ElevenLabs engine instead.

Models download automatically on first use (Hugging Face cache):
- Hebrew ASR: `mlx-community/ivrit-ai-whisper-large-v3-turbo-mlx` (~1.6 GB) — ivrit-ai's Hebrew fine-tune, MLX format.
- English ASR (practice scoring): `mlx-community/whisper-large-v3-mlx` (~3 GB).

If a Hugging Face download hangs at 0%, run with `HF_HUB_DISABLE_XET=1`. Once downloaded, models are loaded from the local cache, so transcription works offline.

## Run

```bash
streamlit run app.py                       # the UI  →  http://localhost:8501
uvicorn api.main:app --port 8000           # optional REST API  →  http://localhost:8000/docs
pytest                                     # tests (no models needed — engines are faked)
```

Both share the same SQLite database (`data/kolmud.db`) and lesson folders (`data/projects/lesson_<id>/`).

## Workflow

1. **Sidebar → reference voice.** Upload or record 15–30 s of clean speech. Read it **in English with your best American accent** — zero-shot cloning copies pronunciation habits as well as timbre, so an English reference gives a much cleaner accent than a Hebrew one.
2. **Sidebar → new lesson.**
3. **Studio tab.** Upload the Hebrew recording → transcribe → fix the transcript → translate. Long shiurim are split into ~1,400-character chunks (with the previous paragraph passed as continuity context), so local models with limited context work.
4. Review each paragraph: edits save automatically. If you edit the display text, **🔁 rebuild the phonetic script** from it + the lexicon. Paragraphs whose TTS script changed after their audio was generated are marked ⚠️.
5. Generate audio per paragraph or all at once → **merge** into WAV/MP3 + SRT.
6. **Lab tab.** Read the colour-coded guide (stress, ‿ linking, flap-T, reductions), listen at 0.75× / 0.9× / 1×, record yourself, get a score and a word-level diff. Every attempt is stored, with a progress chart per paragraph.
7. **Lexicon tab.** Add/edit terms (`Chacham → Khah-khahm`), and audition a respelling in your cloned voice.

## Why the TTS script isn't written by the LLM

Tested on this Mac with gemma4:26b, qwen3.5:27b and DictaLM 3.0 12B: when asked to produce the phonetic script, every model also respelled ordinary English words ("fun-dah-men-tal", "Ah-cord-ing too"), which XTTS then reads badly. So the LLM only *lists* the Hebrew terms it used, and the script is built deterministically — the lexicon always wins, LLM respellings fill gaps, English stays untouched. Glosses in parentheses become comma pauses. New terms the LLM found can be added to the lexicon from each paragraph with one click.

Similarly, a shadowing guide that changes the words or is written in all caps is replaced by a rule-based one (linking ‿ and flap-T only).

In that test gemma4:26b gave the most faithful translation (it applied the terminology rules) at ~45 s per 1,400-character chunk, i.e. roughly 30 minutes for an hour-long shiur.

## How scoring works

- **Clarity %** — share of target words that English Whisper recognised, counting near misses (spelling-level differences such as `machlokes`/`machloket`, or linked speech like `borer on` → `boron`) as recognised. Hebrew/Aramaic lexicon terms get a looser similarity bar, since they have no single English spelling.
- **WER** — the strict jiwer word error rate, for comparison.
- **WPM** — measured from the first to the last spoken word (ignores silence at the start and end of the recording). Target for a shiur: 110–165.
- **Low-confidence words** — words Whisper recognised with probability < 0.75: usually the ones carrying a heavy accent.

Whisper is a proxy for an American listener, not a phonetician: a high score means "intelligible to an English ear", not "native-like".

## Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama`, `openai` (any OpenAI-compatible URL, incl. Gemini), `anthropic` |
| `OLLAMA_MODEL` | `gemma4:26b` | any installed model can be picked in the sidebar |
| `OLLAMA_NUM_CTX` | `16384` | context window requested from Ollama |
| `TRANSLATION_CHUNK_CHARS` | `1400` | lower it if a local model drops content |
| `HEBREW_ASR_CONTEXT_PROMPT` | `false` | prime Whisper with Torah vocabulary; off because ivrit-ai spells the terms correctly without it, and with it the first word was dropped in testing |
| `TTS_ENGINE` | `xtts` | or `elevenlabs` (needs API key + cloned voice ID) |
| `TTS_DEVICE` | `auto` | Metal with automatic CPU fallback; set `cpu` if MPS output sounds wrong |
| `XTTS_ACCEPT_CPML` | `false` | see the license note above |
| `XTTS_TEMPERATURE` / `XTTS_SPEED` | `0.65` / `1.0` | lower temperature = steadier delivery |

## Project layout

```
app.py                     Streamlit UI (studio, accent lab, lexicon)
api/main.py                FastAPI server over the same service layer
core/config.py             settings (.env) + hardware detection
core/database.py           SQLite models: Lesson, Segment, PracticeAttempt
core/pipeline.py           StudioService — the whole workflow
core/asr_whisper.py        mlx-whisper: Hebrew lecture + English practice
core/llm_translator.py     chunked translation, providers, JSON-schema output
core/lexicon.py            phonetic lexicon (deterministic final pass)
core/tts_cloner.py         XTTS-v2 (cached speaker latents, MPS→CPU) / ElevenLabs
core/accent_evaluator.py   clarity / WER / WPM / word alignment
core/audio_utils.py        ffmpeg conversion, tempo variants, merge, SRT
prompts/halachic_translator.txt   the translation + phonetics system prompt
data/lexicon.json          seed lexicon (everything else in data/ is git-ignored)
```
