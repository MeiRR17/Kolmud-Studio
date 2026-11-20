import base64
import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen
from urllib.error import HTTPError

import httpx
import pytest

from core.artifacts import private_json
from core.pipeline import WorkflowStateError
from core.publication import CaptionPublication
from core.schemas import SourceCueUpdate, SegmentUpdate
from core.youtube import CaptionClient, SCOPE, YouTubeError, canonical_srt, connect, credential_path
from test_source_timing import timed_lesson

VIDEO = "abcdefghijk"
CHANNEL = "UC" + "a" * 22


class FakeGoogle:
    """Stateful wire fake: Google API paths, multipart/related, ownership and readback."""
    def __init__(self):
        self.tracks = {}
        self.requests = []
        self.writes = []
        self.channel = CHANNEL
        self.owner = CHANNEL
        self.duration = "PT1S"
        self.status = "serving"
        self.mismatch = False
        self.error_on_language = None
        self.interrupt = False
        self.on_read = lambda: None

    def handle(self, request):
        self.requests.append(request)
        if request.url.path == "/token":
            assert request.url.host == "oauth2.googleapis.com"
            return httpx.Response(200, json={"access_token": "FAKE_ACCESS", "refresh_token": "FAKE_REFRESH", "scope": SCOPE})
        assert request.headers["Authorization"] == "Bearer FAKE_ACCESS"
        path = request.url.path
        if path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"id": self.owner}]})
        if path.endswith("/videos"):
            return httpx.Response(200, json={"items": [{"id": VIDEO, "snippet": {"channelId": self.channel},
                                                        "contentDetails": {"duration": self.duration}}]})
        if path == "/youtube/v3/captions":
            self.on_read()
            return httpx.Response(200, json={"items": [{"id": k, "snippet": v["snippet"]} for k, v in self.tracks.items()]})
        if path.startswith("/youtube/v3/captions/"):
            content = self.tracks[path.rsplit("/", 1)[1]]["content"]
            return httpx.Response(200, text=content.replace("Shabbat", "WRONG") if self.mismatch else content)
        if path == "/upload/youtube/v3/captions":
            assert "sync" not in request.url.params
            assert request.url.params["uploadType"] == "multipart"
            boundary = request.headers["content-type"].split("boundary=")[1]
            parts = request.content.decode().split("--" + boundary)
            metadata = json.loads(parts[1].split("\r\n\r\n", 1)[1].strip())
            content = parts[2].split("\r\n\r\n", 1)[1].strip()
            if request.method == "POST":
                snippet = metadata["snippet"]
                assert set(snippet) == {"videoId", "language", "name", "isDraft"}
                key = "track" + snippet["language"]
                assert key not in self.tracks, "Duplicate caption insert"
            else:
                assert request.method == "PUT"
                assert set(metadata) == {"id", "snippet"}
                assert metadata["snippet"] == {"isDraft": False}
                key = metadata["id"]
                snippet = {**self.tracks[key]["snippet"], **metadata["snippet"]}
            if snippet["language"] == self.error_on_language:
                return httpx.Response(403, text="SECRET_GOOGLE_ERROR_BODY")
            snippet["status"] = self.status
            self.tracks[key] = {"snippet": snippet, "content": content}
            self.writes.append((request.method, key))
            if self.interrupt:
                self.interrupt = False
                raise httpx.ReadTimeout("SECRET_REQUEST_DETAILS")
            return httpx.Response(200, json={"id": key, "snippet": snippet})
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")


@pytest.fixture
def google(tmp_path):
    fake = FakeGoogle()
    credentials = tmp_path / "account" / "youtube.json"
    private_json(credentials, {"client_id": "FAKE_ID", "client_secret": "FAKE_SECRET", "refresh_token": "FAKE_REFRESH"})
    with httpx.Client(transport=httpx.MockTransport(fake.handle)) as transport:
        yield fake, CaptionClient(credentials, client=transport)


def prepared(service, wav_bytes, monkeypatch):
    lesson = timed_lesson(service, wav_bytes, monkeypatch)
    service.translate(lesson.id, source_timed=True)
    publication = CaptionPublication(service)
    result = publication.prepare(lesson.id, VIDEO, CHANNEL)
    return lesson, publication, Path(result["reviewPath"])


