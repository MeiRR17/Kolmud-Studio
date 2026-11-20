# Kolmud → Dvir OS: local executor contract (v1)

## Scope and acceptance criteria

This is the first integration slice, not a full content-factory implementation.
The unreleased source-timed extension and its remaining product requirements are
listed in [the completion checklist](../PROJECT_STATUS_HE.md). YouTube caption
delivery is a separate explicitly invoked CLI workflow ([guide](youtube.md));
`run`/`resume` still never publish or approve content.

1. One terminal command accepts an existing local recording and persists a job.
2. Transcription is the default; translation, cloned audio and master export are
   explicit targets. A requirement for authentic teacher speech remains a separate
   recording task and is never silently replaced by synthesis.
3. A failed/interrupted job can resume in a new process using its printed ID.
   Completed ASR/translation and current per-paragraph audio are reused.
4. Changed input audio or voice settings fail closed on resume. An edited
   transcript requires a current translation, and edited English refreshes the
   TTS/coaching script. Missing/stale paragraphs cannot become a complete export.
5. Outputs include inspectable paths, hashes and a Dvir OS agent-run v1 file.
6. The target consumer's `parseRun` accepts the report, including failure checks.
7. No credentials, cloud inference, Dvir OS state mutation, public execution route,
   publication or service installation is part of this slice.

## Interface

Install and run from the source checkout (README). The `kolmud` and
`python -m kolmud` interfaces are equivalent. All commands emit JSON; exit code
0 means the command succeeded, 1 means processing/configuration failed, and 2
means invalid command-line syntax. `NEEDS_HUMAN` is not content acceptance.
The job ID is printed to stderr before long-running work starts; model progress
also goes to stderr so stdout remains a JSON result for orchestration.

```bash
kolmud --data-dir /path/to/local/assets run /path/to/recording.wav --goal-id GOAL_ID
kolmud --data-dir /path/to/local/assets resume JOB_ID --until translation
kolmud --data-dir /path/to/local/assets resume JOB_ID --until master
kolmud --data-dir /path/to/local/assets run /path/to/video.mp4 --until translation --source-timed
kolmud --data-dir /path/to/local/assets recording SEGMENT_ID /path/to/english-paragraph.wav
kolmud --data-dir /path/to/local/assets resume JOB_ID --until synchronized --voice-mode recorded
```

Keep the same data directory across invocations and across UI/API/CLI when they
share lessons. Custom data storage gets a seed lexicon; its default reference voice
path is `voice_profiles/my_reference_voice.wav` inside that directory.
`kolmud doctor` checks hardware/prerequisites without starting inference and
without printing credentials. `kolmud --version` works without model imports.
`synchronized` selects source-timed translation and original-timeline exports instead
of the concatenated `master` target. `recorded` requires a current user recording for
every paragraph and never invokes TTS; `synthetic` uses XTTS; `mixed` preserves supplied
recordings and synthesizes missing/stale paragraphs. Missing recordings fail with
segment IDs and resume instructions. Choose source-timed translation before recording
English, because replacing an unlinked translation replaces its paragraph rows.

The caller must inspect the exit status and job status, not parse an optimistic
summary string. A FAILED job records the stage, error class, safe diagnostic,
attempt count and a review file with a failed check. Raw model error bodies are
excluded. A process killed before it can record failure may retain RUNNING; with
no live worker lock it can be resumed explicitly. There is no automatic watchdog.

## Files

Under the configured data directory:

| Path | Purpose |
| --- | --- |
| `jobs/JOB_ID.json` | Private atomic checkpoint, lesson ID, attempts, target, models and stage history |
| `jobs/JOB_ID/manifest.json` | schemaVersion 1, producer/version, input hash, requested stage, status, goals, asset hashes, checks and review gaps |
| `jobs/JOB_ID/lesson.json` | Snapshot of transcript/paragraphs and local relative lesson asset paths |
| `jobs/JOB_ID/transcript.he.txt` | Current Hebrew transcript, when available |
| `jobs/JOB_ID/translation.en.txt` | Current English translation, when available |
| `jobs/JOB_ID/agent-run.json` | Import into Dvir OS's agent-review screen |
| `projects/lesson_ID/source/GENERATION/` | Retained original recording and normalized WAV |
| `projects/lesson_ID/segments/` | Separately generated paragraph WAV files |
| `projects/lesson_ID/master/GENERATION/` | Complete WAV/MP3/SRT generation |
| `projects/lesson_ID/source_captions/GENERATION/` | Source-anchored Hebrew/English SRT, evidence manifest, optional explicit YouTube review/approval/receipt |
| `projects/lesson_ID/synchronized/GENERATION/` | Source-timed English WAV/MP3/SRT, optional MP4 and input/output fingerprint manifest |

