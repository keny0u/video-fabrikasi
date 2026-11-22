import hashlib
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .. import config
from .base import AuthError, RateLimited, TransientError, UploadError, read_json, write_json

CONFIG_FILE = config.SECRETS / "tiktok.json"
API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
REDIRECT_PORT = 3455
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback/"
SCOPE = "video.upload,video.publish"

MB = 1024 * 1024
CHUNK_SIZE = 10 * MB
TITLE_LIMIT = 2200
STATUS_POLLS = 40
POLL_SECONDS = 15
TOKEN_MARGIN = 120
CONTENT_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
PRIVACY_LEVELS = {
    "public": "PUBLIC_TO_EVERYONE",
    "private": "SELF_ONLY",
    "unlisted": "SELF_ONLY",
}
LIMIT_CODES = {"spam_risk_too_many_posts", "spam_risk_too_many_pending_share", "reached_active_user_cap"}
AUTH_CODES = {"access_token_invalid", "scope_not_authorized"}
RETRYABLE_FAILURES = {"internal"}


def chunk_plan(size):
    """Parça sayısı aşağı yuvarlanır, artan baytlar son parçaya eklenir; küçük dosya tek parça gider."""
    if size <= CHUNK_SIZE:
        return size, 1
    return CHUNK_SIZE, size // CHUNK_SIZE


def chunk_ranges(size, chunk_size, count):
    for index in range(count):
        start = index * chunk_size
        end = size - 1 if index == count - 1 else start + chunk_size - 1
        yield start, end


def code_challenge(verifier):
    # TikTok masaüstü akışı standart base64url yerine hex SHA256 ister.
    return hashlib.sha256(verifier.encode()).hexdigest()


def _save_tokens(settings, data):
    now = time.time()
    settings.update(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=now + data["expires_in"],
        refresh_expires_at=now + data["refresh_expires_in"],
    )
    write_json(CONFIG_FILE, settings)


def _token_request(settings, **fields):
    response = requests.post(
        f"{API}/oauth/token/",
        data={
            "client_key": settings["client_key"],
            "client_secret": settings["client_secret"],
            **fields,
        },
        timeout=30,
    )
    data = response.json()
    if "access_token" not in data:
        raise AuthError(f"TikTok jetonu alınamadı: {data.get('error_description') or data}")
    return data


def connect():
    """Tarayıcıda TikTok izin ekranını açar, dönüşü localhost'ta yakalar."""
    settings = read_json(CONFIG_FILE)
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(16)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            result.update({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Tamam, bu sekmeyi kapatabilirsin.".encode())

        def log_message(self, *args):
            pass

    query = urlencode({
        "client_key": settings["client_key"],
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "response_type": "code",
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
    })
    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Callback)
    webbrowser.open(f"{AUTH_URL}?{query}")
    print(f"Tarayıcı açılmadıysa bu adrese git:\n{AUTH_URL}?{query}")
    server.handle_request()
    server.server_close()

    if result.get("state") != state or "code" not in result:
        raise AuthError(f"TikTok izni alınamadı: {result.get('error_description') or result}")
    _save_tokens(settings, _token_request(
        settings,
        code=result["code"],
        grant_type="authorization_code",
        redirect_uri=REDIRECT_URI,
        code_verifier=verifier,
    ))


def is_connected():
    return CONFIG_FILE.exists() and "refresh_token" in read_json(CONFIG_FILE)


class TikTokUploader:
    _token_lock = threading.Lock()

    def connected(self):
        return is_connected()

    def _access_token(self):
        if not self.connected():
            raise AuthError("TikTok bağlı değil.")
        with self._token_lock:
            settings = read_json(CONFIG_FILE)
            if time.time() < settings["expires_at"] - TOKEN_MARGIN:
                return settings["access_token"]
            # Yenileme jetonu her kullanımda değişir; yenisi hemen kaydedilmeli.
            _save_tokens(settings, _token_request(
                settings, grant_type="refresh_token", refresh_token=settings["refresh_token"]
            ))
            return settings["access_token"]

    def _call(self, path, body=None):
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        try:
            response = requests.post(f"{API}{path}", headers=headers, json=body, timeout=60)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TransientError(f"TikTok'a ulaşılamadı: {exc}")
        code = payload.get("error", {}).get("code", "ok")
        if code == "ok":
            return payload["data"]
        message = f"TikTok: {code}"
        if code in LIMIT_CODES:
            raise RateLimited(message, time.time() + 86400)
        if code in AUTH_CODES:
            raise AuthError(message)
        if code == "rate_limit_exceeded" or response.status_code >= 500:
            raise TransientError(message)
        raise UploadError(f"{message} ({payload['error'].get('message', '')})")

    def _init_body(self, post, size, settings):
        chunk_size, count = chunk_plan(size)
        body = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk_size,
                "total_chunk_count": count,
            }
        }
        if settings.get("mode") == "direct":
            creator = self._call("/post/publish/creator_info/query/")
            privacy = PRIVACY_LEVELS.get(post.privacy, "SELF_ONLY")
            if privacy not in creator["privacy_level_options"]:
                privacy = "SELF_ONLY"
            body["post_info"] = {"title": post.caption[:TITLE_LIMIT], "privacy_level": privacy}
        return body

    def _upload(self, url, path, size, chunk_size, count):
        content_type = CONTENT_TYPES.get(path.suffix.lower(), "video/mp4")
        with open(path, "rb") as file:
            for start, end in chunk_ranges(size, chunk_size, count):
                chunk = file.read(end - start + 1)
                try:
                    response = requests.put(
                        url,
                        data=chunk,
                        headers={
                            "Content-Type": content_type,
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{end}/{size}",
                        },
                        timeout=600,
                    )
                except requests.RequestException as exc:
                    raise TransientError(f"TikTok yüklemesi koptu: {exc}")
                if response.status_code not in (201, 206):
                    raise TransientError(f"TikTok parça yüklemesi başarısız ({response.status_code})")

    def _wait(self, publish_id):
        for _ in range(STATUS_POLLS):
            data = self._call("/post/publish/status/fetch/", {"publish_id": publish_id})
            status = data["status"]
            if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                return
            if status == "FAILED":
                reason = data.get("fail_reason", "")
                if reason in RETRYABLE_FAILURES:
                    raise TransientError(f"TikTok işleyemedi: {reason}")
                if reason == "spam_risk_too_many_posts":
                    raise RateLimited(f"TikTok: {reason}", time.time() + 86400)
                raise UploadError(f"TikTok işleyemedi: {reason}")
            time.sleep(POLL_SECONDS)
        raise UploadError("TikTok işlemi zamanında bitmedi. Hesabı kontrol et, tekrar denemeden önce yayınlanmadığına bak.")

    def publish(self, post):
        settings = read_json(CONFIG_FILE) if CONFIG_FILE.exists() else {}
        size = post.path.stat().st_size
        body = self._init_body(post, size, settings)
        endpoint = "video/init/" if settings.get("mode") == "direct" else "inbox/video/init/"
        data = self._call(f"/post/publish/{endpoint}", body)
        source = body["source_info"]
        self._upload(data["upload_url"], post.path, size, source["chunk_size"], source["total_chunk_count"])
        self._wait(data["publish_id"])
        return ""
