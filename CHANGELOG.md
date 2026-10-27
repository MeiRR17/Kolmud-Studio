# Changelog

## Unreleased — local executor and Dvir OS handoff

- Installable `kolmud` command (`run`, `resume`, `status`, `jobs`, `doctor`, `--version`)
  sharing the service layer with Streamlit and FastAPI.
- Persisted single-host jobs, atomic private checkpoints, resume of completed
  stages/paragraphs, source hashes, and model/voice provenance.
- Explicit transcript/translation/audio/master targets. Transcription is the
  default; cloud inference providers are refused by the CLI.
- File-based content manifest and agent-run v1 review report for Dvir OS;
  text/media references and hashes, failed checks and human review requirements.
- Capacity guards across Kolmud UI/API/CLI using the same data directory.
- Complete/current export checks, checksum verification on download, immutable
  export generations, and invalidation after edits or regenerated audio.
- Automatic phonetic/coaching rebuild after English display edits.
- Additive SQLite upgrade preserving legacy lessons. Legacy translations need
  explicit retranslation before synthesis/export because their provenance is unknown.
- Automated restart, integrity, policy, migration and CLI coverage. Model-quality
  pilot and live Dvir OS orchestration are still outstanding.
- GitHub Actions workflow runs the model-free tests and checks the installed CLI
  without downloading model weights or using inference credentials.

## [0.1.0](https://github.com/MeiRR17/Kolmud-Studio/releases/tag/v0.1.0) — 2026-10-27

Initial alpha release of Kolmud Studio for local use on Apple Silicon with Python 3.12.

### Features

- Streamlit studio for Hebrew Torah/Halacha lesson transcription, editable English
  translation, cloned voice synthesis, and WAV/MP3/SRT export.
- Local Hebrew and English transcription with MLX Whisper; translation through
  Ollama or optional OpenAI-compatible and Anthropic providers.
- Voice synthesis through local XTTS-v2 or optional ElevenLabs.
- Deterministic pronunciation lexicon for Hebrew/Aramaic terms.
- Shadowing practice lab with intelligibility scoring, word comparisons, pacing,
  and saved practice history.
- SQLite lesson storage and a FastAPI interface over the shared workflow.
- Shared application version shown in the UI, browser tab title, API documentation,
  and health response.

### Validation

- All 20 automated tests pass on the development Mac. Model engines are faked in
  the tests; these checks do not establish real transcription, translation, or
  voice quality.
- A real-model end-to-end lesson pilot remains outstanding.

### Known limitations

- Installation and startup are manual; a packaged CLI, background job manager,
  and automatic resume after interruption are not yet available.
- Master export can skip missing audio and does not reject stale audio.
- Editing display text requires manually rebuilding the phonetic script before
  regenerating speech.
- Practice scores estimate intelligibility, not native American accent quality.
- The local model stack targets Apple Silicon. Follow the README for setup,
  model downloads, and provider configuration.
- XTTS-v2 model weights require acceptance of their non-commercial CPML license;
  see the README for details.
