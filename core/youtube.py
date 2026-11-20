"""Explicit desktop OAuth and native YouTube caption delivery.

No model/provider credentials are used. All URLs are fixed Google endpoints;
token responses and request bodies never appear in errors or application logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from core.artifacts import private_json
from core.config import PROJECT_ROOT
from core.runtime import file_lock

SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/captions"
DEFAULT_CREDENTIALS = Path.home() / ".config" / "kolmud" / "youtube.json"


class YouTubeError(RuntimeError):
    pass


def credential_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError("Keep YouTube credentials outside the source checkout.")
    return path


def _request(client, method, url, *, allowed_statuses=(), **kwargs):
    try:
        response = client.request(method, url, follow_redirects=False, **kwargs)
    except httpx.HTTPError:
        raise YouTubeError("Google request interrupted; retry the command to reconcile remote state.") from None
    if not response.is_success and response.status_code not in allowed_statuses:
        raise YouTubeError(f"Google request failed (HTTP {response.status_code}); check account permissions/quota and retry.")
    return response


def _json(response):
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise YouTubeError("Google returned an invalid response.") from None


def connect(client_config: Path, credentials: Path = DEFAULT_CREDENTIALS, *, client=None,
            open_browser=webbrowser.open, timeout=180) -> dict:
    """One explicit interactive consent; PKCE and a single-use loopback callback."""
    credentials = credential_path(credentials)
    config = json.loads(client_config.read_text(encoding="utf-8")).get("installed", {})
    if not config.get("client_id") or not config.get("client_secret"):
        raise ValueError("Use a Google OAuth Desktop app client JSON file.")
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # OAuth callback query contains a secret authorization code.

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            valid = parsed.path == "/oauth2callback" and secrets.compare_digest(query.get("state", [""])[0], state)
            if valid:
                result.update(code=query.get("code", [""])[0], error=bool(query.get("error")))
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"You may close this window." if valid else b"Invalid authorization callback.")

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2callback"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": config["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        if not open_browser(url):
            raise YouTubeError("Could not open the consent browser; configure the desktop browser and retry.")
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if result.get("error") or not result.get("code"):
        raise YouTubeError("YouTube connection was declined or timed out; no credentials saved.")
    owned_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        tokens = _json(_request(client, "POST", TOKEN_URL, data={
            "client_id": config["client_id"], "client_secret": config["client_secret"],
            "code": result["code"], "code_verifier": verifier, "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"}))
        if not tokens.get("refresh_token") or SCOPE not in tokens.get("scope", "").split():
            raise YouTubeError("Offline YouTube caption permission was not granted; reconnect with full consent.")
        credentials.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_json(credentials, {"client_id": config["client_id"], "client_secret": config["client_secret"],
                                   "refresh_token": tokens["refresh_token"], "scope": SCOPE})
        return {"connected": True, "credentialsPath": str(credentials), "scope": SCOPE}
    finally:
        if owned_client:
            client.close()


def canonical_srt(text: str) -> list[tuple[str, str, str]]:
    """Compare text and exact millisecond anchors, ignoring SRT wrapping/indices."""
    cues = []
    for block in re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if len(lines) < 2:
            raise YouTubeError("Invalid caption content returned by YouTube.")
        match = re.fullmatch(r"(\d{2,}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(\d{2,}:\d{2}:\d{2}[,.]\d{3})", lines[0].strip())
        if not match:
            raise YouTubeError("Caption timestamps could not be verified.")
        cues.append((match[1].replace(".", ","), match[2].replace(".", ","), " ".join(" ".join(lines[1:]).split())))
    if not cues:
        raise YouTubeError("Caption track is empty.")
    return cues


def video_duration(value: str) -> float:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value)
    if not match or not any(match.groups()):
        raise YouTubeError("Video duration is unavailable; verify the selected source video.")
    return sum(float(v or 0) * scale for v, scale in zip(match.groups(), (3600, 60, 1)))


class CaptionClient:
    def __init__(self, credentials: Path = DEFAULT_CREDENTIALS, *, client=None):
        self.credentials = credential_path(credentials)
        self.client = client or httpx.Client(timeout=60)
        self.owns_client = client is None
        self.token = None

    def close(self):
        if self.owns_client:
            self.client.close()

    def _access_token(self):
        if self.token:
            return self.token
        try:
            if self.credentials.stat().st_mode & 0o077:
                raise YouTubeError("YouTube credentials must have private permissions (chmod 600).")
            saved = json.loads(self.credentials.read_text(encoding="utf-8"))
            data = {key: saved[key] for key in ("client_id", "client_secret", "refresh_token")}
        except (OSError, ValueError, KeyError):
            raise YouTubeError("Connect YouTube first with kolmud youtube-connect.") from None
        tokens = _json(_request(self.client, "POST", TOKEN_URL, data={**data, "grant_type": "refresh_token"}))
        self.token = tokens.get("access_token")
        if not self.token:
            raise YouTubeError("YouTube did not return an access token; reconnect the account.")
        if tokens.get("refresh_token"):
            private_json(self.credentials, {**saved, "refresh_token": tokens["refresh_token"]})
        return self.token

    def _call(self, method, url, **kwargs):
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self._access_token()}"}
        return _request(self.client, method, url, headers=headers, **kwargs)

    def verify_channel(self, channel_id):
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
            raise ValueError("Use the exact YouTube channel ID (UC…).")
        channels = _json(self._call("GET", API + "/channels", params={"part": "id", "mine": "true", "maxResults": 50}))
        if channel_id not in {c["id"] for c in channels.get("items", [])}:
            raise YouTubeError("Connected account does not own the selected channel.")

    def verify_video(self, video_id, channel_id, duration, *, expected_privacy=None, expected_title=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Use the exact YouTube video ID.")
        self.verify_channel(channel_id)
        parts = "snippet,contentDetails,status" if expected_privacy else "snippet,contentDetails"
        videos = _json(self._call("GET", API + "/videos", params={"part": parts, "id": video_id}))
        items = videos.get("items", [])
        if len(items) != 1 or items[0].get("snippet", {}).get("channelId") != channel_id:
            raise YouTubeError("Selected video does not belong to the connected channel.")
        if abs(video_duration(items[0].get("contentDetails", {}).get("duration", "")) - duration) > 2:
            raise YouTubeError("YouTube video length differs from the local source; review the video selection.")
        if expected_privacy and items[0].get("status", {}).get("privacyStatus") != expected_privacy:
            raise YouTubeError("Uploaded video privacy does not match the reviewed private upload; inspect Studio.")
        if expected_title and items[0].get("snippet", {}).get("title") != expected_title:
            raise YouTubeError("Uploaded video title differs from the reviewed title; inspect Studio.")

    @staticmethod
    def _upload_session(url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com" or parsed.port is not None or parsed.username or parsed.fragment or parsed.path != "/upload/youtube/v3/videos":
            raise YouTubeError("Google returned an unexpected upload destination; no media was sent.")
        return url

    def upload_video(self, path: Path, metadata: dict, state: dict, checkpoint, *, before_write=lambda: None):
        """Bounded chunks; save session before media, query it after lost acknowledgements.

        An expired session is an error, never permission to create another video.
        Its URI stays in the private receipt and is not returned to CLI stdout.
        """
        if state.get("videoId"):
            return state["videoId"]
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("Generated video is empty.")
        if not state.get("uploadSession"):
            before_write()
            response = self._call("POST", "https://www.googleapis.com/upload/youtube/v3/videos",
                                  params={"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "false"},
                                  json=metadata, headers={"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
            location = response.headers.get("Location", "")
            state["uploadSession"] = self._upload_session(location)
            state["uploadBytes"] = size
            checkpoint()
        if state.get("uploadBytes") != size:
            raise YouTubeError("Video bytes changed after upload started; reconcile this upload before preparing a new one.")
        session = self._upload_session(state["uploadSession"])
        response = self._call("PUT", session, headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"},
                              content=b"", allowed_statuses=(308,))
        while True:
            if response.status_code in (200, 201):
                video_id = _json(response).get("id", "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                    raise YouTubeError("Upload completed without a valid video ID; retry to reconcile the saved session.")
                state["videoId"] = video_id
                state.pop("uploadSession", None)
                state["uploadOffset"] = size
                checkpoint()
                return video_id
            acknowledged = response.headers.get("Range")
            if acknowledged:
                match = re.fullmatch(r"bytes=0-(\d+)", acknowledged)
                if not match:
                    raise YouTubeError("Google returned an invalid upload offset; retry to query the saved session.")
                offset = int(match[1]) + 1
            else:
                offset = 0
            if not 0 <= offset < size:
                raise YouTubeError("Google has not returned a completed video ID; retry the saved session, without starting another upload.")
            before_write()
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(8 * 1024 * 1024)  # a multiple of Google's 256 KiB chunk unit
            state["uploadOffset"] = offset
            checkpoint()
            response = self._call("PUT", session, content=chunk, allowed_statuses=(308,),
                                  headers={"Content-Type": "video/mp4", "Content-Length": str(len(chunk)),
                                           "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}"})
            if response.status_code == 308:
                match = re.fullmatch(r"bytes=0-(\d+)", response.headers.get("Range", ""))
                if not match or int(match[1]) + 1 <= offset:
                    raise YouTubeError("Upload made no progress; retry the saved session later.")

    def tracks(self, video_id):
        return _json(self._call("GET", API + "/captions", params={"part": "snippet", "videoId": video_id})).get("items", [])

    def download(self, track_id):
        if not isinstance(track_id, str) or not track_id or len(track_id) > 1024:
            raise YouTubeError("Invalid caption track identifier.")
        return self._call("GET", API + "/captions/" + quote(track_id, safe=""), params={"tfmt": "srt"}).text

    def deliver(self, video_id, language, content, *, before_write=lambda: None):
        if language not in ("he", "en"):
            raise ValueError("Only prepared Hebrew/English caption tracks are supported.")
        expected = canonical_srt(content)
        name = f"Kolmud Studio ({language})"
        matches = [track for track in self.tracks(video_id) if track.get("snippet", {}).get("language") == language
                   and track.get("snippet", {}).get("name") == name]
        if len(matches) > 1:
            raise YouTubeError("Multiple managed tracks found; resolve the duplicate tracks in Studio before retrying.")
        current = matches[0] if matches else None
        if current and current["snippet"].get("status") == "syncing":
            return {"language": language, "trackId": current["id"], "status": "processing", "verified": False}
        if current and current["snippet"].get("status") == "serving" and canonical_srt(self.download(current["id"])) == expected and not current["snippet"].get("isDraft", False):
            return {"language": language, "trackId": current["id"], "status": "serving", "action": "unchanged", "verified": True}
        metadata = ({"id": current["id"], "snippet": {"isDraft": False}} if current else
                    {"snippet": {"videoId": video_id, "language": language, "name": name, "isDraft": False}})
        boundary = "kolmud_" + secrets.token_hex(20)
        payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" +
                   json.dumps(metadata) + f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n" +
                   content + f"\r\n--{boundary}--\r\n").encode("utf-8")
        before_write()  # local source/translation/review checks after network reads
        result = _json(self._call("PUT" if current else "POST", UPLOAD,
                                 params={"uploadType": "multipart", "part": "snippet"}, content=payload,
                                 headers={"Content-Type": f"multipart/related; boundary={boundary}"}))
        track_id = result.get("id")
        if not track_id:
            raise YouTubeError("Caption upload returned no track ID; retry to reconcile remote state.")
        # Read the remote resource again; an upload acknowledgement is not publication proof.
        remote = next((t for t in self.tracks(video_id) if t["id"] == track_id), None)
        status = remote.get("snippet", {}).get("status", "processing") if remote else "processing"
        if status == "failed":
            raise YouTubeError("YouTube rejected the caption track; inspect its processing status in Studio.")
        verified = False
        if status == "serving":
            verified = (not remote["snippet"].get("isDraft", False) and canonical_srt(self.download(track_id)) == expected)
            if not verified:
                raise YouTubeError("Uploaded caption readback differs from the reviewed text/timestamps; inspect Studio before retrying.")
        return {"language": language, "trackId": track_id, "status": status,
                "action": "updated" if current else "inserted", "verified": verified}

    def publication_lock(self, video_id):
        # Shared across data directories for this account; independent CLI instances cannot duplicate inserts.
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Invalid YouTube video ID.")
        return file_lock(self.credentials.parent / f".youtube-{video_id}.lock")
