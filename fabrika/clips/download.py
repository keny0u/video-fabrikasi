import re
import time

import imageio_ffmpeg
import yt_dlp

from .. import config

FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b"
# YouTube bir bilgisayarı bot sanınca giriş ister; giriş yapılmış oturumun çerezleri bunu aşar.
COOKIE_FILE = config.SECRETS / "youtube_cookies.txt"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
NETWORK_RETRIES = 6
RETRY_PAUSE = 3
NETWORK_MARKERS = ("ssl", "invalid_session_id", "timed out", "connection reset", "connection aborted",
                   "unable to download api page", "remote end closed")

FRIENDLY_ERRORS = [
    ("not a bot", "YouTube bu bilgisayarı bot sandı. Hesaplar → YouTube bölümünden cookies.txt yükle, "
                  "birkaç saat bekle ya da videoyu 'Bilgisayardan dosya' ile ver."),
    ("video is unavailable", "Video kullanılamıyor: kaldırılmış, gizli ya da bölgende erişime kapalı olabilir."),
    ("private video", "Bu video gizli; yalnızca sahibi izleyebilir."),
    ("members-only", "Bu video yalnızca kanal üyelerine açık."),
    ("confirm your age", "Yaş doğrulaması isteyen bir video. cookies.txt yükleyince indirilebilir."),
]


def _options(extra=None):
    options = {
        "quiet": True,
        "no_warnings": True,
        "color": "no_color",
        # YouTube imza çözümü için JavaScript çalıştırıcısı gerekir; bilgisayarda Node var.
        "js_runtimes": {"node": {}},
        **(extra or {}),
    }
    if COOKIE_FILE.exists():
        options["cookiefile"] = str(COOKIE_FILE)
    return options


def friendly_error(exc):
    """yt-dlp hatasını renk kodlarından arındırıp ne yapılacağını söyleyen Türkçe mesaja çevirir."""
    message = ANSI.sub("", str(exc)).replace("ERROR: ", "").strip()
    lowered = message.casefold()
    for marker, advice in FRIENDLY_ERRORS:
        if marker in lowered:
            return advice
    return message


def _is_network_error(exc):
    text = str(exc).casefold()
    return any(marker in text for marker in NETWORK_MARKERS)


def _extract(url, options, download, error_prefix):
    """Bağlantı koparsa (GoodbyeDPI gibi programlar yüzünden) birkaç kez yeniden dener."""
    for attempt in range(NETWORK_RETRIES):
        try:
            with yt_dlp.YoutubeDL(options) as client:
                return client.extract_info(url, download=download, process=download)
        except yt_dlp.utils.DownloadError as exc:
            if attempt == NETWORK_RETRIES - 1 or not _is_network_error(exc):
                raise ValueError(f"{error_prefix}: {friendly_error(exc)}")
            time.sleep(RETRY_PAUSE)


def download(url, workdir, on_progress=None):
    """Videoyu en fazla 1080p olarak indirir; (dosya yolu, başlık, kanal adı) döner."""

    def hook(status):
        if on_progress and status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            if total:
                on_progress(status.get("downloaded_bytes", 0) / total)

    options = _options({
        "format": FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "kaynak.%(ext)s"),
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "noplaylist": True,
        "progress_hooks": [hook],
        "retries": NETWORK_RETRIES,
        "fragment_retries": NETWORK_RETRIES,
    })
    info = _extract(url, options, download=True, error_prefix="Video indirilemedi")
    files = sorted(workdir.glob("kaynak.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise ValueError("Video indirilemedi: dosya oluşmadı.")
    return files[0], info.get("title") or "Video", info.get("channel") or info.get("uploader") or ""


def channel_name(url):
    """Önceden indirilmiş videonun kanal adı; bulunamazsa boş."""
    try:
        info = _extract(url, _options(), download=False, error_prefix="Kanal adı alınamadı")
    except ValueError:
        return ""
    return info.get("channel") or info.get("uploader") or ""


def looks_like_cookie_file(text):
    """Netscape biçiminde, içinde YouTube çerezi olan bir cookies.txt mi?"""
    rows = [line.split("\t") for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return any(len(row) >= 7 and "youtube.com" in row[0] for row in rows)
