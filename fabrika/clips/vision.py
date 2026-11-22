"""Videoyu baştan sona küçük kareler halinde izler; ani hareketleri ve sahne değişimlerini ölçer."""
import subprocess
from dataclasses import dataclass

import imageio_ffmpeg
import numpy as np

SCAN_FPS = 4
SCAN_WIDTH, SCAN_HEIGHT = 256, 144
PROGRESS_EVERY = 200


@dataclass
class Motion:
    step: float
    camera: np.ndarray  # kamera kutusundaki kare farkı: irkilme, sıçrama
    game: np.ndarray  # kamera dışındaki kare farkı: ani sahne değişimi, jumpscare


def _command(path, gpu):
    return [
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
        *(["-hwaccel", "cuda"] if gpu else []),
        "-i", str(path),
        "-vf", f"fps={SCAN_FPS},scale={SCAN_WIDTH}:{SCAN_HEIGHT},format=gray",
        "-f", "rawvideo", "-",
    ]


def _box_mask(box):
    mask = np.zeros((SCAN_HEIGHT, SCAN_WIDTH), dtype=bool)
    x, y, w, h = box
    mask[int(y * SCAN_HEIGHT): int((y + h) * SCAN_HEIGHT) + 1, int(x * SCAN_WIDTH): int((x + w) * SCAN_WIDTH) + 1] = True
    return mask


def scan(path, duration, camera_boxes=(), on_progress=None):
    """Kareleri akış halinde okur; bellekte yalnızca bir önceki kare tutulur.

    Kamera sahneye göre köşe değiştirebildiği için olası her kutu ayrı ölçülür, en hareketlisi alınır.
    """
    camera_masks = [_box_mask(box) for box in camera_boxes]
    game_mask = ~np.any(camera_masks, axis=0) if camera_masks else np.ones((SCAN_HEIGHT, SCAN_WIDTH), bool)
    frame_size = SCAN_WIDTH * SCAN_HEIGHT
    for gpu in (True, False):
        process = subprocess.Popen(_command(path, gpu), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        camera, game, previous, count = [], [], None, 0
        while True:
            data = process.stdout.read(frame_size)
            if len(data) < frame_size:
                break
            frame = np.frombuffer(data, np.uint8).reshape(SCAN_HEIGHT, SCAN_WIDTH).astype(np.int16)
            if previous is not None:
                difference = np.abs(frame - previous)
                camera.append(max((float(difference[m].mean()) for m in camera_masks), default=0.0))
                game.append(float(difference[game_mask].mean()))
            previous = frame
            count += 1
            if on_progress and duration and count % PROGRESS_EVERY == 0:
                on_progress(min(count / SCAN_FPS / duration, 1.0))
        process.wait()
        if count > 1:
            # İlk karenin farkı yok; diziler zaman ekseniyle hizalı kalsın diye başa sıfır eklenir.
            return Motion(1 / SCAN_FPS, np.array([0.0] + camera), np.array([0.0] + game))
    raise ValueError("Video taranamadı.")
