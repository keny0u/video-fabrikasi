import subprocess

import imageio_ffmpeg
import numpy as np

SAMPLE_RATE = 16000


def load(path):
    """Sesi Whisper'ın istediği biçimde (16 kHz, tek kanal, float32) okur."""
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
            "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip()[-300:] or "Ses okunamadı.")
    return np.frombuffer(result.stdout, dtype=np.float32)


def loudness(samples, step=0.5):
    """Her step saniyelik dilimin ses yüksekliği (dB)."""
    size = int(SAMPLE_RATE * step)
    count = len(samples) // size
    if count == 0:
        return np.zeros(0)
    frames = samples[: count * size].reshape(count, size)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return 20 * np.log10(rms + 1e-6)
