# Changelog

## Unreleased

- Automatic original video from Hebrew audio or a UTF-8 Hebrew script: local 1080p
  RTL-aware text slides, English narration at its natural pace, poster, storyboard
  and SRT. CLI video target, studio panel and API; no filmed source video or prebuilt
  external project required. Script input skips ASR entirely.
- New generated-video YouTube route: reviewed private upload via checkpointed resumable
  chunks, server offset recovery, saved video ID, native English captions and remote
  ownership/privacy/title/content verification. Interrupted uploads never blindly start
  a second video. Live account behavior is still unverified.
- Original-source timeline: Whisper word evidence, retained transcription generations,
  versioned source-cue editing with audit history, confidence/timing review flags, and
  source-linked translation. Export bilingual anchored SRT without running TTS.
- User English recordings per paragraph, synthetic and mixed routes; source-timed WAV,
  MP3, SRT and optional original-video-stream MP4. Bounded tempo, exact silence gaps,
  no silent truncation, stale/concurrent-input rejection and output integrity checks.
- CLI synchronized job target, persistent voice modes and verified resume; recorded
  mode needs no TTS provider/license/reference. Dvir handoffs include current source
  captions and synchronized assets with accurate recorded/synthetic labels.
- Native YouTube caption connector with Desktop OAuth/PKCE, private refresh storage
  outside the checkout, owner/video checks, generation-bound review, upload/update,
  remote readback, partial delivery receipts and retry reconciliation. No live account
  was connected or modified during development; Google HTTP exchanges are simulated.
- Completion checklist and operator instructions distinguish implemented segment
  anchors from the remaining full-recording alignment, logical QA and Dvir UI bridge.
- Reference voice check on save (UI, API `PUT /voice-profile`, `kolmud doctor`): outer silence
  trimmed; silent takes or < 6 s of speech rejected without replacing the current voice;
  warnings for short, long, quiet, clipped or pause-heavy takes. The sidebar offers a ready
  ~25 s English text to read.
- Dvir OS contract test: every agent-run report (completed and failed) is checked against a
  mirror of `parseRun`; with a local Dvir OS checkout (`DVIR_OS_DIR`) the real parser runs too.
- CI: actions/checkout and actions/setup-python v7 (Node 24), runner pinned to ubuntu-24.04.

## 0.2.0 — source milestone, 2026-10-31

Source version validated by a real-model pilot (2:34 synthetic Hebrew shiur → master
export on the M1 Max). This entry does not represent a published v0.2.0 GitHub Release;
the latest published Release is v0.1.0. See "Pilot results" in the README for measurements.

### Fixed from the pilot

- Hebrew/English ASR no longer drops speech at Whisper's 30 s window boundaries: audio is
  transcribed in pause-aligned chunks of at most 25 s (pilot 1 lost a whole halachic condition).
- Translation prompt: "לאלתר" is "immediately" (never L'chatchila), Talmud folio numbers are
  Hebrew numerals (דף עד = 74), no invented editorial notes, ASR mishearings of known terms
  are corrected (שולחן ארוך → Shulchan Aruch).
- The Ollama model is unloaded after translation (XTTS ran 2.5x slower while it stayed in GPU memory).
- Shadowing guide is rule-based by default (`TRANSLATION_LLM_COACH_GUIDE=true` restores the LLM
  guide); saves ~40% of generated tokens. Translation time in the pilot: 18.2 → 6.7 min.
- Job handoff no longer fails a narrower (e.g. audio-only) job because of an outdated master left
  by an earlier run; it lists only verified master files.
- CLI shows engine warnings on stderr and records the synthesis device (`XTTS-v2 (mps)`), so a
  silent Metal→CPU fallback is visible. Undecodable recordings get an actionable error message.
- Lexicon respellings re-tuned by an XTTS + Whisper A/B test.

### Local executor and Dvir OS handoff

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
- Automated restart, integrity, policy, migration and CLI coverage. Pilot `agent-run.json`
  files (completed and failed) were accepted by Dvir OS's own `parseRun`; a live Dvir OS
  orchestration and a user import trial are still outstanding.
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
