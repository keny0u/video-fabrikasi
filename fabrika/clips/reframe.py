"""Yatay videodan dikey kesit çıkarırken konuşan yüzü kadrajda tutar."""
import subprocess
from dataclasses import dataclass

import cv2
import imageio_ffmpeg
import numpy as np

from .. import media

SAMPLE_STEP = 0.25
DETECT_WIDTH = 640
# Oyun yayınlarındaki köşe kameraları gibi küçük yüzler takip edilmez; kadraj oraya yakınlaşmasın.
MIN_FACE = int(DETECT_WIDTH * 0.09)
MIN_FACE_RATIO = 0.3  # yüzün bulunduğu örnek oranı bundan azsa bulanık arka plana geçilir
SMOOTHING = 0.15
DEAD_ZONE = 0.04  # kadraj küçük kıpırdanmalarda oynamasın
VERTICAL_RATIO = 9 / 16
# "split": oyun ortadan kırpılıp alanı doldurur; "split_fit": oyunun tamamı görünür.
SPLIT_LAYOUTS = ("split", "split_fit")

# Kamera kutusundaki küçük yüzleri bulmak için daha büyük çözünürlükte, küçük yüzlere izin vererek bakılır.
LOCATE_WIDTH = 960
LOCATE_FRAMES = 10
LOCATE_MIN_FACE = 18
SAME_BOX = 0.02
BORDER_STEP = 2
CAMERA_INSET = 0.01
# Gerçek yayın ölçümleri: kamera çerçevesi 4,6-18 arası, kamerasız giriş 3,4 puan aldı.
STRONG_BORDER = 6.0
WEAK_BORDER = 3.5
MIN_FACE_FRAMES = 2

_detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


def camera_candidates(box):
    """Yayınlarda kamera genelde köşeler arasında yer değiştirir: çizilen yer ve aynalı karşılıkları."""
    x, y, w, h = box
    variants = [(x, y), (1 - x - w, y), (x, 1 - y - h), (1 - x - w, 1 - y - h)]
    candidates = []
    for cx, cy in variants:
        if all(abs(cx - ox) > SAME_BOX or abs(cy - oy) > SAME_BOX for ox, oy, _, _ in candidates):
            candidates.append((cx, cy, w, h))
    return candidates


def _clip_frames(path, start, end, width, height):
    """Klip boyunca eşit aralıklı birkaç gri kare."""
    small_height = round(height * LOCATE_WIDTH / width / 2) * 2
    duration = max(end - start, 0.5)
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path),
            "-vf", f"fps={LOCATE_FRAMES / duration:.4f},scale={LOCATE_WIDTH}:{small_height},format=gray",
            "-f", "rawvideo", "-",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=media.FFMPEG_TIMEOUT,
    )
    frame_size = LOCATE_WIDTH * small_height
    count = len(result.stdout) // frame_size
    return np.frombuffer(result.stdout[: count * frame_size], np.uint8).reshape(count, small_height, LOCATE_WIDTH)


def _face_points(frame):
    """Karedeki yüzlerin merkezleri (0-1 oranında)."""
    height, width = frame.shape
    faces = _detector.detectMultiScale(frame, scaleFactor=1.1, minNeighbors=5,
                                       minSize=(LOCATE_MIN_FACE, LOCATE_MIN_FACE))
    return [((x + w / 2) / width, (y + h / 2) / height) for x, y, w, h in faces]


def _widen(edges, axis):
    """Kenarı komşu piksellere yayar; adımlı arama kenarı bir piksel ıskalasa da yakalasın."""
    before = np.roll(edges, 1, axis=axis)
    after = np.roll(edges, -1, axis=axis)
    return np.maximum(edges, np.maximum(before, after))


def _border_search(frames, box):
    """Çizilen boyuttaki, kenarları klip boyunca hiç kıpırdamayan dikdörtgeni arar.

    Kamera kutusu oyunun üstüne yapıştırılmış sabit bir çerçevedir: oyun değiştikçe kutunun
    kenarlarındaki keskin geçiş yerinde kalır. Kareler arası medyan kenar gücü, hareket eden
    oyun kenarlarını bastırır, sabit çerçeveyi bırakır. Ekranın kenarına yaslı taraflar sayılmaz.
    """
    frames = frames.astype(np.float32)
    _, height, width = frames.shape
    vertical = _widen(np.median(np.abs(np.diff(frames, axis=2)), axis=0), axis=1)  # (H, W-1)
    horizontal = _widen(np.median(np.abs(np.diff(frames, axis=1)), axis=0), axis=0)  # (H-1, W)
    down = np.vstack([np.zeros((1, width - 1)), np.cumsum(vertical, axis=0)])
    across = np.hstack([np.zeros((height - 1, 1)), np.cumsum(horizontal, axis=1)])
    box_w, box_h = round(box[2] * width), round(box[3] * height)
    xs = np.arange(0, width - box_w + 1, BORDER_STEP)
    ys = np.arange(0, height - box_h + 1, BORDER_STEP)
    X, Y = np.meshgrid(xs, ys)
    total, sides = np.zeros(X.shape), np.zeros(X.shape)
    for inside, column in ((X >= 2, X - 1), (X + box_w <= width - 2, X + box_w - 1)):
        column = np.clip(column, 0, width - 2)
        total += np.where(inside, (down[Y + box_h, column] - down[Y, column]) / box_h, 0)
        sides += inside
    for inside, row in ((Y >= 2, Y - 1), (Y + box_h <= height - 2, Y + box_h - 1)):
        row = np.clip(row, 0, height - 2)
        total += np.where(inside, (across[row, X + box_w] - across[row, X]) / box_w, 0)
        sides += inside
    # Karanlık ve aydınlık sahneler karşılaştırılabilsin diye kenar gücü görüntünün genel düzeyine bölünür.
    level = float(np.median(np.concatenate([vertical.ravel(), horizontal.ravel()]))) + 2.0
    score = np.where(sides >= 2, total / np.maximum(sides, 1), 0) / level
    index = np.unravel_index(np.argmax(score), score.shape)
    return (X[index] / width, Y[index] / height, box_w / width, box_h / height), float(score[index])


