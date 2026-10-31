# Kolmud Studio v0.2.0 — TorahVoice Studio & Accent Lab

[![Release v0.2.0](https://img.shields.io/badge/release-v0.2.0-blue)](https://github.com/MeiRR17/Kolmud-Studio/releases/tag/v0.2.0)

**Status: alpha, piloted end to end with real local models** (see [Pilot results](#pilot-results)).
See the [changelog](CHANGELOG.md) for features, validation, and known limitations.
The application version is defined in `core/__init__.py` and shared by the UI,
API documentation, and `/health` response.

Local-first app that turns a recorded Hebrew Torah/Halacha shiur into an English shiur in **your own cloned voice**, and doubles as a **shadowing lab** for training a General American accent against that voice.

```
Hebrew audio ──► mlx-whisper (ivrit-ai, Metal) ──► Hebrew transcript (editable)
            ──► LLM (Ollama local / OpenAI-compatible / Claude)
                  ├─ english_display         – academic/Orthodox English (subtitles, reading)
                  ├─ hebrew_terms            – Hebrew/Aramaic terms found + phonetic respellings
            ──► shadowing_coach_guide    – stress / linking / flap-T, rule-based (LLM version optional)
            ──► english_tts_phonetic = display text + lexicon + hebrew_terms (deterministic)
            ──► XTTS-v2 voice clone (MPS → CPU fallback) or ElevenLabs ──► per-paragraph WAV
            ──► master WAV + MP3 + SRT subtitles

Practice: you record a paragraph ──► English Whisper ──► clarity %, WER, WPM, word-level diff, history
```

Everything except the optional API providers runs offline on the Mac.

## Terminal jobs and review handoff

Since v0.2.0 a local CLI and a file-based Dvir OS review handoff are included. See
[the integration contract](docs/integrations/dvir-os.md) for acceptance criteria,
recovery behavior, model policy, and remaining integration work.

After installing the existing requirements, install the entry point from this checkout:

```bash
source .venv/bin/activate
pip install -e . --no-deps --no-build-isolation
kolmud --version
kolmud doctor
kolmud run /path/to/lesson.m4a --title "My lesson"                 # transcription only
kolmud resume JOB_ID --until translation                          # local English translation
kolmud resume JOB_ID --until master                               # explicitly request cloned speech + exports
kolmud status JOB_ID
kolmud jobs
```

`python -m kolmud` is an equivalent entry point. Commands emit JSON and return a
nonzero exit status on failure. Use the same `--data-dir /absolute/path` **before**
the command on every invocation if storing data outside the checkout. The default
uses the same configured data directory as the UI and API. Installation above is
from a source checkout, not a standalone application bundle.

The CLI permits **local Ollama and XTTS only**; cloud providers configured for the
interactive UI are refused by jobs before processing begins. `--until master`
also requires an existing reference voice and prior XTTS license acceptance.
Models may need a first-time download. No background service is installed.

Jobs persist under `data/jobs/`. Every completed/failed attempt writes
`data/jobs/JOB_ID/manifest.json` and `agent-run.json`, plus a text snapshot of the
lesson. Import **agent-run.json** manually in Dvir OS's agent review screen.
`NEEDS_HUMAN` means the requested processing finished and needs content review;
it does not mean the lesson is approved or published. Local file paths and hashes
are references, not automatically uploaded media or clickable web downloads.

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
4. Review each paragraph: edits save automatically. On the development branch,
   editing display text rebuilds the phonetic script and coaching guide automatically;
   explicit script/guide overrides are retained. Paragraphs whose TTS script changed
   after their audio was generated are marked ⚠️. In v0.1.0, use **🔁 rebuild the phonetic script** manually.
5. Generate audio per paragraph or all at once → **merge** into WAV/MP3 + SRT.
   On the development branch, export/download requires every paragraph's current
   audio and a translation of the current transcript. Edits invalidate previous
   masters; old files are retained but not served as current output.
6. **Lab tab.** Read the colour-coded guide (stress, ‿ linking, flap-T, reductions), listen at 0.75× / 0.9× / 1×, record yourself, get a score and a word-level diff. Every attempt is stored, with a progress chart per paragraph.
7. **Lexicon tab.** Add/edit terms (`Chacham → Khah-khahm`), and audition a respelling in your cloned voice.

## Why the TTS script isn't written by the LLM

Tested on this Mac with gemma4:26b, qwen3.5:27b and DictaLM 3.0 12B: when asked to produce the phonetic script, every model also respelled ordinary English words ("fun-dah-men-tal", "Ah-cord-ing too"), which XTTS then reads badly. So the LLM only *lists* the Hebrew terms it used, and the script is built deterministically — the lexicon always wins, LLM respellings fill gaps, English stays untouched. Glosses in parentheses become comma pauses. New terms the LLM found can be added to the lexicon from each paragraph with one click.

The shadowing guide is likewise built by rules by default (stress on content words, ‿ linking, flap-T). In the pilot the LLM's guides were indistinguishable from the rule-based ones while costing ~40% of all generated tokens; set `TRANSLATION_LLM_COACH_GUIDE=true` to ask the LLM anyway (a guide that changes the words or is all caps is still replaced).

## Pilot results

Run on 2026-10-31 on the M1 Max (64 GB) with `kolmud run … --until master`: a 2:34 Hebrew shiur on Melechet Borer (352 words, read by the macOS Hebrew voice), translated by gemma4:26b and voiced with XTTS-v2 from a 16-second English reference voice (also synthetic — no personal recording was used).

| | Pilot 1 (v0.1 branch) | Pilot 2 (v0.2.0) |
|---|---|---|
| Hebrew ASR | 43 s · WER 7.8%, **one halachic condition dropped** | 43 s · WER 5.8%, nothing dropped |
| Translation | 18.2 min | **6.7 min** |
| Voice (≈4 min of audio) | 11.3 min | 10.2 min (Metal) |
| Total | 30.4 min | **17.7 min** |

What pilot 1 found, and the fix in v0.2.0:
- **Dropped speech at 30 s window boundaries** (a whole clause at exactly 30.0 s, in every Whisper configuration) → audio is now transcribed in pause-aligned chunks of ≤ 25 s.
- **"לאלתר" translated as "L'chatchila"**, "דף עד" as "page 70", and an invented "[the second condition is]" → explicit terminology, Hebrew-numeral and fidelity rules in the prompt. Pilot 2: "immediately", "page 74", no brackets, and "שולחן ארוך" from the ASR corrected to Shulchan Aruch.
- **XTTS 2.5× slower while gemma4 stayed resident in GPU memory** (server keep-alive: forever) → the Ollama model is unloaded after translation.
- **~40% of LLM output spent on the shadowing guide** → rule-based guide by default.

Quality of the result: English Whisper transcribed the generated 607-word master at 7.1% WER (98% clarity counting near-spellings). Most Hebrew terms were heard as recognisable spellings (melaka, shulkan aruk, akharonim, hala-kik); the weakest respellings were tuned with an A/B test (see the lexicon). Remaining translation errors trace back to misheard words in the synthetic recording ("עכור" heard as "החור" → "dark liquid") — which is why every job ends in `NEEDS_HUMAN`.

Timing caveat: during both pilots macOS background services (photo analysis, Google Drive sync, XProtect) kept the CPU/GPU busy; LLM speed was 5–11 tokens/s versus ~45 tokens/s measured on a quiet machine earlier, so expect faster runs on an idle Mac. A real recording of a human teacher, and your own reference voice, have not been tested yet.

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
| `TRANSLATION_LLM_COACH_GUIDE` | `false` | also ask the LLM for the shadowing guide (~40% more output tokens) |
| `OLLAMA_UNLOAD_AFTER_TRANSLATION` | `true` | free GPU memory for XTTS once translation is done |
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
core/jobs.py               checkpointed CLI jobs + Dvir OS manifest / agent-run handoff
core/runtime.py            cross-process capacity locks
core/prosody.py            rule-based shadowing guide + guide validation
kolmud/cli.py              `kolmud` terminal entry point
core/asr_whisper.py        mlx-whisper in pause-aligned ≤25 s chunks: Hebrew lecture + English practice
core/llm_translator.py     chunked translation, providers, JSON-schema output
core/lexicon.py            phonetic lexicon (deterministic final pass)
core/tts_cloner.py         XTTS-v2 (cached speaker latents, MPS→CPU) / ElevenLabs
core/accent_evaluator.py   clarity / WER / WPM / word alignment
core/audio_utils.py        ffmpeg conversion, tempo variants, merge, SRT
prompts/halachic_translator.txt   the translation + Hebrew-terms system prompt
prompts/shadowing_coach_guide.txt optional LLM shadowing-guide instructions
data/lexicon.json          seed lexicon (everything else in data/ is git-ignored)
```
