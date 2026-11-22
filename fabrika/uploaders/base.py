import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Post:
    path: Path
    title: str
    description: str = ""
    tags: list = field(default_factory=list)
    privacy: str = "public"

    @property
    def caption(self):
        written = self.description.casefold()
        hashtags = " ".join(f"#{tag.lstrip('#')}" for tag in self.tags
                            if f"#{tag.lstrip('#')}".casefold() not in written)
        return "\n\n".join(part for part in (self.title, self.description, hashtags) if part)


class UploadError(Exception):
    """Tekrar denemenin işe yaramadığı kalıcı hata."""


class TransientError(UploadError):
    """Geçici hata; bekleyip tekrar denenir."""


class AuthError(UploadError):
    """Hesap bağlantısı geçersiz; hedef elle devam edilene kadar durur."""


class RateLimited(UploadError):
    """Platform limiti doldu; hedef retry_at zamanına kadar durur."""

    def __init__(self, message, retry_at):
        super().__init__(message)
        self.retry_at = retry_at


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    """Yazım yarıda kesilirse eski dosya bozulmasın diye geçici dosyadan değiştirir."""
    temporary = Path(f"{path}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
