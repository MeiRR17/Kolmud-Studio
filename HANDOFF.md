# Kolmud Studio: continuation notes

Last reviewed: 2026-11-20.

Current application version: **0.2.0 (alpha)**. The version is defined in
`core/__init__.py` and displayed in the Streamlit sidebar, browser tab title,
API documentation, and `/health`. See `CHANGELOG.md` for release details.
The latest actually published GitHub Release is **v0.1.0**; a v0.2.0 Release
does not exist. Current source-timed/YouTube work is unreleased. Do not infer
publication from a local tag or application version.

The full agreed product is **not finished**. Read [the Hebrew completion checklist](docs/PROJECT_STATUS_HE.md)
before continuing. Keep the current Dvir OS planning/implementation tracker linked
to the private repository's issue 36; do not modify its generated Drive mirrors or
another chat's active checkout.

Owner clarification: the video itself must be created automatically; don't request a
filmed source as a prerequisite. This is captured in issue 36. The local renderer makes
text slides. A pending optional owner question asks whether the final look should be
slides, illustration footage or a talking avatar. Don't claim avatar/face animation
support or silently enable paid providers.

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
- SourceCue generations preserve raw Whisper text/word evidence, source hashes,
  revisioned edits, review flags and exact segment anchors. Timed translation links
  each English paragraph to a specific cue, and bilingual source SRT needs no TTS.
- Per-paragraph English recording/upload; synthetic, recorded and mixed CLI modes.
  `--until synchronized` requires source-linked translation, composes bounded audio
  on the original timeline, and optionally muxes an unchanged video stream to MP4.
  Continuous-take forced alignment, lip-sync and background music mixing are absent.
- Explicit native YouTube caption preparation/review/delivery commands; Desktop
  OAuth/PKCE, private refresh tokens outside the repository, ownership/duration
  checks, insert/update, readback and partial delivery receipts. See
  [the operator guide](docs/integrations/youtube.md). No publishing HTTP route added.
- Automatic-video route: `run SOURCE --until video`, or `--input-kind text` for Hebrew
  scripts. Generates narration-paced 1080p text-slide MP4, poster, storyboard and SRT,
  with no filmed source or external project. Existing-audio lesson command `video ID`.
  Pillow is a direct dependency; local fonts/RTL shaping are checked, never downloaded.
- `youtube-video-prepare/approve/publish` creates a new **private** YouTube video from a
  reviewed generated MP4, with resumable upload, saved session/video ID and native English
  captions. Lost acknowledgements are queried, never blindly reinserted.

## Verification and limits

On 2026-11-20 `.venv/bin/python -m pytest -q` passed **134 tests** locally
(fake ASR/translation/TTS and Google HTTP transport; real ffmpeg). GitHub Actions
runs the same suite, with the real Dvir TypeScript parser test skipped if that
private adjacent checkout is absent. This verifies workflow behavior, not model
quality, live OAuth or real channel behavior. No Google account was connected
and no YouTube write was executed during this development.
An actual 13.648 s local demo was rendered with the macOS Samantha synthetic voice,
Hebrew title and two English paragraphs. ffprobe verified 410 frames, 1920×1080 video
covering the complete audio; PNG layout and an encoded video frame were visually
inspected. This is an illustrative demo, not the owner's voice or a real-model
translation pilot. Files stay under ignored `data/demos/automatic_video/`.

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

1. Implement real text-to-audio forced alignment for a continuous English take and
   inspectable cuts/retakes. Existing ASR segment timestamps and text wrapping must
   never be reported as forced word alignment.
2. Implement source-backed transcription/translation QA; confidence flags and a
   human `--reviewed` declaration are not an automatic logical verifier.
3. Build the authenticated Dvir OS lesson bridge/UI in an isolated checkout,
   respecting that repository's AGENTS.md and existing work. No live bridge,
   background worker, mobile media access or reboot recovery is installed.
4. With an owner-selected script or audio and own reference voice, run both voice
   paths and generated video delivery. No existing video is required. OAuth
   account selection and approval of actual content belong to the owner. Verify
   channel-specific behavior and readback. New generated videos upload privately;
   the optional filmed-source route still has no verified secondary-audio API route.
5. Validate long lessons, local installation, backups/restore, cancellation and
   reliable service restart. Stop SQLite writers before making a backup with the
   backup API plus asset folders. New nullable columns and SourceCue rows are added
   automatically; legacy translations still require explicit retranslation.

## Continuing on another computer

Clone `https://github.com/MeiRR17/Kolmud-Studio.git` and open the cloned folder in
Codex. Ask the new chat to read `README.md` and `HANDOFF.md` before continuing.
This file carries project context; it is not a copy of the original chat.

The repository intentionally excludes `.env`, the virtual environment, recordings,
voice profiles, lesson data, and the SQLite database. Only `data/lexicon.json` is
included from the data directory. Configure credentials locally and transfer any
personal recordings or voice samples separately if needed.
