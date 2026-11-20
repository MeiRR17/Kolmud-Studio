"""Review a specific caption generation, then reconcile its native YouTube tracks."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core import audio_utils
from core.artifacts import private_json
from core.database import session_scope
from core.pipeline import WorkflowStateError, file_digest
from core.runtime import file_lock


def _now():
    return datetime.now(timezone.utc).isoformat()


class CaptionPublication:
    def __init__(self, service):
        self.service = service

    def prepare(self, lesson_id: int, video_id: str, channel_id: str, *, include_english=True) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
            raise ValueError("Use the exact YouTube video ID and channel ID (UC…).")
        outputs = self.service.build_source_captions(lesson_id, include_english=include_english)
        manifest = Path(outputs.manifest_path)
        with session_scope(self.service.db_url) as session:
            lesson = self.service._lesson(session, lesson_id)
            duration = audio_utils.duration_seconds(self.service.abs_path(lesson.source_audio_path))
        review = {"schemaVersion": 1, "lessonId": lesson_id, "videoId": video_id, "channelId": channel_id,
                  "sourceDuration": duration, "captionManifest": str(manifest), "manifestSha256": file_digest(manifest),
                  "createdAt": _now(), "reviewScope": "source fidelity, translation, timings and source-video match"}
        path = manifest.parent / "youtube-review.json"
        private_json(path, review)
        return {"reviewPath": str(path), "review": review, "captions": outputs.model_dump(),
                "next": "Review both SRT files against the source video, then run youtube-approve REVIEW_PATH --reviewed."}

    def validate(self, review_path: Path):
        review_path = review_path.expanduser().resolve()
        if not review_path.is_relative_to(self.service.settings.projects_dir.resolve()):
            raise WorkflowStateError("Use a prepared caption review from this data directory.")
        review = json.loads(review_path.read_text(encoding="utf-8"))
        expected_dir = self.service._lesson_dir(int(review["lessonId"])).resolve() / "source_captions"
        manifest_path = Path(review["captionManifest"]).resolve()
        if manifest_path.parent != review_path.parent or not manifest_path.is_relative_to(expected_dir):
            raise WorkflowStateError("Caption generation does not belong to this lesson.")
        if file_digest(manifest_path) != review["manifestSha256"]:
            raise WorkflowStateError("Caption manifest changed after preparation; prepare and review again.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["lessonId"] != review["lessonId"]:
            raise WorkflowStateError("Caption lesson identity differs from the review.")
        files = {}
        for item in manifest["files"]:
            path = Path(item["path"]).resolve()
            if path.parent != manifest_path.parent or path.name not in ("source.he.srt", "source.en.srt") or file_digest(path) != item["sha256"]:
                raise WorkflowStateError("Caption files changed after preparation; prepare and review again.")
            files["he" if path.name == "source.he.srt" else "en"] = path.read_text(encoding="utf-8")
        if "he" not in files:
            raise WorkflowStateError("Prepared Hebrew captions are missing.")
        with session_scope(self.service.db_url) as session:
            lesson = self.service._lesson(session, review["lessonId"])
            if self.service._source_snapshot(lesson) != manifest["sourceSnapshot"]:
                raise WorkflowStateError("Source timeline changed after preparation; prepare and review again.")
            if "en" in files:
                expected = {cue["id"]: cue["english_text"] for cue in manifest["cues"]}
                if lesson.translation_is_stale or len(lesson.segments) != len(expected) or {seg.source_cue_id: seg.english_display for seg in lesson.segments} != expected:
                    raise WorkflowStateError("Translation changed after preparation; prepare and review again.")
        return review, files

    def approve(self, review_path: Path, *, reviewed=False) -> dict:
        if not reviewed:
            raise WorkflowStateError("Review source, translation, timings and the selected video, then pass --reviewed.")
        review_path = review_path.expanduser().resolve()
        signature = file_digest(review_path)
        review, files = self.validate(review_path)
        if file_digest(review_path) != signature:
            raise WorkflowStateError("Caption review changed during approval; review the current generation again.")
        approval = {"reviewSha256": signature, "reviewedAt": _now(),
                    "videoId": review["videoId"], "channelId": review["channelId"],
                    "languages": list(files), "status": "owner_review_recorded"}
        private_json(review_path.parent / "youtube-approval.json", approval)
        return approval

    def _approved(self, review_path):
        review, files = self.validate(review_path)
        try:
            approval = json.loads((review_path.parent / "youtube-approval.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise WorkflowStateError("This caption generation has no owner review; run youtube-approve first.") from None
        if approval.get("reviewSha256") != file_digest(review_path):
            raise WorkflowStateError("Publication target or content changed after review; prepare and review again.")
        return review, files

    def publish(self, review_path: Path, client) -> dict:
        review_path = review_path.expanduser().resolve()
        review, _ = self._approved(review_path)  # zero network calls for unreviewed/stale input
        receipt_path = review_path.parent / "youtube-receipt.json"
        with client.publication_lock(review["videoId"]):
            review, files = self._approved(review_path)
            receipt = {"reviewSha256": file_digest(review_path), "videoId": review["videoId"],
                       "channelId": review["channelId"], "startedAt": _now(), "status": "in_progress", "tracks": []}
            private_json(receipt_path, receipt)
            def still_current():
                self._approved(review_path)
                if file_digest(review_path) != receipt["reviewSha256"]:
                    raise WorkflowStateError("Publication review changed during upload; reconcile remote state before continuing.")
            try:
                client.verify_video(review["videoId"], review["channelId"], review["sourceDuration"])
                for language, content in files.items():
                    track = client.deliver(review["videoId"], language, content,
                                           before_write=still_current)
                    receipt["tracks"].append(track)
                    private_json(receipt_path, receipt)  # preserve partial success across languages
                still_current()
                receipt["status"] = "verified" if all(track["verified"] for track in receipt["tracks"]) else "awaiting_youtube_processing"
                receipt["finishedAt"] = _now()
                private_json(receipt_path, receipt)
            except Exception:
                receipt["status"] = "interrupted_reconcile_required"
                receipt["finishedAt"] = _now()
                # Do not persist exception strings: transports may contain secret request URLs.
                private_json(receipt_path, receipt)
                raise
        return {**receipt, "receiptPath": str(receipt_path)}


class VideoPublication(CaptionPublication):
    """Deliver a generated lesson to a new private video and its native English captions."""

    def prepare(self, lesson_id: int, manifest_path: Path, channel_id: str) -> dict:
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
            raise ValueError("Use the exact YouTube channel ID (UC…).")
        output = self.service.get_generated_video(lesson_id, manifest_path)
        manifest_path = Path(output.manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        title = manifest["inputs"]["title"]
        if not 1 <= len(title) <= 100 or any(char in title for char in "<>"):
            raise ValueError("YouTube title must contain 1–100 characters and no angle brackets; rename the lesson.")
        status = {"privacyStatus": "private"}
        if any(segment["audioOrigin"] == "synthetic" for segment in manifest["inputs"]["segments"]):
            status["containsSyntheticMedia"] = True
        review = {"schemaVersion": 1, "kind": "generated_video", "requestId": uuid.uuid4().hex,
                  "lessonId": lesson_id, "channelId": channel_id, "generatedManifest": str(manifest_path),
                  "manifestSha256": file_digest(manifest_path), "durationSeconds": output.duration_seconds,
                  "metadata": {"snippet": {"title": title, "description": "English lesson with text slides, produced with Kolmud Studio.",
                                           "categoryId": "27"}, "status": status},
                  "createdAt": _now(), "reviewScope": "generated video, narration, English captions, rights, metadata and selected channel"}
        directory = manifest_path.parent / "publications" / review["requestId"]
        path = directory / "youtube-review.json"
        private_json(path, review)
        return {"reviewPath": str(path), "review": review, "video": output.model_dump(),
                "next": "Review the generated MP4, audio, captions and channel, then run youtube-video-approve REVIEW --reviewed."}

    def validate(self, review_path: Path):
        review_path = review_path.expanduser().resolve()
        if not review_path.is_relative_to(self.service.settings.projects_dir.resolve()):
            raise WorkflowStateError("Use a generated-video review in this data directory.")
        review = json.loads(review_path.read_text(encoding="utf-8"))
        if review.get("kind") != "generated_video" or not re.fullmatch(r"[a-f0-9]{32}", review.get("requestId", "")):
            raise WorkflowStateError("Use a prepared generated-video review.")
        manifest = Path(review["generatedManifest"]).resolve()
        if review_path.parent != manifest.parent / "publications" / review["requestId"]:
            raise WorkflowStateError("Video review does not belong to this generation.")
        if file_digest(manifest) != review["manifestSha256"]:
            raise WorkflowStateError("Generated video manifest changed; prepare and review again.")
        output = self.service.get_generated_video(review["lessonId"], manifest)
        if review["metadata"].get("status", {}).get("privacyStatus") != "private":
            raise WorkflowStateError("Generated-video delivery currently supports private uploads only.")
        if review["durationSeconds"] != output.duration_seconds:
            raise WorkflowStateError("Generated video duration changed after preparation.")
        return review, {"en": Path(output.english_srt_path).read_text(encoding="utf-8")}

    def approve(self, review_path: Path, *, reviewed=False) -> dict:
        if not reviewed:
            raise WorkflowStateError("Review the generated video/narration/captions/rights/channel, then pass --reviewed.")
        review_path = review_path.expanduser().resolve()
        signature = file_digest(review_path)
        review, files = self.validate(review_path)
        if file_digest(review_path) != signature:
            raise WorkflowStateError("Video review changed during approval; review again.")
        approval = {"reviewSha256": signature, "reviewedAt": _now(), "channelId": review["channelId"],
                    "requestId": review["requestId"], "languages": list(files), "status": "owner_review_recorded"}
        private_json(review_path.parent / "youtube-approval.json", approval)
        return approval

    def publish(self, review_path: Path, client) -> dict:
        review_path = review_path.expanduser().resolve()
        review, _ = self._approved(review_path)
        receipt_path = review_path.parent / "youtube-receipt.json"
        with file_lock(client.credentials.parent / f".youtube-upload-{review['requestId']}.lock"):
            review, files = self._approved(review_path)
            signature = file_digest(review_path)
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if receipt.get("reviewSha256") != signature:
                    raise WorkflowStateError("An upload exists for different reviewed bytes; reconcile it before preparing another upload.")
            else:
                receipt = {"reviewSha256": signature, "requestId": review["requestId"], "channelId": review["channelId"],
                           "startedAt": _now(), "tracks": []}

            def checkpoint():
                private_json(receipt_path, receipt)

            def still_current():
                self._approved(review_path)
                if file_digest(review_path) != signature:
                    raise WorkflowStateError("Video review changed during upload; reconcile remote state.")

            receipt["status"] = "in_progress"
            checkpoint()
            try:
                client.verify_channel(review["channelId"])
                generated = self.service.get_generated_video(review["lessonId"], Path(review["generatedManifest"]))
                video_id = client.upload_video(Path(generated.video_path), review["metadata"], receipt, checkpoint, before_write=still_current)
                # YouTube processing may not yet expose a usable duration. The saved ID
                # prevents any retry from inserting a second video while waiting.
                client.verify_video(video_id, review["channelId"], review["durationSeconds"], expected_privacy="private",
                                    expected_title=review["metadata"]["snippet"]["title"])
                for language, content in files.items():
                    track = client.deliver(video_id, language, content, before_write=still_current)
                    receipt["tracks"] = [track]
                    checkpoint()
                still_current()
                receipt["status"] = "verified_private_delivery" if all(track["verified"] for track in receipt["tracks"]) else "awaiting_youtube_processing"
                receipt["finishedAt"] = _now()
                checkpoint()
            except Exception:
                receipt["status"] = "interrupted_reconcile_required"
                receipt["finishedAt"] = _now()
                checkpoint()
                raise
        # The resumable session URI is a credential-like capability, private even on errors.
        return {**{key: value for key, value in receipt.items() if key != "uploadSession"}, "receiptPath": str(receipt_path)}
