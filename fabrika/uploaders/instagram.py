import time

import requests

from .. import config
from .base import AuthError, RateLimited, TransientError, UploadError, read_json

CONFIG_FILE = config.SECRETS / "instagram.json"
GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"
DEFAULT_VERSION = "v27.0"
CAPTION_LIMIT = 2200
STATUS_POLLS = 10  # uzun videolar için Meta'nın önerdiği 5 dakikadan biraz uzun
POLL_SECONDS = 60
RATE_CODES = {4, 17, 341}
AUTH_CODES = {102, 190}
TEMPORARY_CODES = {1, 2}


def translate_error(response):
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    code, message = error.get("code"), error.get("message", response.text[:200])
    if code in RATE_CODES:
        return RateLimited(f"Instagram limiti doldu: {message}", time.time() + 3600)
    if code in AUTH_CODES:
        return AuthError(f"Instagram jetonu geçersiz, yenile: {message}")
    if code in TEMPORARY_CODES or response.status_code >= 500:
        return TransientError(f"Instagram geçici hata: {message}")
    return UploadError(f"Instagram reddetti ({code}): {message}")


class InstagramUploader:
    def connected(self):
        return CONFIG_FILE.exists()

    def _settings(self):
        if not self.connected():
            raise AuthError("Instagram bağlı değil.")
        settings = read_json(CONFIG_FILE)
        settings.setdefault("api_version", DEFAULT_VERSION)
        return settings

    def _request(self, method, url, **kwargs):
        try:
            response = requests.request(method, url, timeout=kwargs.pop("timeout", 60), **kwargs)
        except requests.RequestException as exc:
            raise TransientError(f"Instagram'a ulaşılamadı: {exc}")
        if not response.ok:
            raise translate_error(response)
        return response.json()

    def _wait(self, base, container, token):
        for _ in range(STATUS_POLLS):
            time.sleep(POLL_SECONDS)
            status = self._request(
                "GET", f"{base}/{container}",
                params={"fields": "status_code", "access_token": token},
            ).get("status_code")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise UploadError(f"Instagram videoyu işleyemedi ({status}).")
        raise TransientError("Instagram işlemesi zamanında bitmedi.")

    def publish(self, post):
        settings = self._settings()
        version, token, ig_id = settings["api_version"], settings["access_token"], settings["ig_user_id"]
        base = f"{GRAPH}/{version}"
        container = self._request(
            "POST", f"{base}/{ig_id}/media",
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": post.caption[:CAPTION_LIMIT],
                "share_to_feed": "true",
                "access_token": token,
            },
        )["id"]
        with open(post.path, "rb") as file:
            self._request(
                "POST", f"{RUPLOAD}/{version}/{container}",
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(post.path.stat().st_size),
                },
                data=file,
                timeout=1800,
            )
        self._wait(base, container, token)
        try:
            media_id = self._request(
                "POST", f"{base}/{ig_id}/media_publish",
                data={"creation_id": container, "access_token": token},
            )["id"]
        except TransientError as exc:
            # Yanıt gelmese de yayınlanmış olabilir; otomatik tekrar çift gönderi yapar.
            raise UploadError(f"Yayın sonucu belirsiz, Instagram'ı kontrol et: {exc}")
        try:
            details = self._request(
                "GET", f"{base}/{media_id}",
                params={"fields": "permalink", "access_token": token},
            )
        except UploadError:
            return "", media_id  # yayınlandı; yalnızca bağlantı okunamadı
        return details.get("permalink", ""), media_id
