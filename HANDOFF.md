# Kolmud Studio: continuation notes

Last reviewed: 2026-10-27.

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
3. Improve export completeness checks: `StudioService.build_master` currently
   skips paragraphs without existing audio files and does not reject stale audio.
   Make incomplete or stale exports explicit to the user.
4. Review edit propagation: changing display text currently requires manually
   rebuilding the phonetic script before regenerating audio. Review consistency
   of coaching text, subtitles, and previously generated master files as well.
5. Address issues found in the pilot before processing full-length lessons.

## Continuing on another computer

Clone `https://github.com/MeiRR17/Kolmud-Studio.git` and open the cloned folder in
Codex. Ask the new chat to read `README.md` and `HANDOFF.md` before continuing.
This file carries project context; it is not a copy of the original chat.

The repository intentionally excludes `.env`, the virtual environment, recordings,
voice profiles, lesson data, and the SQLite database. Only `data/lexicon.json` is
included from the data directory. Configure credentials locally and transfer any
personal recordings or voice samples separately if needed.
