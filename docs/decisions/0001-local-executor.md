# ADR 0001 — Local executor with a file-based review contract

Status: implemented on a development branch, pending review.

## Decision

Dvir OS coordinates work, goals, scheduling and human review. Kolmud owns its
lesson processing and pronunciation/practice tools. Both the terminal interface
and existing UI/API call `StudioService`. Do not embed a second general-purpose
agent runtime in Kolmud or expose shell commands over an HTTP endpoint.

The first bridge consists of persisted jobs plus versioned JSON manifests and
agent-run v1 files. It can be tested independently, imported manually, and later
called by an authenticated server-side coordinator. It writes no Dvir OS database
state and installs no service. Transcription is a useful standalone operation;
translation and synthetic speech are explicit optional stages.

Heavy CLI inference uses only loopback Ollama and local XTTS. Existing optional
cloud providers remain an interactive feature; unattended jobs refuse them.
A shared Kolmud data directory supplies process-level capacity locks. Coordination
with other applications' inference remains the external scheduler's job.

## Reliability and review

- Checkpoints use atomic replacement, private file permissions and fsync. An
  OS-released file lock allows an interrupted job to be resumed manually.
- Persisted lesson results are reused after restart; paragraphs are checkpointed
  individually. An interrupted ASR call or translation is retried in full.
- Original audio is retained. New source uploads, generated segment audio and
  exports get separate generations; failures never overwrite an existing master.
- Translation provenance and export checksums fail closed when inputs or outputs
  change. The additive SQLite upgrade does not certify legacy translations.
- A completed process produces `NEEDS_HUMAN`, not publication or pedagogical,
  translation, pronunciation, source, or rights approval. Failed processes yield
  a failed review check.

## Consequences / next gates

A source checkout and Python environment are still required. There is no daemon,
automatic queue dispatch, automatic retry policy, deadline scheduler or phone
execution route. Real model-quality testing, source/rights review, backup/restore,
and a real Dvir OS user-import trial are required before relying on production
content. No public release or production deployment is performed by this branch.
