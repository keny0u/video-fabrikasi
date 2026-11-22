"""2D kukla canlandırma: karakter bir kez çizilir, her sahnede aynı çizim mekânın önünde hafifçe canlanır.

Yapay zekâ videosu 8 GB ekran kartında pratik değil; sensec.tv gibi dizilerde de hareket az, tutarlılık çok önemli.
Bu yüzden karakter dizi başına bir kez çizilip arka planı silinir; sahnelerde nefes, salınım, sesle aralanan dudak,
kamera yaklaşması ve yağmur/sis/ışık titremesi gibi efektlerle canlandırılır.
"""
import json
import math
import re
import subprocess

import cv2
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import images

W, H = 1080, 1920
FPS = 30
CUT_MODEL = "isnet-anime"  # çizgi karakterlerde saç kenarını u2net'ten temiz kesiyor
ACTOR_TRIES = 3
# Model "portrait" deyince yalnız baş-omuz çiziyor; orta çekim için bel hizası ve kollar istenir.
ACTOR_PROMPT = ("medium shot, waist-up view of a {look}, full upper body with arms and hands visible, standing, "
                "facing the viewer, centered, neutral calm expression, mouth closed, plain flat light gray background")
BACKGROUND_PROMPT = "{place}, empty scene, no people, nobody, wide establishing view"
EFFECTS = ("yagmur", "sis", "isik", "titreme", "karanlik")
SHOTS = ("genis", "orta", "yakin")
POSITIONS = {"sol": 0.34, "orta": 0.5, "sag": 0.66}
# Çekime göre yüzün çerçeve genişliğine oranı ve yüz merkezinin yüksekliği.
FRAMING = {"genis": (0.2, 0.5), "orta": (0.36, 0.38), "yakin": (0.66, 0.4)}
MOUTH_LEVELS = (0.0, 0.35, 0.7, 1.0)
LABEL_FONT = "C:/Windows/Fonts/ariblk.ttf"
LABEL_TOP = 300  # platformun üst çubuğunun altı; yüzün üstünde kalır
TIMEOUT = 1800


def slug(name):
    table = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    return re.sub(r"[^a-z0-9]+", "_", name.translate(table).lower()).strip("_") or "karakter"


# --- Karakter hazırlama ---------------------------------------------------------------------------------------------

