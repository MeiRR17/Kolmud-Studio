# Kolmud Studio: continuation notes

Last reviewed: 2026-10-31.

Current application version: **0.2.0 (alpha)**. The version is defined in
`core/__init__.py` and displayed in the Streamlit sidebar, browser tab title,
API documentation, and `/health`. See `CHANGELOG.md` for release details.

v0.2.0 merges the former development branch `codex/dvir-local-pipeline`
(checkpointed CLI jobs, file-based Dvir OS agent-review handoff, export
consistency, automatic edit propagation) plus the fixes from the first
real-model pilot. See `docs/integrations/dvir-os.md` for the CLI contract.

## Current implementation

This local-first Python application turns recorded Hebrew Torah/Halacha lessons
into English audio using the speaker's cloned voice. It also provides a shadowing
lab for English speaking practice.

- Streamlit UI: `app.py` (studio, practice lab, pronunciation lexicon).
- Optional FastAPI interface: `api/main.py`.
- Shared workflow: `core/pipeline.py`.
- Local Hebrew/English transcription with MLX Whisper; translation through Ollama
  or optional external providers; voice synthesis through XTTS or ElevenLabs.
- SQLite stores lessons, paragraphs, and practice history.
- Exports: WAV, MP3, and SRT subtitles.
- Hebrew/Aramaic pronunciation is applied through a deterministic lexicon pass;
  ordinary English words are kept intact.

## Verification and limits

On 2026-10-31 `.venv/bin/python -m pytest -q` passed all 54 tests on the
original Mac (fake ASR/translation/TTS engines: workflow behavior, not model
quality), and GitHub Actions runs the same suite.

A real-model pilot was run twice with `kolmud run … --until master` on the M1 Max:
a 2:34 Hebrew shiur on Melechet Borer read by the macOS Hebrew voice, gemma4:26b,
XTTS-v2 with a synthetic 16 s English reference voice. Results and the fixes they
led to are in the README ("Pilot results"). In short: v0.2.0 transcribes without
dropping speech (WER 5.8%), translates the terminology correctly, and produced a
master that English Whisper transcribed at 7.1% WER. The completed and the failed
`agent-run.json` were both accepted by Dvir OS's own `parseRun` (lib/review.ts).
Timings were inflated by macOS background services during the pilot.

Not yet tested: a real human teacher's recording, the user's own reference voice,
a full-length (≈1 h) lesson, and an actual import by the user in the Dvir OS UI.

The practice score measures recognition/intelligibility, not a complete measure
of an American accent. See `README.md` for setup and usage.

## Recommended next work

1. Record the reference voice (15–30 s, in English, best American accent) in the
   Streamlit sidebar, or at `data/voice_profiles/my_reference_voice.wav`.
2. Run a real 2–5 minute recorded shiur by a human teacher end to end and review
   translation fidelity, voice similarity and pronunciation by ear. Tune weak
   respellings in the lexicon tab (▶️ plays a term in the cloned voice).
3. Then try a full-length lesson; check total time on an idle Mac
   (the pilot's ~17 min for 2.5 min of audio was measured under heavy background load).
4. Import a produced `agent-run.json` in the Dvir OS review screen (user trial).
   File/contract validation is not a deployed bridge, scheduler or media uploader.
5. Before opening a v0.1.0 database with v0.2.0, take a SQLite backup: two nullable
   columns are added automatically, and legacy translations need explicit
   retranslation before synthesis/export.

## Continuing on another computer

Clone `https://github.com/MeiRR17/Kolmud-Studio.git` and open the cloned folder in
Codex. Ask the new chat to read `README.md` and `HANDOFF.md` before continuing.
This file carries project context; it is not a copy of the original chat.

The repository intentionally excludes `.env`, the virtual environment, recordings,
voice profiles, lesson data, and the SQLite database. Only `data/lexicon.json` is
included from the data directory. Configure credentials locally and transfer any
personal recordings or voice samples separately if needed.
