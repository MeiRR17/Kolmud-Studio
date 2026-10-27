"""Local, checkpointed CLI jobs and file-based Dvir OS review handoff.

This is a single-host executor, not a scheduler or a public execution API.
Checkpoints are committed after durable service operations; an interrupted
ASR call or translation is retried, while completed audio paragraphs are reused.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable

from core import __version__
from core.pipeline import StudioService, WorkflowStateError, file_digest
from core.runtime import PipelineBusyError, file_lock

STAGES = ("transcript", "translation", "audio", "master")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def private_text(path: Path, value: str) -> None:
    """Atomically replace a private file, including a durable file flush."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".checkpoint-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def private_json(path: Path, value: dict) -> None:
    private_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def loopback_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            return False
        return parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        return False


class PipelineRunError(RuntimeError):
    def __init__(self, job: dict):
        self.job = job
        super().__init__(f"Job {job['id']} failed at {job['stage']} ({job['errorType']}): {job['errorMessage']}")


class PipelineRunner:
    def __init__(self, service: StudioService):
        self.service = service
        self.directory = service.settings.data_dir / "jobs"

    def _path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("Job ID must be the 32-character ID printed by kolmud run.")
        return self.directory / f"{job_id}.json"

    def read(self, job_id: str) -> dict:
        return json.loads(self._path(job_id).read_text(encoding="utf-8"))

    def list_jobs(self) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.directory.glob("*.json"))]

    def _save(self, job: dict) -> None:
        job["updatedAt"] = now()
        private_json(self._path(job["id"]), job)

    def _policy(self, until: str) -> None:
        settings = self.service.settings
        if STAGES.index(until) >= 1:
            if settings.llm_provider != "ollama" or not loopback_url(settings.ollama_base_url):
                raise WorkflowStateError("CLI jobs require local Ollama on a loopback URL; cloud translation is disabled.")
        if STAGES.index(until) >= 2 and settings.tts_engine != "xtts":
            raise WorkflowStateError("CLI jobs require local XTTS; cloud voice APIs are disabled.")
        if STAGES.index(until) >= 2:
            if not settings.xtts_accept_cpml:
                raise WorkflowStateError("Read the XTTS license and explicitly set XTTS_ACCEPT_CPML=true before synthesis.")
            if not settings.reference_voice_path.is_file():
                raise WorkflowStateError("Set up a reference voice recording before requesting synthesis.")

    def run(self, source: Path | None = None, *, job_id: str | None = None,
            until: str | None = None, title: str | None = None, goal_ids: list[str] | None = None,
            on_started: Callable[[str], None] | None = None) -> dict:
        with file_lock(self.service.settings.data_dir / ".jobs.lock"):
            if job_id:
                job = self.read(job_id)
                if source is not None:
                    raise ValueError("Resume uses the original input; create a new job for a different recording.")
            else:
                if source is None or not source.is_file():
                    raise FileNotFoundError("Choose an existing recording.")
                source = source.resolve()
                job = {
                    "schemaVersion": 1, "id": uuid.uuid4().hex, "appVersion": __version__,
                    "createdAt": now(), "status": "QUEUED", "stage": "source", "attempts": 0,
                    "source": {"path": str(source), "sha256": file_digest(source)},
                    "title": title or source.stem, "goalIds": goal_ids or [], "completedStages": [],
                    "models": {}, "steps": [], "until": until or "transcript", "lessonId": None,
                }
            target = until or job["until"]
            if target not in STAGES:
                raise ValueError("Unknown pipeline stage.")
            # Fail before attaching private audio or invoking any model.
            self._policy(target)
            job.update(until=target, status="RUNNING", attempts=job["attempts"] + 1, errorType=None, errorMessage=None)
            for field in ("manifestPath", "reviewPath", "handoffErrorType"):
                job.pop(field, None)
            self._save(job)
            if on_started:
                on_started(job["id"])
            try:
                self._execute(job)
                self._validate_target(job)
                job["status"] = "NEEDS_HUMAN"
                job["finishedAt"] = now()
                self._handoff(job)
                self._save(job)
            except Exception as exc:
                # Never put raw model replies, credentials, or HTTP error bodies
                # in review files. The exception class and stage identify failure.
                message = str(exc) if isinstance(exc, (WorkflowStateError, PipelineBusyError, FileNotFoundError)) else "Engine execution failed; retry or inspect the provider locally."
                job.update(status="FAILED", errorType=type(exc).__name__, errorMessage=message, finishedAt=now())
                self._save(job)
                try:
                    self._handoff(job)
                except Exception as handoff_error:
                    job["handoffErrorType"] = type(handoff_error).__name__
                self._save(job)
                raise PipelineRunError(job) from exc
            return job

    def _stage(self, job: dict, stage: str) -> None:
        job["stage"] = stage
        self._save(job)

    def _complete(self, job: dict, stage: str) -> None:
        if stage not in job["completedStages"]:
            job["completedStages"].append(stage)
        self._save(job)

    def _execute(self, job: dict) -> None:
        svc, settings = self.service, self.service.settings
        self._stage(job, "source")
        if job["lessonId"] is None:
            job["lessonId"] = svc.create_lesson(job["title"]).id
            self._save(job)
        lesson = svc.get_lesson(job["lessonId"])
        attached = svc.abs_path(lesson.source_audio_path)
        if attached is None:
            original = Path(job["source"]["path"])
            if file_digest(original) != job["source"]["sha256"]:
                raise WorkflowStateError("Original recording changed; create a new job.")
            lesson = svc.attach_source_audio(lesson.id, original.name, original.read_bytes())
            attached = svc.abs_path(lesson.source_audio_path)
        assert attached is not None
        digest = file_digest(attached)
        if job.get("attachedSha256") and job["attachedSha256"] != digest:
            raise WorkflowStateError("Attached source changed; create a new job.")
        job["attachedSha256"] = digest
        self._complete(job, "source")

        self._stage(job, "transcript")
        if not lesson.hebrew_transcript.strip():
            lesson = svc.transcribe(lesson.id)
            job["models"]["transcription"] = settings.hebrew_asr_model
            job["steps"].append({"tool": "MLX Whisper", "detail": "Transcribed the attached Hebrew recording.", "at": now()})
        if not lesson.hebrew_transcript.strip():
            raise WorkflowStateError("Transcription was empty; review the source recording.")
        self._complete(job, "transcript")
        if job["until"] == "transcript":
            return

        self._stage(job, "translation")
        if not lesson.segments or lesson.translation_is_stale:
            lesson = svc.translate(lesson.id)
            job["models"]["translation"] = settings.ollama_model
            job["steps"].append({"tool": "Ollama", "detail": "Translated the current transcript; applied pronunciation lexicon.", "at": now()})
        self._complete(job, "translation")
        if job["until"] == "translation":
            return

        self._stage(job, "audio")
        voice = {"referenceSha256": file_digest(settings.reference_voice_path), "engine": "XTTS-v2",
                 "temperature": settings.xtts_temperature, "speed": settings.xtts_speed, "devicePreference": settings.tts_device}
        if job.get("voice") and job["voice"] != voice:
            raise WorkflowStateError("Voice settings changed; restore the original voice/settings or start a new job.")
        job["voice"] = voice
        self._save(job)
        for seg in lesson.segments:
            path = svc.abs_path(seg.audio_path)
            if path is None or not path.is_file() or seg.audio_is_stale:
                svc.synthesize_segment(seg.id)
                job["models"]["synthesis"] = "XTTS-v2"
                job["steps"].append({"tool": "XTTS-v2", "detail": f"Generated paragraph {seg.position}.", "at": now()})
                self._save(job)
        self._complete(job, "audio")
        if job["until"] == "audio":
            return

        self._stage(job, "master")
        try:
            svc.get_master(lesson.id)
        except WorkflowStateError:
            svc.build_master(lesson.id)
        self._complete(job, "master")

    def _validate_target(self, job: dict) -> None:
        lesson = self.service.get_lesson(job["lessonId"])
        source = self.service.abs_path(lesson.source_audio_path)
        if source is None or file_digest(source) != job["attachedSha256"]:
            raise WorkflowStateError("Attached recording changed during the run.")
        if not lesson.hebrew_transcript.strip():
            raise WorkflowStateError("Transcript is empty.")
        target = STAGES.index(job["until"])
        if target >= 1 and (not lesson.segments or lesson.translation_is_stale):
            raise WorkflowStateError("Translation is missing or out of date.")
        if target >= 2:
            if file_digest(self.service.settings.reference_voice_path) != job["voice"]["referenceSha256"]:
                raise WorkflowStateError("Reference voice changed during the run.")
            for seg in lesson.segments:
                path = self.service.abs_path(seg.audio_path)
                if path is None or not path.is_file() or seg.audio_is_stale:
                    raise WorkflowStateError(f"Paragraph {seg.position} has missing or stale audio.")
        if target >= 3:
            self.service.get_master(lesson.id)

    def _handoff(self, job: dict) -> None:
        """Produce metadata and bounded review JSON; never publish or approve it."""
        directory = self.directory / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        assets = []

        def asset(label: str, path: Path) -> None:
            assets.append({"label": label, "path": str(path.resolve()), "sha256": file_digest(path), "bytes": path.stat().st_size})

        lesson = self.service.get_lesson(job["lessonId"]) if job["lessonId"] else None
        if lesson:
            snapshot = directory / "lesson.json"
            private_json(snapshot, lesson.model_dump(mode="json"))
            asset("Lesson text and paragraphs", snapshot)
            source = self.service.abs_path(lesson.source_audio_path)
            if source and source.is_file():
                asset("Source recording (normalized)", source)
            if lesson.hebrew_transcript:
                transcript = directory / "transcript.he.txt"
                private_text(transcript, lesson.hebrew_transcript + "\n")
                asset("Hebrew transcript", transcript)
            if lesson.segments and not lesson.translation_is_stale:
                translation = directory / "translation.en.txt"
                private_text(translation, "\n\n".join(seg.english_display for seg in lesson.segments) + "\n")
                asset("English translation", translation)
                for seg in lesson.segments:
                    path = self.service.abs_path(seg.audio_path)
                    if path and path.is_file() and not seg.audio_is_stale:
                        asset(f"Synthetic paragraph {seg.position}", path)
            if lesson.master_audio_path and job["status"] != "FAILED":
                master = self.service.get_master(lesson.id)
                for kind in ("wav", "mp3", "srt"):
                    asset(kind.upper(), Path(getattr(master, f"{kind}_path")))
        checks = [
            {"name": "Requested processing stage", "status": "fail" if job["status"] == "FAILED" else "pass",
             "detail": f"Requested: {job['until']}; last stage: {job['stage']}; error: {job.get('errorMessage') or 'none'}."},
            {"name": "Asset SHA-256 fingerprints", "status": "pass",
             "detail": f"Recorded hashes for {len(assets)} local assets; hash equality is not content-quality verification."},
            {"name": "Human content / pronunciation / rights review", "status": "skipped",
             "detail": "Required before acceptance or publication; no automatic quality or rights approval."},
        ]
        questions = ["Review transcription accuracy and source/recording rights before accepting this output."]
        if STAGES.index(job["until"]) >= 1:
            questions.append("Check translation fidelity and Torah terminology against the original; this is not a source-verified lesson or psak.")
        if STAGES.index(job["until"]) >= 2:
            questions.append("Listen to the generated voice and check pronunciation. Synthetic voice does not replace an authentic teacher recording when one is required.")
        manifest = {"schemaVersion": 1, "producer": {"name": "Kolmud Studio", "version": __version__},
                    "jobId": job["id"], "lessonId": job["lessonId"], "status": job["status"],
                    "requestedStage": job["until"], "source": job["source"], "goalIds": job["goalIds"],
                    "models": job["models"], "voice": job.get("voice"), "assets": assets, "checks": checks, "openQuestions": questions}
        manifest_path = directory / "manifest.json"
        private_json(manifest_path, manifest)
        report = {
            "id": "kolmud-" + job["id"], "title": job["title"], "kind": "content",
            "agent": {"name": "Kolmud Studio", "runtime": f"local Python CLI v{__version__}",
                      "model": "; ".join(job["models"].values()) or "No model invocation recorded; output may have been reused"},
            "startedAt": job["createdAt"], "finishedAt": job["finishedAt"],
            "input": f"Process local recording through {job['until']}; source SHA-256: {job['source']['sha256']}.",
            "summary": f"{job['status']}: local processing through {job['stage']}. Human review is required; nothing published.",
            "goalIds": job["goalIds"], "permissions": ["Read: selected recording, reference voice and local configuration", "Write: local processing artifacts and model runtime caches", "Inference: local models only"],
            "steps": job["steps"][-200:], "artifacts": [{"label": "Content package manifest", "kind": "file", "content": str(manifest_path.resolve())}] +
                [{"label": a["label"], "kind": "file", "content": f"{a['path']}\nSHA-256: {a['sha256']}"} for a in assets],
            "evidence": [{"source": "Selected local recording", "anchor": "SHA-256 " + job["source"]["sha256"]}],
            "checks": checks, "confidence": "low", "openQuestions": questions,
            "rollback": "Keep the original recording. This run writes local processing artifacts; first-time downloads may populate model caches. No Dvir OS state was written.",
        }
        review_path = directory / "agent-run.json"
        private_json(review_path, report)
        job["manifestPath"], job["reviewPath"] = str(manifest_path.resolve()), str(review_path.resolve())