def _find_face(rgb):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, 1.1, 4, minSize=(rgb.shape[1] // 8, rgb.shape[1] // 8))
    if not len(faces):
        return None
    return [int(v) for v in max(faces, key=lambda f: f[2] * f[3])]


def _find_lips(rgb, face):
    """Yüzün alt kısmında tenden doygun ve kırmızımsı en büyük leke dudaktır; dudak çizgisi onun en koyu satırı."""
    x, y, w, h = face
    y0, y1, x0, x1 = y + int(h * .6), min(rgb.shape[0], y + h), x + int(w * .2), x + int(w * .85)
    hsv = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2HSV)
    hue = hsv[..., 0].astype(int)
    lips = ((hsv[..., 1] > 110) & ((hue < 12) | (hue > 170)) & (hsv[..., 2] > 90)).astype(np.uint8)
    # Koyu dudak çizgisi eşiğe girmeyip dudağı ikiye bölebilir; dikeyde kapatılınca iki dudak tek leke olur.
    lips = cv2.morphologyEx(lips, cv2.MORPH_CLOSE, np.ones((7, 3), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(lips)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    if count < 2:
        return _guess_lips(gray, face)
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    lx, ly, lw, lh, area = (int(v) for v in stats[best])
    if area < w * h * 0.004 or lw < w * 0.12 or lh < 6:
        return _guess_lips(gray, face)
    # Renk eşiği bazen yalnız alt dudağı yakalar; dudak çizgisi lekenin üstünde de olabilir. Bu yüzden lekenin bir
    # boyu yukarısından yarım boy aşağısına kadar, dudağın orta sütunlarındaki en koyu satır aranır.
    left, right = lx + x0, lx + lw + x0
    middle = gray[:, left + lw // 5:right - lw // 5]
    top, bottom = ly + y0, ly + lh + y0
    start = max(0, top - lh)
    rows = middle[start:bottom + lh // 2].mean(axis=1)
    seam = start + int(np.argmin(rows))
    return {"left": left, "right": right, "seam": seam, "height": max(6, bottom - seam)}


def _guess_lips(gray, face):
    """Dudak renkten ayrılmıyorsa (yaşlı yüz, soluk dudak) ağız yüzün oranından tahmin edilir; dudak çizgisi o
    bandın ortasındaki en koyu satırdır."""
    x, y, w, h = face
    left, right = x + int(w * .36), x + int(w * .64)
    start, end = y + int(h * .68), min(gray.shape[0], y + int(h * .9))
    if end - start < 4:
        return None
    rows = gray[start:end, x + int(w * .44):x + int(w * .56)].mean(axis=1)
    seam = start + int(np.argmin(rows))
    return {"left": left, "right": right, "seam": seam, "height": max(6, int(h * .06))}


def _cut(image):
    from rembg import new_session, remove

    global _session
    if _session is None:
        _session = new_session(CUT_MODEL)
    return remove(image, session=_session)


_session = None


def make_actor(name, look, style, seed, folder):
    """Karakteri çizer, arka planını siler, yüz ve dudak yerini kaydeder. Daha önce hazırlandıysa dokunmaz."""
    folder.mkdir(parents=True, exist_ok=True)
    sprite_path, info_path = folder / f"{slug(name)}.png", folder / f"{slug(name)}.json"
    if sprite_path.exists() and info_path.exists():
        return sprite_path
    best = None
    for attempt in range(ACTOR_TRIES):
        image = images.draw(ACTOR_PROMPT.format(look=look), style, seed + attempt)
        face = _find_face(np.array(image.convert("RGB")))
        best = best or image
        if face:
            best = image
            break
    cut = np.array(_cut(best).convert("RGBA"))
    alpha = cut[..., 3]
    ys, xs = np.where(alpha > 16)
    top, bottom, left, right = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    cut = cut[top:bottom, left:right]
    # Yüz ve dudak, arka planı silinmiş karakterin gri zemin üzerindeki hâlinde aranır.
    flat = _over(cut, (200, 200, 200))
    face = _find_face(flat)
    lips = _find_lips(flat, face) if face else None
    Image.fromarray(cut).save(sprite_path)
    info_path.write_text(json.dumps({"name": name, "face": face, "lips": lips}, ensure_ascii=False), encoding="utf-8")
    return sprite_path


def _over(rgba, color):
    alpha = rgba[..., 3:4].astype(np.float32) / 255
    return (rgba[..., :3] * alpha + np.array(color, np.float32) * (1 - alpha)).astype(np.uint8)


def load_actor(sprite_path):
    info = json.loads(sprite_path.with_suffix(".json").read_text(encoding="utf-8"))
    return np.array(Image.open(sprite_path).convert("RGBA")), info


def mouth_frames(sprite, lips):
    """Dudak çizgisinden aşağısı ortada en çok, kenarlara doğru sıfıra inerek kayar; aralıkta koyu ağız içi görünür."""
    if not lips:
        return [sprite] * len(MOUTH_LEVELS)
    height, width = sprite.shape[:2]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    center, half = (lips["left"] + lips["right"]) / 2, (lips["right"] - lips["left"]) / 2 + 6
    seam, lip_h = lips["seam"], lips["height"]
    # Kayma yanlara ve aşağı doğru yumuşakça söner; sert kenar kırışık tende dikdörtgen bir bant gibi görünüyordu.
    def smooth(v):
        v = np.clip(v, 0, 1)
        return v * v * (3 - 2 * v)

    across = smooth(1 - np.abs(xx - center) / (half * 1.8))
    down = smooth((yy - seam) / 4) * smooth(1 - (yy - seam - lip_h) / (lip_h * 3))
    frames = []
    for level in MOUTH_LEVELS:
        gap = lip_h * 0.35 * level
        if gap < 1:
            frames.append(sprite)
            continue
        image = cv2.remap(sprite.astype(np.float32), xx, (yy - gap * across * down).astype(np.float32),
                          cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        inside = np.zeros((height, width), np.float32)
        cv2.ellipse(inside, (int(center), int(seam + gap / 2)), (int(half * 0.5), max(1, int(gap / 2))),
                    0, 0, 360, 1.0, -1, cv2.LINE_AA)
        inside = cv2.GaussianBlur(inside, (3, 3), 0)[..., None]
        image[..., :3] = image[..., :3] * (1 - inside) + np.array([48, 18, 22], np.float32) * inside
        frames.append(np.clip(image, 0, 255).astype(np.uint8))
    return frames


# --- Ses ------------------------------------------------------------------------------------------------------------

def mouth_track(audio, frames):
    """Her kare için ağız açıklığı basamağı (0-3): konuşmanın ses yüksekliğinden."""
    raw = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(audio),
         "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT,
    ).stdout
    samples = np.frombuffer(raw, np.float32)
    hop = 8000 // FPS
    levels = np.zeros(frames, int)
    count = min(frames, len(samples) // hop)
    if count == 0:
        return levels
    rms = np.sqrt(np.mean(samples[:count * hop].reshape(count, hop) ** 2, axis=1))
    loud = np.percentile(rms, 95) or 1
    openness = np.clip((rms / loud - 0.15) / 0.6, 0, 1)
    levels[:count] = np.round(openness * (len(MOUTH_LEVELS) - 1)).astype(int)
    return levels


# --- Efekt katmanları -----------------------------------------------------------------------------------------------

def _rain_tile(rng):
    tile = np.zeros((H * 2, W), np.float32)
    for _ in range(700):
        x, y, length = rng.integers(0, W), rng.integers(0, H * 2), rng.integers(30, 80)
        cv2.line(tile, (int(x), int(y)), (int(x - length * 0.18), int(y + length)), float(rng.uniform(0.3, 0.9)), 1,
                 cv2.LINE_AA)
    return cv2.GaussianBlur(tile, (3, 3), 0)


def _fog_tile(rng):
    noise = rng.random((H // 32, W // 16)).astype(np.float32)
    fog = cv2.resize(noise, (W * 2, H), interpolation=cv2.INTER_CUBIC)
    fog = cv2.GaussianBlur(fog, (0, 0), 60)
    fog -= fog.min()
    return fog / (fog.max() or 1)


def _vignette(strength):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    distance = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2) / math.sqrt(2)
    return (1 - strength * np.clip(distance, 0, 1) ** 2)[..., None]


def _label(text):
    """Sol üstte koyu kutu içinde etiket ("ELİF'İN GÖZÜ", "OTUZ DOKUZ GECE KALDI")."""
    text = text.strip().upper().replace("i", "İ")
    try:
        font = ImageFont.truetype(LABEL_FONT, 46)
    except OSError:
        font = ImageFont.load_default()
    left, top, right, bottom = font.getbbox(text)
    box_w, box_h = right - left + 60, bottom - top + 36
    layer = Image.new("RGBA", (box_w + 12, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rectangle([0, 0, 11, box_h], fill=(200, 30, 40, 255))
    draw.rectangle([12, 0, box_w + 12, box_h], fill=(10, 10, 14, 200))
    draw.text((42 - left, 18 - top), text, font=font, fill=(255, 255, 255, 255))
    return np.array(layer)


# --- Sahne ----------------------------------------------------------------------------------------------------------

def _cover(image, scale):
    """Görseli W x H çerçeveyi kaplayacak ve kamera hareketine pay kalacak şekilde büyütür."""
    height, width = image.shape[:2]
    factor = max(W / width, H / height) * scale
    return cv2.resize(image, (round(width * factor), round(height * factor)), interpolation=cv2.INTER_CUBIC)


def _placement(sprite_shape, info, shot, position):
    """Karakterin çerçevedeki ölçeği ve sol-üst köşesi (kamera hareketinden önce).

    Ölçek yüz genişliğinden alınır; çizimler bazen bel hizasından bazen omuzdan kesildiği için boy güvenilmez."""
    height, width = sprite_shape[:2]
    face = info.get("face")
    center_x = W * (0.5 if shot == "yakin" else POSITIONS.get(position, 0.5))
    if face:
        face_share, center_y = FRAMING.get(shot, FRAMING["orta"])
        face_x, face_y = face[0] + face[2] / 2, face[1] + face[3] / 2
        scale = W * face_share / face[2]
        # Karakterin alt kenarı çerçevenin içinde kalırsa havada kesik görünür. Kısa çizimde karakteri aşağı itmek
        # yüzü altyazının üstüne düşürür; bunun yerine alt kenar çerçeveden taşana kadar yakınlaşılır.
        scale = max(scale, H * (1.02 - center_y) / max(1, height - face_y))
        # Yakınlaşınca kenardaki karakterin yüzü çerçeveden taşmasın; yüz kutusu her zaman içeride kalır.
        x = center_x - face_x * scale
        x = min(max(x, W * 0.04 - face[0] * scale), W * 0.96 - (face[0] + face[2]) * scale)
        return scale, x, H * center_y - face_y * scale
    scale = H * (0.56 if shot == "genis" else 0.78) / height
    return scale, center_x - width * scale / 2, H * 1.02 - height * scale


def _tint(sprite, background):
    """Karakteri mekânın ışığına yaklaştırır: karanlık sahnede karartır, renge hafifçe boyar."""
    mean = background.reshape(-1, 3).mean(0)
    light = float(np.clip(0.45 + mean.mean() / 255 * 0.9, 0.55, 1.0))
    tinted = sprite.astype(np.float32)
    tinted[..., :3] = (tinted[..., :3] * light) * 0.85 + (mean * light) * 0.15
    return np.clip(tinted, 0, 255).astype(np.uint8)


def render_scene(background, actor, audio, seconds, output, shot="orta", position="orta", effects=(),
                 label="", speaking=False, move=0, seed=0, label_delay=0.0, label_until=None):
    """Tek sahnenin hareketli klibini üretir. actor: make_actor'ın döndürdüğü yol ya da None.
    label_delay / label_until: etiket üstteki kanca ve sonuç kartlarıyla çakışmasın diye yalnız bu aralıkta görünür."""
    rng = np.random.default_rng(seed)
    frames = max(1, round(seconds * FPS))
    effects = set(effects or ())
    back = _cover(np.array(Image.open(background).convert("RGB")), 1.12)

    sprites, info, track = None, {}, np.zeros(frames, int)
    if actor:
        sprite, info = load_actor(actor)
        sprites = [_tint(s, back) for s in mouth_frames(sprite, info.get("lips") if speaking else None)]
        if speaking and audio:
            track = mouth_track(audio, frames)
        scale, base_x, base_y = _placement(sprite.shape, info, shot, position)

    rain = _rain_tile(rng) if "yagmur" in effects else None
    fog = _fog_tile(rng) if "sis" in effects else None
    vignette = _vignette(0.75 if "karanlik" in effects else 0.45)
    darken = 0.6 if "karanlik" in effects else 1.0
    tag = _label(label) if label else None
    flicker = np.ones(frames, np.float32)
    if "isik" in effects:
        index = 0
        while index < frames:
            index += int(rng.integers(FPS // 2, FPS * 2))
            for offset in range(int(rng.integers(2, 6))):
                if index + offset < frames:
                    flicker[index + offset] = rng.uniform(0.35, 0.7)
    shake = rng.normal(0, 7, (frames, 2)) if "titreme" in effects else np.zeros((frames, 2))
    direction = (-1, 1)[move % 2]  # sahneler sırayla sağa ve sola kayar; hep aynı hareket sıkıcı olmasın

    process = subprocess.Popen(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "16", "-pix_fmt", "yuv420p", str(output)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        for frame_index in range(frames):
            t = frame_index / FPS
            progress = frame_index / max(1, frames - 1)
            # Kamera: yavaş yaklaşma ve hafif yana kayma. Karakter arka plandan biraz daha çok hareket eder (derinlik).
            zoom = 1 + 0.06 * progress
            pan = direction * 30 * progress
            bh, bw = back.shape[:2]
            matrix = np.float32([[zoom, 0, W / 2 - bw / 2 * zoom + pan + shake[frame_index, 0]],
                                 [0, zoom, H / 2 - bh / 2 * zoom + shake[frame_index, 1]]])
            frame = cv2.warpAffine(back, matrix, (W, H), flags=cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_REFLECT).astype(np.float32)
            if sprites:
                sprite = sprites[track[frame_index]]
                breathe = 1 + 0.007 * math.sin(2 * math.pi * t / 3.6)
                sway = 5 * math.sin(2 * math.pi * t / 5.3)
                char_zoom = 1 + 0.09 * progress
                sx, sy = scale * char_zoom, scale * char_zoom * breathe
                sh = sprite.shape[0]
                # Nefes alttan yukarı doğru uzar: karakterin alt kenarı sabit kalır.
                x = W / 2 + (base_x - W / 2) * char_zoom + pan * 1.6 + sway + shake[frame_index, 0] * 1.4
                y = H / 2 + (base_y - H / 2) * char_zoom + sh * scale * char_zoom * (1 - breathe) \
                    + shake[frame_index, 1] * 1.4
                layer = cv2.warpAffine(sprite, np.float32([[sx, 0, x], [0, sy, y]]), (W, H),
                                       flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
                alpha = layer[..., 3:4].astype(np.float32) / 255
                frame = frame * (1 - alpha) + layer[..., :3] * alpha
            if fog is not None:
                shift = int(t * 25) % W
                frame = frame + (255 - frame) * fog[:, shift:shift + W, None] * 0.35
            if rain is not None:
                shift = int(t * 1500) % H
                frame = frame + rain[shift:shift + H, :, None] * 110
            frame = frame * vignette * (darken * flicker[frame_index])
            if tag is not None and t >= label_delay and (label_until is None or t < label_until):
                fade = min(1.0, (t - label_delay) / 0.3)
                th, tw = tag.shape[:2]
                region = frame[LABEL_TOP:LABEL_TOP + th, 0:tw]
                a = tag[..., 3:4].astype(np.float32) / 255 * fade
                frame[LABEL_TOP:LABEL_TOP + th, 0:tw] = region * (1 - a) + tag[..., :3] * a
            process.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
        process.stdin.close()
        process.wait(timeout=TIMEOUT)
    except (BrokenPipeError, OSError):
        process.kill()
        process.wait()
    if process.returncode != 0:
        error = process.stderr.read().decode("utf-8", "replace").strip()[-400:]
        raise ValueError(f"Kukla sahnesi işlenemedi: {error}")
    return output
