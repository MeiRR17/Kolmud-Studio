"""Local, checkpointed CLI jobs and file-based Dvir OS review handoff.

This is a single-host executor, not a scheduler or a public execution API.
Checkpoints are committed after durable service operations; an interrupted
ASR call or translation is retried, while completed audio paragraphs are reused.
"""

from __future__ import annotations

import ipaddress
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable

from core import __version__
from core.artifacts import private_json, private_text
from core.audio_utils import AudioToolError
from core.pipeline import StudioService, WorkflowStateError, file_digest
from core.runtime import PipelineBusyError, file_lock

STAGES = ("transcript", "translation", "audio", "master", "synchronized", "video")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def _policy(self, until: str, voice_mode: str = "synthetic") -> None:
        settings = self.service.settings
        if STAGES.index(until) >= 1:
            if settings.llm_provider != "ollama" or not loopback_url(settings.ollama_base_url):
                raise WorkflowStateError("CLI jobs require local Ollama on a loopback URL; cloud translation is disabled.")
        if STAGES.index(until) >= 2 and voice_mode != "recorded" and settings.tts_engine != "xtts":
            raise WorkflowStateError("CLI jobs require local XTTS; cloud voice APIs are disabled.")
        if STAGES.index(until) >= 2 and voice_mode != "recorded":
            if not settings.xtts_accept_cpml:
                raise WorkflowStateError("Read the XTTS license and explicitly set XTTS_ACCEPT_CPML=true before synthesis.")
            if not settings.reference_voice_path.is_file():
                raise WorkflowStateError("Set up a reference voice recording before requesting synthesis.")

    def run(self, source: Path | None = None, *, job_id: str | None = None,
            until: str | None = None, title: str | None = None, goal_ids: list[str] | None = None,
            on_started: Callable[[str], None] | None = None, source_timed: bool | None = None,
            voice_mode: str | None = None, max_speed: float | None = None,
            input_kind: str | None = None) -> dict:
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
                    "inputKind": input_kind or "audio",
                }
            job.setdefault("inputKind", "audio")
            if job["inputKind"] not in ("audio", "text") or (input_kind is not None and input_kind != job["inputKind"]):
                raise ValueError("Choose audio or text input when creating a job; resume preserves its input kind.")
            if source_timed is not None:
                job["translationMode"] = "source_timed" if source_timed else "free"
            job.setdefault("translationMode", "free")
            target = until or job["until"]
            if target not in STAGES:
                raise ValueError("Unknown pipeline stage.")
            if target == "synchronized":
                job["translationMode"] = "source_timed"
            if job["inputKind"] == "text" and job["translationMode"] == "source_timed":
                raise WorkflowStateError("Text input has no source recording timestamps; use the narration-based video target without --source-timed.")
            if voice_mode is not None:
                job["voiceMode"] = voice_mode
            job.setdefault("voiceMode", "synthetic")
            if job["voiceMode"] not in ("synthetic", "recorded", "mixed"):
                raise ValueError("Unknown voice mode.")
            if max_speed is not None:
                job["maxSpeed"] = max_speed
            job.setdefault("maxSpeed", 1.15)
            import math
            if not math.isfinite(job["maxSpeed"]) or not 1 <= job["maxSpeed"] <= 1.25:
                raise ValueError("Maximum dubbing speed must be between 1.0 and 1.25.")
            # Fail before attaching private audio or invoking any model.
            self._policy(target, job["voiceMode"])
            job.update(until=target, status="RUNNING", attempts=job["attempts"] + 1, errorType=None, errorMessage=None)
            for field in ("manifestPath", "reviewPath", "handoffErrorType", "sourceCaptions"):
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
                if isinstance(exc, (WorkflowStateError, PipelineBusyError, FileNotFoundError)):
                    message = str(exc)
                elif isinstance(exc, AudioToolError):
                    message = "The recording could not be decoded; check that it is a valid audio file."
                else:
                    message = "Engine execution failed; retry or inspect the provider locally."
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
        attached = (Path(job["attachedTextPath"]) if job.get("attachedTextPath") else None) if job["inputKind"] == "text" else svc.abs_path(lesson.source_audio_path)
        if attached is None and job["inputKind"] == "text":
            raw = Path(job["source"]["path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != job["source"]["sha256"]:
                raise WorkflowStateError("Original script changed; create a new job.")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise WorkflowStateError("Text input must be a UTF-8 Hebrew script.") from None
            if not text.strip():
                raise WorkflowStateError("The Hebrew script is empty.")
            attached = svc._lesson_dir(lesson.id) / "source" / uuid.uuid4().hex / "script.he.txt"
            private_text(attached, text)
            job["attachedTextPath"] = str(attached.resolve())
            job["attachedSha256"] = file_digest(attached)
            self._save(job)
        elif attached is None:
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
            if job["inputKind"] == "text":
                lesson = svc.set_transcript(lesson.id, attached.read_text(encoding="utf-8"))
                job["steps"].append({"tool": "Hebrew script import", "detail": "Imported the selected text; no recording/ASR timestamps claimed.", "at": now()})
            else:
                lesson = svc.transcribe(lesson.id)
                job["models"]["transcription"] = settings.hebrew_asr_model
                job["steps"].append({"tool": "MLX Whisper", "detail": "Transcribed the attached Hebrew recording.", "at": now()})
        if not lesson.hebrew_transcript.strip():
            raise WorkflowStateError("Transcription was empty; review the source recording.")
        self._complete(job, "transcript")
        if job["until"] == "transcript":
            return

        self._stage(job, "translation")
        timed = job["translationMode"] == "source_timed"
        links_current = True
        if timed:
            timeline = svc.get_source_timeline(lesson.id)
            if timeline.stale:
                raise WorkflowStateError("Review the current source timeline before timed translation.")
            links_current = [seg.source_cue_id for seg in lesson.segments] == [cue.id for cue in timeline.cues]
        if not lesson.segments or lesson.translation_is_stale or not links_current:
            lesson = svc.translate(lesson.id, source_timed=True) if timed else svc.translate(lesson.id)
            job["models"]["translation"] = settings.ollama_model
            job["steps"].append({"tool": "Ollama", "detail": "Translated the current transcript; applied pronunciation lexicon.", "at": now()})
        self._complete(job, "translation")
        if job["until"] == "translation":
            return

        self._stage(job, "audio")
        mode = job["voiceMode"]
        voice = ({"engine": "human_recordings"} if mode == "recorded" else
                 {"referenceSha256": file_digest(settings.reference_voice_path), "engine": "XTTS-v2",
                  "temperature": settings.xtts_temperature, "speed": settings.xtts_speed, "devicePreference": settings.tts_device})
        if mode != "recorded" and job.get("voice", {}).get("engine") == "XTTS-v2" and job["voice"] != voice:
            raise WorkflowStateError("Voice settings changed; restore the original voice/settings or start a new job.")
        job["voice"] = voice
        self._save(job)
        for seg in lesson.segments:
            path = svc.abs_path(seg.audio_path)
            ready = path is not None and path.is_file() and not seg.audio_is_stale
            if mode == "recorded":
                if not ready or seg.audio_origin != "recorded":
                    raise WorkflowStateError(f"Attach a current English recording for paragraph {seg.position} (segment {seg.id}) with kolmud recording, then resume; recorded mode never synthesizes speech.")
                continue
            if not ready or (mode == "synthetic" and seg.audio_origin == "recorded"):
                svc.synthesize_segment(seg.id)
                # Record where synthesis really ran: a silent MPS->CPU fallback is ~5x slower.
                device = getattr(svc.make_tts_engine(), "device", None)
                job["models"]["synthesis"] = f"XTTS-v2 ({device})" if device else "XTTS-v2"
                job["steps"].append({"tool": "XTTS-v2", "detail": f"Generated paragraph {seg.position}.", "at": now()})
                self._save(job)
        self._complete(job, "audio")
        if job["until"] == "audio":
            return

        if job["until"] == "video":
            self._stage(job, "video")
            previous = job.get("generatedVideo", {})
            try:
                if not previous:
                    raise WorkflowStateError("No generated video yet.")
                result = svc.get_generated_video(lesson.id, Path(previous["manifest_path"]))
            except WorkflowStateError:
                result = svc.build_generated_video(lesson.id)
            job["generatedVideo"] = result.model_dump()
            job["steps"].append({"tool": "Local slide renderer", "detail": "Created or verified English-narration video; no filmed source video required.", "at": now()})
            self._complete(job, "video")
            return

        if job["until"] == "synchronized":
            self._stage(job, "synchronized")
            previous = job.get("synchronizedOutputs", {})
            try:
                if not previous:
                    raise WorkflowStateError("No synchronized generation yet.")
                result = svc.get_synchronized_audio(lesson.id, Path(previous["manifest_path"]))
                manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
                if manifest["maxSpeed"] != job["maxSpeed"]:
                    raise WorkflowStateError("Requested timing settings changed.")
            except WorkflowStateError:
                result = svc.build_synchronized_audio(lesson.id, job["maxSpeed"])
            job["synchronizedOutputs"] = result.model_dump()
            self._complete(job, "synchronized")
            return

        self._stage(job, "master")
        try:
            svc.get_master(lesson.id)
        except WorkflowStateError:
            svc.build_master(lesson.id)
        self._complete(job, "master")

    def _validate_target(self, job: dict) -> None:
        lesson = self.service.get_lesson(job["lessonId"])
        source = Path(job["attachedTextPath"]) if job["inputKind"] == "text" else self.service.abs_path(lesson.source_audio_path)
        if source is None or file_digest(source) != job["attachedSha256"]:
            raise WorkflowStateError("Attached recording changed during the run.")
        if not lesson.hebrew_transcript.strip():
            raise WorkflowStateError("Transcript is empty.")
        target = STAGES.index(job["until"])
        if target >= 1 and (not lesson.segments or lesson.translation_is_stale):
            raise WorkflowStateError("Translation is missing or out of date.")
        if target >= 2:
            if job["voiceMode"] != "recorded" and file_digest(self.service.settings.reference_voice_path) != job["voice"]["referenceSha256"]:
                raise WorkflowStateError("Reference voice changed during the run.")
            for seg in lesson.segments:
                path = self.service.abs_path(seg.audio_path)
                if path is None or not path.is_file() or seg.audio_is_stale:
                    raise WorkflowStateError(f"Paragraph {seg.position} has missing or stale audio.")
                if job["voiceMode"] == "recorded" and seg.audio_origin != "recorded":
                    raise WorkflowStateError("Recorded voice mode requires authentic paragraph recordings.")
                if job["voiceMode"] == "synthetic" and seg.audio_origin == "recorded":
                    raise WorkflowStateError("Voice origin changed during synthesis.")
        if job["until"] == "master":
            self.service.get_master(lesson.id)
        elif job["until"] == "synchronized":
            self.service.get_synchronized_audio(lesson.id, Path(job["synchronizedOutputs"]["manifest_path"]))
        elif job["until"] == "video":
            self.service.get_generated_video(lesson.id, Path(job["generatedVideo"]["manifest_path"]))

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
            if job["translationMode"] == "source_timed" and STAGES.index(job["until"]) >= 1:
                # Regenerate from current state on each handoff; never include
                # a stale source caption from an earlier attempt.
                if job["status"] != "FAILED":
                    captions = self.service.build_source_captions(lesson.id)
                    job["sourceCaptions"] = captions.model_dump()
                    for key, label in (("hebrew_srt_path", "Source Hebrew captions"),
                                       ("english_srt_path", "Source English captions"),
                                       ("manifest_path", "Source caption manifest")):
                        asset(label, Path(getattr(captions, key)))
            source = self.service.abs_path(lesson.source_audio_path)
            if source and source.is_file():
                asset("Source recording (normalized)", source)
            if job["inputKind"] == "text" and job.get("attachedTextPath") and Path(job["attachedTextPath"]).is_file():
                asset("Source Hebrew script", Path(job["attachedTextPath"]))
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
                        origin = "Recorded" if seg.audio_origin == "recorded" else "Synthetic"
                        asset(f"{origin} paragraph {seg.position}", path)
            if job["until"] == "synchronized" and job["status"] != "FAILED":
                synced = self.service.get_synchronized_audio(lesson.id, Path(job["synchronizedOutputs"]["manifest_path"]))
                for key, label in (("wav_path", "Synchronized English WAV"), ("mp3_path", "Synchronized English MP3"),
                                   ("english_srt_path", "Synchronized English SRT"), ("video_path", "Synchronized English video"),
                                   ("manifest_path", "Synchronized generation manifest")):
                    if getattr(synced, key):
                        asset(label, Path(getattr(synced, key)))
            if job["until"] == "video" and job["status"] != "FAILED":
                generated = self.service.get_generated_video(lesson.id, Path(job["generatedVideo"]["manifest_path"]))
                for key, label in (("video_path", "Generated English lesson video"), ("audio_path", "Generated video narration"),
                                   ("english_srt_path", "Generated video captions"), ("poster_path", "Generated video poster"),
                                   ("storyboard_path", "Generated video storyboard"), ("manifest_path", "Generated video manifest")):
                    asset(label, Path(getattr(generated, key)))
            if lesson.master_audio_path and job["status"] != "FAILED":
                # A master left over from an earlier, broader run may no longer be current;
                # list only a verified one instead of failing a narrower (e.g. audio) job.
                try:
                    master = self.service.get_master(lesson.id)
                except WorkflowStateError:
                    if job["until"] == "master":
                        raise
                else:
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
            questions.append("Listen to the English recordings/synthesis and check pronunciation and synchronization. Synthetic voice does not replace an authentic teacher recording when one is required.")
        manifest = {"schemaVersion": 1, "producer": {"name": "Kolmud Studio", "version": __version__},
                    "jobId": job["id"], "lessonId": job["lessonId"], "status": job["status"],
                    "requestedStage": job["until"], "inputKind": job["inputKind"], "translationMode": job["translationMode"], "source": job["source"], "goalIds": job["goalIds"],
                    "models": job["models"], "voice": job.get("voice"), "voiceMode": job["voiceMode"],
                    "assets": assets, "checks": checks, "openQuestions": questions}
        manifest_path = directory / "manifest.json"
        private_json(manifest_path, manifest)
        report = {
            "id": "kolmud-" + job["id"], "title": job["title"], "kind": "content",
            "agent": {"name": "Kolmud Studio", "runtime": f"local Python CLI v{__version__}",
                      "model": "; ".join(job["models"].values()) or "No model invocation recorded; output may have been reused"},
            "startedAt": job["createdAt"], "finishedAt": job["finishedAt"],
            "input": f"Process local {job['inputKind']} through {job['until']}; source SHA-256: {job['source']['sha256']}.",
            "summary": f"{job['status']}: local processing through {job['stage']}. Human review is required; nothing published.",
            "goalIds": job["goalIds"], "permissions": ["Read: selected recording, reference voice and local configuration", "Write: local processing artifacts and model runtime caches", "Inference: local models only"],
            "steps": job["steps"][-200:], "artifacts": [{"label": "Content package manifest", "kind": "file", "content": str(manifest_path.resolve())}] +
                [{"label": a["label"], "kind": "file", "content": f"{a['path']}\nSHA-256: {a['sha256']}"} for a in assets],
            "evidence": [{"source": "Selected local " + job["inputKind"], "anchor": "SHA-256 " + job["source"]["sha256"]}],
            "checks": checks, "confidence": "low", "openQuestions": questions,
            "rollback": "Keep the original recording. This run writes local processing artifacts; first-time downloads may populate model caches. No Dvir OS state was written.",
        }
        review_path = directory / "agent-run.json"
        private_json(review_path, report)
        job["manifestPath"], job["reviewPath"] = str(manifest_path.resolve()), str(review_path.resolve())
