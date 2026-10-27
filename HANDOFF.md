# Kolmud Studio: continuation notes

Last reviewed: 2026-10-27.

Current application version: **0.1.0 (initial alpha)**. The version is defined in
`core/__init__.py` and displayed in the Streamlit sidebar, browser tab title,
API documentation, and `/health`. See `CHANGELOG.md` for release details.

Development branch `codex/dvir-local-pipeline` adds checkpointed CLI jobs and
a file-based Dvir OS agent-review handoff. It also fixes export consistency and
automatic edit propagation. The published v0.1.0 tag is unchanged. See
`docs/integrations/dvir-os.md` and the Unreleased changelog before continuing.

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

During the review on 2026-10-27, `.venv/bin/python -m pytest -q` passed all 20
tests on the original Mac. The tests substitute fake ASR, translation, and TTS
engines, so they validate workflow behavior rather than real model quality.
No real-model end-to-end pilot was run during this review.

The practice score measures recognition/intelligibility, not a complete measure
of an American accent. See `README.md` for setup and usage.

## Recommended next work

1. Set up the application on the destination computer using `README.md`.
   The current local model stack targets Apple Silicon and Python 3.12.
2. Run a 2–5 minute Hebrew lesson through transcription, translation, cloned
   speech, and export. Review translation fidelity, Torah terminology, voice
   similarity, pronunciation, and processing time.
3. Review/test the development branch before release. It rejects incomplete/stale
   exports and downloads, automatically rebuilds phonetics/coaching after English
   edits, and tracks transcript-to-translation provenance. Before opening an
   existing production database on this branch, take a SQLite backup: two nullable
   columns are added automatically. Legacy lessons are retained but translations
   need explicit retranslation before synthesis/export.
4. Import a locally produced `agent-run.json` in Dvir OS for a real user trial.
   File/contract validation is not a deployed bridge, scheduler or media uploader.
5. Address issues found in the pilot before processing full-length lessons.

## Continuing on another computer

Clone `https://github.com/MeiRR17/Kolmud-Studio.git` and open the cloned folder in
Codex. Ask the new chat to read `README.md` and `HANDOFF.md` before continuing.
This file carries project context; it is not a copy of the original chat.

The repository intentionally excludes `.env`, the virtual environment, recordings,
voice profiles, lesson data, and the SQLite database. Only `data/lexicon.json` is
included from the data directory. Configure credentials locally and transfer any
personal recordings or voice samples separately if needed.
