import re
import subprocess
from dataclasses import dataclass

import imageio_ffmpeg

from . import config

FFMPEG_TIMEOUT = 600
MB = 1024 * 1024
GB = 1024 * MB


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    fps: float
    size: int


def _run(args):
    return subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT,
        encoding="utf-8",
        errors="replace",
    )


def probe(path):
    """Videonun boyut, süre ve fps bilgisini okur; video değilse None döner."""
    output = _run(["-i", str(path)]).stderr
    size_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", output)
    duration_match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not size_match or not duration_match:
        return None
    width, height = int(size_match[1]), int(size_match[2])
    if re.search(r"rotation of -?(90|270)", output):
        width, height = height, width
    hours, minutes, seconds = duration_match.groups()
    fps_match = re.search(r"(\d+(?:\.\d+)?) fps", output)
    return VideoInfo(
        width=width,
        height=height,
        duration=int(hours) * 3600 + int(minutes) * 60 + float(seconds),
        fps=float(fps_match[1]) if fps_match else 0.0,
        size=path.stat().st_size,
    )


def image_to_video(image, output, vertical, seconds=config.IMAGE_SECONDS):
    """Görseli, oranını bozmadan siyah kenarlarla sessiz bir MP4'e çevirir."""
    width, height = (1080, 1920) if vertical else (1920, 1080)
    frames = seconds * 30
    # -loop girişi bozuk görselde sonsuza kadar döner; tek kare okuyup filtreyle uzatılır.
    fit = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p,"
        f"loop=loop={frames - 1}:size=1:start=0,fps=30"
    )
    try:
        result = _run([
            "-y", "-i", str(image),
            "-f", "lavfi", "-t", str(seconds), "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf", fit, "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(output),
        ])
    except subprocess.TimeoutExpired:
        raise ValueError("ffmpeg zaman aşımına uğradı.")
    if result.returncode != 0:
        raise ValueError(result.stderr.strip()[-300:])


def check(target, info, suffix):
    """Dosya hedef platforma uymuyorsa nedenini döner, uyuyorsa None."""
    if info is None:
        return "Video okunamadı, dosya bozuk olabilir."
    if target == "youtube_shorts":
        if info.width > info.height:
            return f"Shorts için dikey ya da kare video gerekir ({info.width}x{info.height})."
        if info.duration > 180:
            return f"Shorts en fazla 3 dakika olabilir ({info.duration:.0f} sn)."
    if target == "tiktok":
        if info.duration > 600:
            return f"TikTok API en fazla 10 dakika kabul eder ({info.duration:.0f} sn)."
        if info.size > 4 * GB:
            return "TikTok API en fazla 4 GB kabul eder."
        if min(info.width, info.height) < 360 or max(info.width, info.height) > 4096:
            return f"TikTok kenar başına 360-4096 piksel ister ({info.width}x{info.height})."
    if target == "instagram_reels":
        if suffix not in {".mp4", ".mov"}:
            return "Reels yalnızca MP4 ve MOV kabul eder."
        if not 3 <= info.duration <= 900:
            return f"Reels 3 saniye ile 15 dakika arası olmalı ({info.duration:.0f} sn)."
        if info.size > 300 * MB:
            return "Reels API en fazla 300 MB kabul eder."
    if target in {"tiktok", "instagram_reels"} and info.fps and not 23 <= info.fps <= 60:
        return f"Kare hızı 23-60 fps arası olmalı ({info.fps:g} fps)."
    return None
