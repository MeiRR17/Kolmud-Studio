import json
from pathlib import Path

import httpx
import pytest

from core.artifacts import private_json
from core.pipeline import WorkflowStateError
from core.publication import VideoPublication
from core.youtube import CaptionClient, YouTubeError
from test_generated_video import lesson_with_audio
from test_youtube import CHANNEL, VIDEO, FakeGoogle


class UploadGoogle(FakeGoogle):
    def __init__(self):
        super().__init__()
        self.starts = 0
        self.offset = 0
        self.total = None
        self.complete = False
        self.disconnect = False
        self.expired = False
        self.location = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=PRIVATE_CAPABILITY"
        self.upload_metadata = None
        self.remote_privacy = "private"

    def handle(self, request):
        if request.url.path == "/youtube/v3/videos":
            self.requests.append(request)
            return httpx.Response(200, json={"items": [{"id": VIDEO, "snippet": {"channelId": self.channel,
                                                       "title": self.upload_metadata["snippet"]["title"]},
                                                       "status": {"privacyStatus": self.remote_privacy},
                                                       "contentDetails": {"duration": self.duration}}]})
        if request.url.path == "/upload/youtube/v3/videos":
            self.requests.append(request)
            assert request.headers["Authorization"] == "Bearer FAKE_ACCESS"
            if request.method == "POST":
                self.starts += 1
                self.upload_metadata = json.loads(request.content)
                self.total = int(request.headers["X-Upload-Content-Length"])
                assert request.url.params["notifySubscribers"] == "false"
                assert self.upload_metadata["status"]["privacyStatus"] == "private"
                return httpx.Response(200, headers={"Location": self.location})
            assert request.method == "PUT"
            if self.expired:
                return httpx.Response(404)
            content_range = request.headers["Content-Range"]
            if content_range.startswith("bytes */"):
                assert not request.content
                assert int(content_range.split("/")[1]) == self.total
                if self.complete:
                    return httpx.Response(201, json={"id": VIDEO})
                return httpx.Response(308, headers={"Range": f"bytes=0-{self.offset - 1}"} if self.offset else {})
            start_end, total = content_range.removeprefix("bytes ").split("/")
            start, end = map(int, start_end.split("-"))
            assert start == self.offset and len(request.content) == end - start + 1 and int(total) == self.total
            self.offset = end + 1
            self.complete = self.offset == self.total
            if self.disconnect:
                self.disconnect = False
                raise httpx.ReadTimeout("SECRET_UPLOAD_URL_AND_HEADERS")
            return httpx.Response(201, json={"id": VIDEO}) if self.complete else httpx.Response(308, headers={"Range": f"bytes=0-{self.offset - 1}"})
        return super().handle(request)


@pytest.fixture
def upload_google(tmp_path):
    fake = UploadGoogle()
    credentials = tmp_path / "youtube.json"
    private_json(credentials, {"client_id": "FAKE_ID", "client_secret": "FAKE_SECRET", "refresh_token": "FAKE_REFRESH"})
    with httpx.Client(transport=httpx.MockTransport(fake.handle)) as transport:
        yield fake, CaptionClient(credentials, client=transport)


def prepare_video(service):
    lesson = lesson_with_audio(service)
    output = service.build_generated_video(lesson.id)
    publication = VideoPublication(service)
    review = publication.prepare(lesson.id, Path(output.manifest_path), CHANNEL)
    return lesson, publication, Path(review["reviewPath"])


def test_generated_video_and_native_captions_deliver_without_existing_video_id(service, upload_google):
    fake, client = upload_google
    lesson, publication, review = prepare_video(service)
    assert "videoId" not in json.loads(review.read_text())
    publication.approve(review, reviewed=True)
    result = publication.publish(review, client)
    assert result["status"] == "verified_private_delivery" and result["videoId"] == VIDEO
    assert result["tracks"][0]["language"] == "en" and result["tracks"][0]["verified"]
    assert "uploadSession" not in result and "PRIVATE_CAPABILITY" not in json.dumps(result)
    assert fake.starts == 1 and fake.offset == fake.total
    assert publication.publish(review, client)["status"] == "verified_private_delivery"
    assert fake.starts == 1 and len(fake.writes) == 1
    assert fake.upload_metadata["snippet"]["title"] == lesson.title
    assert Path(result["receiptPath"]).stat().st_mode & 0o077 == 0


