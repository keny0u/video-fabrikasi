import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .. import config
from .base import AuthError, RateLimited, TransientError, UploadError

# Yükleme ve dizi oylaması için yorum okuma. Eski bağlantılar yalnızca yükleme iznine sahiptir.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
CLIENT_FILE = config.SECRETS / "youtube_client.json"
TOKEN_FILE = config.SECRETS / "youtube_token.json"
CHUNK_SIZE = 8 * 1024 * 1024
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000
CATEGORY_PEOPLE_AND_BLOGS = "22"
LIMIT_REASONS = {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
# Trafiği değiştiren programlar (GoodbyeDPI gibi) bağlantıların bir kısmını koparabiliyor; birkaç kez denenir.
NETWORK_RETRIES = 6
RETRY_PAUSE = 2


def next_quota_reset():
    """YouTube kotası Pasifik saatiyle gece yarısı sıfırlanır."""
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return midnight.timestamp() + 300


def build_body(post, shorts):
    title = post.title
    if shorts and "#shorts" not in title.lower():
        title = f"{title[:TITLE_LIMIT - 8].rstrip()} #Shorts"
    return {
        "snippet": {
            "title": title[:TITLE_LIMIT],
            "description": post.description[:DESCRIPTION_LIMIT],
            "tags": post.tags,
            "categoryId": CATEGORY_PEOPLE_AND_BLOGS,
        },
        "status": {"privacyStatus": post.privacy, "selfDeclaredMadeForKids": False},
    }


def _error_reason(error):
    try:
        return json.loads(error.content)["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def translate_error(error):
    status, reason = error.resp.status, _error_reason(error)
    if reason in LIMIT_REASONS:
        return RateLimited(f"YouTube limiti doldu ({reason})", next_quota_reset())
    if status == 401:
        return AuthError("YouTube bağlantısı geçersiz, yeniden bağlan.")
    if status >= 500:
        return TransientError(f"YouTube sunucu hatası ({status})")
    return UploadError(f"YouTube reddetti ({status} {reason})")


def connect():
    """Tarayıcıda Google izin ekranını açar ve jetonu kaydeder."""
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    credentials = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")


class YouTubeUploader:
    def __init__(self, shorts):
        self.shorts = shorts

    def connected(self):
        return TOKEN_FILE.exists()

    def _credentials(self):
        if not TOKEN_FILE.exists():
            raise AuthError("YouTube bağlı değil.")
        credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE))
        if credentials.valid:
            return credentials
        for attempt in range(NETWORK_RETRIES):
            try:
                credentials.refresh(Request())
                break
            except RefreshError as exc:
                raise AuthError(f"YouTube bağlantısı yenilenemedi, yeniden bağlan: {exc}")
            except TransportError as exc:
                if attempt == NETWORK_RETRIES - 1:
                    raise TransientError(f"Google'a bağlanılamadı: {exc}")
                time.sleep(RETRY_PAUSE)
        TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")
        return credentials

    def publish(self, post):
        service = build("youtube", "v3", credentials=self._credentials(), cache_discovery=False)
        media = MediaFileUpload(str(post.path), chunksize=CHUNK_SIZE, resumable=True)
        request = service.videos().insert(
            part="snippet,status", body=build_body(post, self.shorts), media_body=media
        )
        try:
            response, failures = None, 0
            while response is None:
                try:
                    _, response = request.next_chunk(num_retries=3)
                except (OSError, TransportError, httplib2.HttpLib2Error) as exc:
                    # Kopan parça baştan değil, kaldığı yerden yeniden gönderilir.
                    failures += 1
                    if failures >= NETWORK_RETRIES:
                        raise TransientError(f"Bağlantı koptu: {exc}")
                    time.sleep(RETRY_PAUSE)
        except HttpError as exc:
            raise translate_error(exc)
        finally:
            media.stream().close()  # Windows'ta açık dosya arşive taşınamaz
        return f"https://youtu.be/{response['id']}"