The handoff snapshot is updated on a retry of the same job; media generations are
retained. Hashes let the consumer detect later file changes. They do not prove
linguistic correctness, rights ownership or source support. No media is embedded
in the report or automatically uploaded. File paths are plain text references;
the present Dvir OS importer accepts only HTTP(S) URLs, so do not create `file://`
or unauthenticated local-filesystem web endpoints to make them clickable.

Required agent-run fields: `title`, `kind: content`, `agent.name`, `input`,
`summary`. Also supplied: stable `id`, timestamps, `goalIds`, bounded `permissions`,
`steps`, `artifacts`, source-hash `evidence`, `checks`, `confidence: low`,
`openQuestions` and `rollback`. Reports do not set an owner approval. Human checks
are `skipped`, not fabricated passes. A failed processing attempt has a `fail`
check. Schema compatibility is separate from production sync/mobile verification.

## Models and host capacity

Reuse the configured local models; no new model is chosen merely for the bridge.
Model selection must be tested on the target Hebrew/Torah material. Jobs block
OpenAI-compatible, Anthropic and non-loopback Ollama translation. Synthetic/mixed
jobs also block ElevenLabs; recorded jobs invoke no voice provider, even if the
interactive UI is configured for ElevenLabs. First-time model downloads can still require internet;
"local inference" does not imply an empty machine is already offline-ready.

`.jobs.lock` limits CLI jobs to one per shared data directory. `.inference.lock`
limits heavy Kolmud operations across its UI/API/CLI using that directory.
Locks are released by the OS on process exit. A second operation fails with a
busy error instead of launching a competing model. This does **not** coordinate
with Dvir OS research jobs or different Kolmud data directories; the future
control-plane scheduler must manage the shared machine's total capacity.

Changing a CLI job's voice profile/settings after audio has begun is refused to
avoid mixing voices on resume. Switching translation/TTS models in the interactive
UI does not automatically regenerate existing paragraphs; explicitly regenerate
them when changing those choices. Export freshness checks validate current text
and file bytes, not the semantic equivalence or quality of different models.

## Data upgrade and rollback

Nullable lesson columns include `translation_source_text`, `master_signature`,
`timeline_revision` and `source_asset`; segment columns include `source_cue_id` and
`audio_origin`. A new `source_cues` table preserves timeline generations and edit
history. Existing lessons/history/files are preserved. Legacy translations cannot be certified against their source and
therefore require explicit retranslation before synthesis/export. Existing
master files remain on disk; only verified current generations are downloaded.

Before using this branch on valuable data, stop writers and take a SQLite backup
with the SQLite backup API/tool, plus the lesson asset folders; do not copy only
the live `.db` while WAL writers run. Roll back to the reviewed code and restore
the separately tested snapshot if needed. Do not delete retained asset generations
as an automatic cleanup step.

## Remaining work

- Done in v0.2.0 with synthetic speech (see README "Pilot results"): recording →
  transcript → translation → cloned voice → audible full export, with timings, and
  both the completed and the failed `agent-run.json` accepted by Dvir OS's own
  `parseRun`. Still needed: the same with a human teacher's recording, the user's own
  reference voice and a human quality review.
- Real user import/review in the Dvir OS UI; no live bridge is installed and
  cross-device sync is not validated.
- Authenticated coordinator invoking this CLI, shared capacity scheduling,
  cancellation, heartbeat/retry budgets, and content-package registry.
- Source-backed lesson planning, authentic recording/video, timings, rights,
  captions and learner validation, each with explicit acceptance evidence.
- Independently verified backup/restore and reviewed deployment/release gates.

## Automated checks

`python -m pytest -q` covers the existing workflow plus resume in a new service
instance, provider/voice policy, capacity locks, private checkpoints, CLI JSON
output, source changes, export completeness, edit propagation, tampered/deleted
outputs, edit-during-synthesis, and an additive legacy SQLite upgrade.
`.github/workflows/tests.yml` runs those tests on Python 3.12 with
`requirements-test.txt`; it deliberately excludes model weights/inference.
Real audio/video quality, real device access, and a deployed Dvir OS bridge need
separate evidence.