def test_native_caption_insert_verified_retry_idempotent(service, wav_bytes, monkeypatch, google):
    fake, client = google
    lesson, publication, review = prepared(service, wav_bytes, monkeypatch)
    # Preserve an unrelated track even when it uses the same language.
    fake.tracks["manual"] = {"snippet": {"language": "en", "name": "My captions", "status": "serving"}, "content": "original"}
    publication.approve(review, reviewed=True)
    receipt = publication.publish(review, client)
    assert receipt["status"] == "verified"
    assert len(fake.writes) == 2
    assert all(track["verified"] for track in receipt["tracks"])
    assert publication.publish(review, client)["status"] == "verified"
    assert len(fake.writes) == 2
    assert fake.tracks["manual"]["content"] == "original"
    assert Path(receipt["receiptPath"]).stat().st_mode & 0o077 == 0


def test_new_review_updates_existing_tracks(service, wav_bytes, monkeypatch, google):
    fake, client = google
    lesson, publication, review = prepared(service, wav_bytes, monkeypatch)
    publication.approve(review, reviewed=True)
    publication.publish(review, client)
    segment = service.get_lesson(lesson.id).segments[0]
    service.update_segment(segment.id, SegmentUpdate(english_display="A corrected translation."))
    new_review = Path(publication.prepare(lesson.id, VIDEO, CHANNEL)["reviewPath"])
    publication.approve(new_review, reviewed=True)
    assert publication.publish(new_review, client)["status"] == "verified"
    assert fake.writes == [("POST", "trackhe"), ("POST", "tracken"), ("PUT", "tracken")]


@pytest.mark.parametrize("change", ["no_review", "srt", "translation", "source", "target"])
def test_unreviewed_or_changed_content_never_contacts_google(service, wav_bytes, monkeypatch, google, change):
    fake, client = google
    lesson, publication, review = prepared(service, wav_bytes, monkeypatch)
    if change != "no_review":
        publication.approve(review, reviewed=True)
    if change == "srt":
        (review.parent / "source.en.srt").write_text("changed")
    elif change == "translation":
        service.update_segment(service.get_lesson(lesson.id).segments[0].id, SegmentUpdate(english_display="Changed"))
    elif change == "source":
        cue = service.get_source_timeline(lesson.id).cues[0]
        service.update_source_cue(cue.id, SourceCueUpdate(expected_version=cue.edit_version, end=.39))
    elif change == "target":
        data = json.loads(review.read_text())
        data["videoId"] = "changed1234"
        private_json(review, data)
    with pytest.raises(WorkflowStateError):
        publication.publish(review, client)
    assert fake.requests == []


@pytest.mark.parametrize("change", ["account", "video", "duration"])
def test_wrong_owner_or_timeline_prevents_upload(service, wav_bytes, monkeypatch, google, change):
    fake, client = google
    _, publication, review = prepared(service, wav_bytes, monkeypatch)
    publication.approve(review, reviewed=True)
    if change == "account":
        fake.owner = "UC" + "b" * 22
    elif change == "video":
        fake.channel = "UC" + "b" * 22
    else:
        fake.duration = "PT1M"
    with pytest.raises(YouTubeError):
        publication.publish(review, client)
    assert not fake.writes


def test_processing_track_retry_waits_without_duplicate_upload(service, wav_bytes, monkeypatch, google):
    fake, client = google
    _, publication, review = prepared(service, wav_bytes, monkeypatch)
    publication.approve(review, reviewed=True)
    fake.status = "syncing"
    assert publication.publish(review, client)["status"] == "awaiting_youtube_processing"
    assert publication.publish(review, client)["status"] == "awaiting_youtube_processing"
    assert len(fake.writes) == 2
    for value in fake.tracks.values():
        value["snippet"]["status"] = "serving"
    assert publication.publish(review, client)["status"] == "verified"
    assert len(fake.writes) == 2


def test_partial_delivery_and_transport_loss_reconcile(service, wav_bytes, monkeypatch, google):
    fake, client = google
    _, publication, review = prepared(service, wav_bytes, monkeypatch)
    publication.approve(review, reviewed=True)
    fake.error_on_language = "en"
    with pytest.raises(YouTubeError) as error:
        publication.publish(review, client)
    assert "SECRET" not in str(error.value)
    receipt = json.loads((review.parent / "youtube-receipt.json").read_text())
    assert receipt["status"] == "interrupted_reconcile_required"
    assert receipt["tracks"][0]["verified"]
    fake.error_on_language = None
    fake.interrupt = True  # insert succeeds remotely but client loses the acknowledgement
    with pytest.raises(YouTubeError):
        publication.publish(review, client)
    assert publication.publish(review, client)["status"] == "verified"
    assert len(fake.writes) == 2