def test_lost_final_ack_recovers_id_without_duplicate_video(service, upload_google):
    fake, client = upload_google
    _, publication, review = prepare_video(service)
    publication.approve(review, reviewed=True)
    fake.disconnect = True
    with pytest.raises(YouTubeError) as failure:
        publication.publish(review, client)
    assert "SECRET" not in str(failure.value)
    state = json.loads((review.parent / "youtube-receipt.json").read_text())
    assert state["uploadSession"] == fake.location and "videoId" not in state
    assert publication.publish(review, client)["status"] == "verified_private_delivery"
    assert fake.starts == 1


def test_private_video_survives_caption_error_and_retry_does_not_reupload(service, upload_google):
    fake, client = upload_google
    _, publication, review = prepare_video(service)
    publication.approve(review, reviewed=True)
    fake.error_on_language = "en"
    with pytest.raises(YouTubeError):
        publication.publish(review, client)
    receipt = json.loads((review.parent / "youtube-receipt.json").read_text())
    assert receipt["videoId"] == VIDEO
    fake.error_on_language = None
    assert publication.publish(review, client)["status"] == "verified_private_delivery"
    assert fake.starts == 1


def test_expired_upload_never_starts_another_video_automatically(service, upload_google):
    fake, client = upload_google
    _, publication, review = prepare_video(service)
    publication.approve(review, reviewed=True)
    fake.disconnect = True
    with pytest.raises(YouTubeError):
        publication.publish(review, client)
    fake.expired = True
    with pytest.raises(YouTubeError):
        publication.publish(review, client)
    assert fake.starts == 1


def test_private_delivery_requires_remote_privacy_readback(service, upload_google):
    fake, client = upload_google
    _, publication, review = prepare_video(service)
    publication.approve(review, reviewed=True)
    fake.remote_privacy = "public"
    with pytest.raises(YouTubeError, match="privacy does not match"):
        publication.publish(review, client)
    assert not fake.writes
    assert json.loads((review.parent / "youtube-receipt.json").read_text())["status"] != "verified_private_delivery"


@pytest.mark.parametrize("change", ["unapproved", "title", "channel", "video"])
def test_unreviewed_or_changed_generation_does_not_contact_google(service, upload_google, change):
    fake, client = upload_google
    lesson, publication, review = prepare_video(service)
    if change != "unapproved":
        publication.approve(review, reviewed=True)
    if change == "title":
        service.rename_lesson(lesson.id, "Changed title")
    elif change == "channel":
        data = json.loads(review.read_text())
        data["channelId"] = "UC" + "b" * 22
        private_json(review, data)
    elif change == "video":
        manifest = json.loads(Path(json.loads(review.read_text())["generatedManifest"]).read_text())
        Path(next(item["path"] for item in manifest["files"] if item["path"].endswith(".mp4"))).write_bytes(b"changed")
    with pytest.raises(WorkflowStateError):
        publication.publish(review, client)
    assert not fake.requests


def test_resumable_chunks_keep_server_offset_across_interruption(tmp_path, upload_google):
    fake, client = upload_google
    path = tmp_path / "large.mp4"
    path.write_bytes(b"x" * (8 * 1024 * 1024 + 1024))
    state = {}
    checkpoints = []
    metadata = {"snippet": {"title": "Demo", "categoryId": "27"}, "status": {"privacyStatus": "private"}}
    fake.disconnect = True
    with pytest.raises(YouTubeError):
        client.upload_video(path, metadata, state, lambda: checkpoints.append(dict(state)))
    assert fake.offset == 8 * 1024 * 1024 and fake.starts == 1
    assert client.upload_video(path, metadata, state, lambda: checkpoints.append(dict(state))) == VIDEO
    assert fake.starts == 1 and fake.offset == path.stat().st_size
    assert checkpoints[0]["uploadSession"] == fake.location


@pytest.mark.parametrize("location", ["https://evil.invalid/upload", "http://www.googleapis.com/upload/youtube/v3/videos",
                                      "https://user@www.googleapis.com/upload/youtube/v3/videos"])
def test_upload_destination_cannot_leak_token_or_media(tmp_path, upload_google, location):
    fake, client = upload_google
    fake.location = location
    path = tmp_path / "video.mp4"
    path.write_bytes(b"test media")
    with pytest.raises(YouTubeError, match="unexpected upload destination"):
        client.upload_video(path, {"status": {"privacyStatus": "private"}}, {}, lambda: None)
    assert not any(request.method == "PUT" for request in fake.requests)