def _inset(box):
    """Kutuyu her kenardan biraz içeri alır; kesitte oyunun ince şeridi kalmasın."""
    x, y, w, h = box
    return (x + CAMERA_INSET, y + CAMERA_INSET, w - 2 * CAMERA_INSET, h - 2 * CAMERA_INSET)


def locate_camera(path, start, end, box, width, height):
    """Bu klipte kamera kutusunun gerçek yeri; klipte kamera kutusu yoksa None.

    Kamera sahneye göre farklı köşelere ve yüksekliklere taşınabilir, bazı sahnelerde hiç olmaz.
    Önce çerçeve aranır, sonra içinde yüz olup olmadığına bakılır.
    """
    frames = _clip_frames(path, start, end, width, height)
    if len(frames) == 0:
        return tuple(box)
    faces = [_face_points(frame) for frame in frames]

    def face_frames(candidate):
        x, y, w, h = candidate
        return sum(any(x <= px <= x + w and y <= py <= y + h for px, py in points) for points in faces)

    # Belirgin çerçeve tek başına yeter: kameradakiler başını çevirince ya da ışık azalınca
    # yüz bulunamayabilir. Zayıf çerçeve ancak içinde yüz görülürse kabul edilir.
    found, strength = _border_search(frames, box)
    if strength >= STRONG_BORDER:
        return _inset(found)
    if strength >= WEAK_BORDER and face_frames(found) >= MIN_FACE_FRAMES:
        return _inset(found)
    for candidate in camera_candidates(box):
        if face_frames(candidate) >= MIN_FACE_FRAMES:
            return candidate
    return None


@dataclass
class Plan:
    mode: str  # "fit": zaten dikey, "track": yüz takibi, "blur": bulanık arka plan, "split": oyun düzeni
    crop_width: int = 0
    positions: list = None  # (klip içi saniye, kırpmanın sol kenarı)
    box: tuple = None  # oyun düzeninde kamera kutusu: (x, y, genişlik, yükseklik) oran olarak
    width: int = 0
    height: int = 0
    game_fit: bool = False
    camera_height: int = 450


def _sample_frames(path, start, end, width, height):
    """Bölümü bir kerede küçük gri kareler olarak okur; tek tek atlamaktan çok daha hızlı."""
    small_height = round(height * DETECT_WIDTH / width / 2) * 2
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(path),
            "-vf", f"fps={1 / SAMPLE_STEP},scale={DETECT_WIDTH}:{small_height},format=gray",
            "-f", "rawvideo", "-",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=media.FFMPEG_TIMEOUT,
    )
    frame_size = DETECT_WIDTH * small_height
    count = len(result.stdout) // frame_size
    return np.frombuffer(result.stdout[: count * frame_size], np.uint8).reshape(count, small_height, DETECT_WIDTH)


def face_centers(path, start, end, width, height):
    """Belirli aralıklarla en büyük yüzün yatay merkezini (0-1) bulur; yüz yoksa None."""
    centers = []
    for index, frame in enumerate(_sample_frames(path, start, end, width, height)):
        faces = _detector.detectMultiScale(frame, scaleFactor=1.15, minNeighbors=6, minSize=(MIN_FACE, MIN_FACE))
        center = None
        if len(faces):
            x, _, w, _ = max(faces, key=lambda f: f[2] * f[3])
            center = (x + w / 2) / DETECT_WIDTH
        centers.append((index * SAMPLE_STEP, center))
    return centers


def smooth(centers):
    """Eksik örnekleri doldurur, titremeyi yumuşatır, küçük oynamaları yok sayar."""
    known = [c for _, c in centers if c is not None]
    if not known:
        return []
    filled, last = [], known[0]
    for moment, center in centers:
        last = center if center is not None else last
        filled.append((moment, last))
    values = np.array([c for _, c in filled])
    median = np.array([np.median(values[max(0, i - 2): i + 3]) for i in range(len(values))])
    result, camera = [], median[0]
    for (moment, _), target in zip(filled, median):
        if abs(target - camera) > DEAD_ZONE:
            camera += (target - camera) * SMOOTHING
        result.append((moment, float(camera)))
    return result


def plan(path, start, end, layout="auto", facecam=None, camera_height=450):
    """layout "split" ise kamera kutusu üste, oyun alta yerleşir; yoksa yüz takibi denenir."""
    info = media.probe(path)
    if info is None:
        raise ValueError("Video okunamadı.")
    width, height = info.width, info.height
    if width <= height * VERTICAL_RATIO + 2:
        return Plan("fit")
    if layout in SPLIT_LAYOUTS and facecam:
        box = locate_camera(path, start, end, facecam, width, height)
        if box is not None:
            return Plan("split", box=box, width=width, height=height,
                        game_fit=layout == "split_fit", camera_height=camera_height)
        # Bu klipte kamera kutusu yok (giriş, tam ekran çekim); otomatik düzene geçilir.
    centers = face_centers(path, start, end, width, height)
    found = sum(c is not None for _, c in centers)
    if not centers or found / len(centers) < MIN_FACE_RATIO:
        return Plan("blur")
    crop_width = int(height * VERTICAL_RATIO) // 2 * 2
    positions = [
        (moment, int(np.clip(center * width - crop_width / 2, 0, width - crop_width)))
        for moment, center in smooth(centers)
    ]
    return Plan("track", crop_width, positions)