def test_remote_readback_must_match_text_and_timestamps(service, wav_bytes, monkeypatch, google):
    fake, client = google
    _, publication, review = prepared(service, wav_bytes, monkeypatch)
    publication.approve(review, reviewed=True)
    fake.mismatch = True
    with pytest.raises(YouTubeError, match="readback differs"):
        publication.publish(review, client)
    assert json.loads((review.parent / "youtube-receipt.json").read_text())["status"] != "verified"


def test_source_edit_during_remote_reads_blocks_write(service, wav_bytes, monkeypatch, google):
    fake, client = google
    lesson, publication, review = prepared(service, wav_bytes, monkeypatch)
    publication.approve(review, reviewed=True)
    def edit():
        fake.on_read = lambda: None
        cue = service.get_source_timeline(lesson.id).cues[0]
        service.update_source_cue(cue.id, SourceCueUpdate(expected_version=cue.edit_version, end=.39))
    fake.on_read = edit
    with pytest.raises(WorkflowStateError):
        publication.publish(review, client)
    assert not fake.writes


def test_srt_comparison_ignores_wrapping_but_not_timing():
    assert canonical_srt("1\n00:00:00,100 --> 00:00:01,200\nhello\nworld\n") == canonical_srt("8\r\n00:00:00.100 --> 00:00:01.200\r\nhello world\r\n")
    with pytest.raises(YouTubeError):
        canonical_srt("bad caption")
    assert canonical_srt("1\n00:00:00,100 --> 00:00:01,200\nhello\n") != canonical_srt("1\n00:00:00,200 --> 00:00:01,200\nhello\n")


def test_pkce_desktop_consent_and_private_refresh_storage(tmp_path):
    config = tmp_path / "client.json"
    private_json(config, {"installed": {"client_id": "FAKE_ID", "client_secret": "FAKE_SECRET", "token_uri": "https://evil.invalid"}})
    credentials = tmp_path / "private" / "youtube.json"
    authorization = {}
    threads = []
    def browser(url):
        authorization.update(parse_qs(urlparse(url).query))
        assert authorization["scope"] == [SCOPE]
        def callback():
            with pytest.raises(HTTPError) as error:
                urlopen(authorization["redirect_uri"][0] + "?state=WRONG&code=ATTACKER_CODE", timeout=5)
            assert error.value.code == 400
            with urlopen(authorization["redirect_uri"][0] + "?state=" + authorization["state"][0] + "&code=FAKE_CODE", timeout=5) as response:
                assert response.status == 200
        thread = threading.Thread(target=callback)
        thread.start()
        threads.append(thread)
        return True
    def token(request):
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        data = parse_qs(request.content.decode())
        assert data["code"] == ["FAKE_CODE"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        assert challenge == authorization["code_challenge"][0]
        return httpx.Response(200, json={"scope": SCOPE, "refresh_token": "FAKE_REFRESH", "access_token": "FAKE_ACCESS"})
    with httpx.Client(transport=httpx.MockTransport(token)) as client:
        assert connect(config, credentials, client=client, open_browser=browser, timeout=5)["connected"]
    for thread in threads:
        thread.join(timeout=5)
    assert credentials.stat().st_mode & 0o077 == 0
    assert "access_token" not in json.loads(credentials.read_text())


def test_browser_failure_does_not_create_credentials(tmp_path):
    config = tmp_path / "client.json"
    private_json(config, {"installed": {"client_id": "FAKE_ID", "client_secret": "FAKE_SECRET"}})
    credentials = tmp_path / "youtube.json"
    with pytest.raises(YouTubeError, match="consent browser"):
        connect(config, credentials, open_browser=lambda url: False)
    assert not credentials.exists()


def test_credentials_refuse_checkout_and_public_permissions(google):
    from core.config import PROJECT_ROOT
    with pytest.raises(ValueError, match="outside"):
        credential_path(PROJECT_ROOT / "data" / "youtube.json")
    fake, client = google
    client.credentials.chmod(0o644)
    with pytest.raises(YouTubeError, match="private permissions"):
        client.verify_video(VIDEO, CHANNEL, 1)
    assert not fake.requests
