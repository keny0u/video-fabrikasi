"""Düz 2D çizgi film karakterleri: kodla çizilen, parçalı (baş, göz, kaş, ağız, kollar) ve her karede canlanan kuklalar.

Karakter resim modeline çizdirilmez; görünüşü birkaç ayardan (ten, başörtüsü/saç, kıyafet renkleri, yaş) oluşur.
Bu yüzden her bölümde birebir aynıdır ve göz kırpma, kaş, konuşma ağzı, baş ve kol hareketleri gerçekten oynar.
Çizim skia ile doğrudan videonun kare belleğine (BGRA) yapılır.
"""
import math
import random
import re
from dataclasses import dataclass, field

import skia

# Ölçüler kafa merkezli yerel birimlerde; çizerken çekime göre ölçeklenir. Kalça (0, 0), omuzlar y=-250.
SHOULDER_Y = -250
NECK_Y = -268
HEAD_Y = -385
HEAD_RX, HEAD_RY = 82, 98

SKIN = {"acik": "#f3d2b8", "bugday": "#e0b08c", "esmer": "#b98260", "koyu": "#7d5038"}
HAIR = {"siyah": "#2a2224", "kahve": "#5a3a2a", "sari": "#d9b25b", "kizil": "#a8482a", "gri": "#b9b6b2",
        "beyaz": "#ecebe7"}

EMOTIONS = {
    # kaş açısı (+ iç uç yukarı), kaş yüksekliği, göz açıklığı, gözbebeği, ağız eğrisi (+ gülümseme), titreme
    "normal": dict(brow_tilt=0, brow_raise=0, eye_open=1.0, pupil=1.0, smile=0.15, shake=0),
    "korku": dict(brow_tilt=0.45, brow_raise=10, eye_open=1.3, pupil=0.6, smile=-0.35, shake=2.5),
    "uzgun": dict(brow_tilt=0.5, brow_raise=2, eye_open=0.8, pupil=1.0, smile=-0.45, shake=0),
    "kizgin": dict(brow_tilt=-0.55, brow_raise=-6, eye_open=0.85, pupil=0.9, smile=-0.3, shake=0.6),
    "supheli": dict(brow_tilt=-0.2, brow_raise=-3, eye_open=0.6, pupil=1.0, smile=-0.05, shake=0),
    "mutlu": dict(brow_tilt=0.1, brow_raise=4, eye_open=0.95, pupil=1.1, smile=0.6, shake=0),
}


def color(value, alpha=255):
    value = value.lstrip("#")
    return skia.Color(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha)


def shade(value, factor):
    value = value.lstrip("#")
    parts = [min(255, max(0, round(int(value[i:i + 2], 16) * factor))) for i in (0, 2, 4)]
    return "#" + "".join(f"{p:02x}" for p in parts)


@dataclass
class Look:
    """Karakterin görünüşü; Claude'un seçtiği birkaç değerden oluşur."""
    skin: str = "bugday"
    headwear: str = "basortusu"  # basortusu, yok, kasket
    headwear_color: str = "#c0392b"
    hair: str = "siyah"
    hair_style: str = "uzun"  # uzun, kisa, topuz, kel
    top: str = "#2f6b4f"  # ceket/hırka
    shirt: str = "#f2efe6"
    age: str = "genc"  # cocuk, genc, orta, yasli
    beard: bool = False
    glasses: bool = False


@dataclass
class Pose:
    """Bir karedeki eklem ve yüz durumu."""
    head_tilt: float = 0.0
    head_turn: float = 0.0  # -1 sola, +1 sağa bakar (yüz parçaları kayar)
    breathe: float = 0.0
    blink: float = 0.0  # 0 açık, 1 kapalı
    look_x: float = 0.0
    look_y: float = 0.0
    mouth: int = 0  # 0 kapalı, 1 az açık, 2 açık, 3 çok açık, 4 "o"
    arm_l: tuple = (0.1, 0.05)  # omuz ve dirsek açısı (radyan, aşağıdan)
    arm_r: tuple = (-0.1, -0.05)
    emotion: dict = field(default_factory=lambda: dict(EMOTIONS["normal"]))
    shake: tuple = (0.0, 0.0)


def _paint(value, alpha=255, stroke=0.0):
    paint = skia.Paint(AntiAlias=True, Color=color(value, alpha))
    if stroke:
        paint.setStyle(skia.Paint.kStroke_Style)
        paint.setStrokeWidth(stroke)
        paint.setStrokeCap(skia.Paint.kRound_Cap)
        paint.setStrokeJoin(skia.Paint.kRound_Join)
    return paint


def _oval(canvas, cx, cy, rx, ry, paint):
    canvas.drawOval(skia.Rect.MakeLTRB(cx - rx, cy - ry, cx + rx, cy + ry), paint)


class Character:
    def __init__(self, look, seed=0):
        self.look = look
        self.rng = random.Random(seed)
        self.skin = SKIN.get(look.skin, SKIN["bugday"])
        self.hair = HAIR.get(look.hair, HAIR["siyah"])
        self.line = shade(self.skin, 0.45)

    # --- parçalar ---------------------------------------------------------------------------------------------------

    def _torso(self, canvas, pose):
        lk = self.look
        width = 86 * (1 + pose.breathe * 0.01)
        top = SHOULDER_Y - pose.breathe * 2
        body = skia.Path()
        body.moveTo(-width, top + 26)
        body.quadTo(-width, top, -width + 34, top - 6)
        body.lineTo(width - 34, top - 6)
        body.quadTo(width, top, width, top + 26)
        body.lineTo(width - 8, 40)
        body.lineTo(-width + 8, 40)
        body.close()
        canvas.drawPath(body, _paint(lk.top))
        # Gömlek yakası (V) ve hırkanın iki yanı.
        shirt = skia.Path()
        shirt.moveTo(-28, top - 6)
        shirt.lineTo(28, top - 6)
        shirt.lineTo(0, top + 80)
        shirt.close()
        canvas.drawPath(shirt, _paint(lk.shirt))
        canvas.drawLine(0, top + 80, 0, 40, _paint(shade(lk.top, 0.7), stroke=4))
        for side in (-1, 1):
            fold = skia.Path()
            fold.moveTo(side * 28, top - 6)
            fold.lineTo(side * 4, top + 86)
            canvas.drawPath(fold, _paint(shade(lk.top, 0.75), stroke=5))

    def _arm(self, canvas, side, angles, pose):
        shoulder_angle, elbow_angle = angles
        canvas.save()
        canvas.translate(side * 76, SHOULDER_Y + 18 - pose.breathe * 2)
        canvas.rotate(math.degrees(shoulder_angle))
        # Kol gövdenin önünden geçince kaybolmasın diye biraz koyu ve ince bir konturla çizilir.
        sleeve, edge = shade(self.look.top, 0.8), shade(self.look.top, 0.55)
        canvas.drawLine(0, 0, 0, 110, _paint(edge, stroke=40))
        canvas.drawLine(0, 0, 0, 110, _paint(sleeve, stroke=34))
        canvas.translate(0, 110)
        canvas.rotate(math.degrees(elbow_angle))
        canvas.drawLine(0, 0, 0, 96, _paint(edge, stroke=36))
        canvas.drawLine(0, 0, 0, 96, _paint(sleeve, stroke=30))
        _oval(canvas, 0, 110, 17, 19, _paint(self.skin))
        canvas.restore()

    def _neck(self, canvas):
        if self.look.headwear == "basortusu":
            return  # başörtüsü boynu örter
        canvas.drawRect(skia.Rect.MakeLTRB(-24, NECK_Y - 40, 24, SHOULDER_Y + 4), _paint(shade(self.skin, 0.9)))

    def _scarf_back(self, canvas):
        lk = self.look
        if lk.headwear != "basortusu":
            return
        drape = skia.Path()
        drape.moveTo(-HEAD_RX - 20, HEAD_Y)
        drape.quadTo(-HEAD_RX - 16, SHOULDER_Y - 20, -66, SHOULDER_Y + 28)
        drape.quadTo(0, SHOULDER_Y + 48, 66, SHOULDER_Y + 28)
        drape.quadTo(HEAD_RX + 16, SHOULDER_Y - 20, HEAD_RX + 20, HEAD_Y)
        drape.close()
        canvas.drawPath(drape, _paint(shade(lk.headwear_color, 0.85)))
        _oval(canvas, 0, HEAD_Y - 6, HEAD_RX + 24, HEAD_RY + 22, _paint(lk.headwear_color))

    def _hair_back(self, canvas):
        lk = self.look
        if lk.headwear == "basortusu" or lk.hair_style == "kel":
            return
        if lk.hair_style == "uzun":
            back = skia.Path()
            back.addRRect(skia.RRect.MakeRectXY(
                skia.Rect.MakeLTRB(-HEAD_RX - 14, HEAD_Y - HEAD_RY + 10, HEAD_RX + 14, HEAD_Y + 150), 60, 60))
            canvas.drawPath(back, _paint(self.hair))
        _oval(canvas, 0, HEAD_Y - 10, HEAD_RX + 8, HEAD_RY + 4, _paint(self.hair))

    def _face(self, canvas, pose):
        lk = self.look
        _oval(canvas, 0, HEAD_Y, HEAD_RX, HEAD_RY, _paint(self.skin))
        if lk.headwear != "basortusu":
            for side in (-1, 1):  # kulaklar
                _oval(canvas, side * (HEAD_RX - 2), HEAD_Y + 6, 13, 20, _paint(shade(self.skin, 0.93)))
        turn = pose.head_turn * 14
        emotion = pose.emotion
        eye_y = HEAD_Y - 8
        # Yanaklar.
        for side in (-1, 1):
            _oval(canvas, side * 46 + turn, HEAD_Y + 28, 14, 8, _paint("#e8857a", 70))
        # Gözler.
        for side in (-1, 1):
            cx = side * 31 + turn
            open_ry = 17 * emotion["eye_open"]
            canvas.save()
            clip = skia.Path()
            clip.addOval(skia.Rect.MakeLTRB(cx - 15, eye_y - open_ry, cx + 15, eye_y + open_ry))
            canvas.clipPath(clip, doAntiAlias=True)
            canvas.drawPaint(_paint("#ffffff"))
            iris_x = cx + pose.look_x * 5 + pose.head_turn * 3
            iris_y = eye_y + pose.look_y * 4
            _oval(canvas, iris_x, iris_y, 10.5, 11.5, _paint("#4a2e22" if lk.age != "yasli" else "#3c3a3a"))
            pupil = 5.5 * emotion["pupil"]
            _oval(canvas, iris_x, iris_y, pupil, pupil, _paint("#111111"))
            _oval(canvas, iris_x + 3.5, iris_y - 4, 2.6, 2.6, _paint("#ffffff"))
            # Göz kapağı yukarıdan iner.
            lid = (2 * open_ry + 2) * pose.blink
            if lid > 0:
                canvas.drawRect(skia.Rect.MakeLTRB(cx - 16, eye_y - open_ry - 1, cx + 16, eye_y - open_ry + lid),
                                _paint(self.skin))
            canvas.restore()
            if pose.blink > 0.5:
                # Kapak inince göz akının kenarında kalan ince çizgi tenle örtülür.
                _oval(canvas, cx, eye_y, 15, open_ry, _paint(self.skin, stroke=3))
            lash_y = eye_y - open_ry + (2 * open_ry) * pose.blink
            lash = skia.Path()
            lash.moveTo(cx - 16, lash_y + 4)
            lash.quadTo(cx, lash_y - 5 * (1 - pose.blink), cx + 16, lash_y + 4)
            canvas.drawPath(lash, _paint("#241a1a", stroke=4.5))
            if lk.age == "yasli":
                canvas.drawLine(cx - 12, eye_y + open_ry + 6, cx + 10, eye_y + open_ry + 8, _paint(self.line, 140, 2))
        # Kaşlar: iç uç kaş açısıyla yukarı ya da aşağı.
        brow_color = HAIR["gri"] if lk.age == "yasli" else self.hair
        brow_y = eye_y - 30 - emotion["brow_raise"]
        for side in (-1, 1):
            cx = side * 31 + turn
            inner, outer = cx - side * 14, cx + side * 15
            tilt = emotion["brow_tilt"] * 12
            brow = skia.Path()
            brow.moveTo(inner, brow_y - tilt)
            brow.quadTo(cx, brow_y - 5, outer, brow_y + 2)
            canvas.drawPath(brow, _paint(brow_color, stroke=6))
        if lk.age == "yasli":  # alın kırışıkları
            for offset in (0, 9):
                canvas.drawLine(-24 + turn, brow_y - 18 - offset, 24 + turn, brow_y - 18 - offset,
                                _paint(self.line, 110, 2))
        # Burun.
        nose = skia.Path()
        nose.moveTo(turn * 1.3 - 2, HEAD_Y + 8)
        nose.quadTo(turn * 1.3 + 8, HEAD_Y + 26, turn * 1.3 - 6, HEAD_Y + 28)
        canvas.drawPath(nose, _paint(self.line, 170, 3))
        if lk.glasses:
            for side in (-1, 1):
                _oval(canvas, side * 31 + turn, eye_y, 22, 19, _paint("#2b2b2b", stroke=3.5))
            canvas.drawLine(-9 + turn, eye_y - 2, 9 + turn, eye_y - 2, _paint("#2b2b2b", stroke=3))
        if lk.beard:
            beard = skia.Path()
            beard.moveTo(-HEAD_RX + 4, HEAD_Y + 6)
            beard.quadTo(-HEAD_RX + 10, HEAD_Y + HEAD_RY + 26, 0, HEAD_Y + HEAD_RY + 26)
            beard.quadTo(HEAD_RX - 10, HEAD_Y + HEAD_RY + 26, HEAD_RX - 4, HEAD_Y + 6)
            beard.quadTo(HEAD_RX - 20, HEAD_Y + 40, 0, HEAD_Y + 38)
            beard.quadTo(-HEAD_RX + 20, HEAD_Y + 40, -HEAD_RX + 4, HEAD_Y + 6)
            canvas.drawPath(beard, _paint(self.hair))
            canvas.drawRoundRect(skia.Rect.MakeLTRB(-24 + turn, HEAD_Y + 38, 24 + turn, HEAD_Y + 48), 5, 5,
                                 _paint(self.hair))  # bıyık
        self._mouth(canvas, pose, turn)

    def _mouth(self, canvas, pose, turn):
        cx, cy = turn * 1.1, HEAD_Y + 52
        smile = pose.emotion["smile"]
        lip = shade(self.skin, 0.55)
        if pose.mouth == 0:
            mouth = skia.Path()
            mouth.moveTo(cx - 16, cy - smile * 4)
            mouth.quadTo(cx, cy + smile * 10, cx + 16, cy - smile * 4)
            canvas.drawPath(mouth, _paint(lip, stroke=4))
            return
        rx, ry = {1: (12, 5), 2: (15, 10), 3: (18, 15), 4: (9, 12)}[pose.mouth]
        inside = skia.Path()
        inside.addOval(skia.Rect.MakeLTRB(cx - rx, cy - ry * 0.6, cx + rx, cy + ry * 1.2))
        canvas.drawPath(inside, _paint("#5a1e24"))
        canvas.save()
        canvas.clipPath(inside, doAntiAlias=True)
        if pose.mouth >= 2:
            canvas.drawRect(skia.Rect.MakeLTRB(cx - rx, cy - ry, cx + rx, cy - ry * 0.25), _paint("#ffffff"))
        _oval(canvas, cx, cy + ry * 1.15, rx * 0.7, ry * 0.55, _paint("#c4545c"))
        canvas.restore()
        canvas.drawPath(inside, _paint(lip, stroke=3))

    def _scarf_front(self, canvas):
        lk = self.look
        if lk.headwear == "basortusu":
            rim = skia.Path()
            rim.addArc(skia.Rect.MakeLTRB(-HEAD_RX - 6, HEAD_Y - HEAD_RY - 10, HEAD_RX + 6, HEAD_Y + HEAD_RY + 4),
                       195, 150)
            canvas.drawPath(rim, _paint(lk.headwear_color, stroke=26))
            band = skia.Path()
            band.addArc(skia.Rect.MakeLTRB(-HEAD_RX + 6, HEAD_Y - HEAD_RY + 4, HEAD_RX - 6, HEAD_Y + HEAD_RY - 8),
                        205, 130)
            canvas.drawPath(band, _paint(shade(lk.headwear_color, 0.8), stroke=4))
            # Çenenin altındaki kıvrım.
            fold = skia.Path()
            fold.moveTo(-40, HEAD_Y + HEAD_RY - 4)
            fold.quadTo(0, HEAD_Y + HEAD_RY + 16, 40, HEAD_Y + HEAD_RY - 4)
            canvas.drawPath(fold, _paint(shade(lk.headwear_color, 0.75), stroke=4))
        elif lk.headwear == "kasket":
            cap = skia.Path()
            cap.addArc(skia.Rect.MakeLTRB(-HEAD_RX - 4, HEAD_Y - HEAD_RY - 14, HEAD_RX + 4, HEAD_Y + 40), 180, 180)
            cap.close()
            canvas.drawPath(cap, _paint(lk.headwear_color))
            canvas.drawRoundRect(skia.Rect.MakeLTRB(-HEAD_RX - 10, HEAD_Y - 52, HEAD_RX + 30, HEAD_Y - 38), 7, 7,
                                 _paint(shade(lk.headwear_color, 0.75)))
        elif lk.hair_style != "kel":
            fringe = skia.Path()
            fringe.moveTo(-HEAD_RX - 4, HEAD_Y + 10)
            fringe.quadTo(-HEAD_RX - 6, HEAD_Y - HEAD_RY - 8, 0, HEAD_Y - HEAD_RY - 8)
            fringe.quadTo(HEAD_RX + 6, HEAD_Y - HEAD_RY - 8, HEAD_RX + 4, HEAD_Y + 10)
            fringe.quadTo(HEAD_RX - 14, HEAD_Y - 50, 10, HEAD_Y - 58)
            fringe.quadTo(-50, HEAD_Y - 52, -HEAD_RX + 10, HEAD_Y - 20)
            fringe.close()
            canvas.drawPath(fringe, _paint(self.hair))
            if lk.hair_style == "topuz":
                _oval(canvas, 0, HEAD_Y - HEAD_RY - 16, 30, 24, _paint(self.hair))

    # --- çizim ------------------------------------------------------------------------------------------------------

    def draw(self, canvas, pose, x, y, scale):
        """(x, y) kalça noktası; scale yerel birimden piksele."""
        canvas.save()
        canvas.translate(x + pose.shake[0], y + pose.shake[1])
        canvas.scale(scale, scale)
        self._torso(canvas, pose)
        self._arm(canvas, -1, pose.arm_l, pose)
        self._arm(canvas, 1, pose.arm_r, pose)
        canvas.save()
        canvas.translate(0, NECK_Y)
        canvas.rotate(math.degrees(pose.head_tilt))
        canvas.translate(0, -NECK_Y)
        self._scarf_back(canvas)
        self._hair_back(canvas)
        self._neck(canvas)
        self._face(canvas, pose)
        self._scarf_front(canvas)
        canvas.restore()
        canvas.restore()


class Animator:
    """Karakterin kare kare hareketini üretir: nefes, göz kırpma, bakış, baş salınımı, konuşma ve el hareketi."""

    def __init__(self, emotion="normal", seed=0, fps=30):
        self.rng = random.Random(seed)
        self.fps = fps
        self.emotion = dict(EMOTIONS.get(emotion, EMOTIONS["normal"]))
        self.next_blink = self.rng.uniform(0.6, 2.5)
        self.blink_start = -1.0
        self.look = (0.0, 0.0)
        self.next_look = self.rng.uniform(0.8, 2.0)
        self.gesture = 0.0
        self.mouth = 0
        self.mouth_hold = 0

    def pose(self, t, level=0):
        """level: konuşmanın o karedeki ses yüksekliği basamağı (0-3)."""
        rng = self.rng
        pose = Pose(emotion=self.emotion)
        pose.breathe = math.sin(2 * math.pi * t / 3.4)
        pose.head_tilt = 0.025 * math.sin(2 * math.pi * t / 4.7) + 0.012 * math.sin(2 * math.pi * t / 1.9)
        pose.head_turn = 0.25 * math.sin(2 * math.pi * t / 6.1)
        # Göz kırpma: 2-5 saniyede bir, 5 karede kapanıp açılır; korkuda daha seyrek.
        if t >= self.next_blink:
            self.blink_start = t
            gap = rng.uniform(2.0, 5.0) * (1.6 if self.emotion["shake"] > 1 else 1)
            self.next_blink = t + gap
        since = (t - self.blink_start) * self.fps
        # 2 karede kapanır, 2 kare tam kapalı kalır, 2 karede açılır (tepe noktası tam kareye denk gelsin).
        pose.blink = min(1.0, max(0.0, 1 - (abs(since - 2.5) - 0.5) / 2)) if 0 <= since <= 5 else 0.0
        # Bakış: arada bir başka noktaya kayar.
        if t >= self.next_look:
            self.look = (rng.uniform(-0.8, 0.8), rng.uniform(-0.4, 0.3))
            self.next_look = t + rng.uniform(0.8, 2.5)
        pose.look_x, pose.look_y = self.look
        # Konuşma: ses basamağına göre ağız şekli; aynı şekil en az iki kare kalır ki titremesin.
        if self.mouth_hold > 0:
            self.mouth_hold -= 1
        else:
            if level == 0:
                self.mouth = 0
            else:
                self.mouth = 4 if level == 2 and rng.random() < 0.3 else level
            self.mouth_hold = 1
        pose.mouth = self.mouth
        # Konuşurken arada el kalkar ve iner.
        target = 1.0 if level > 0 and math.sin(2 * math.pi * t / 2.3) > 0.4 else 0.0
        self.gesture += (target - self.gesture) * 0.12
        # El göğüs hizasına, gövdenin önüne gelir (yana açılan kol kırık çubuk gibi görünüyordu).
        pose.arm_r = (-0.1 - 0.15 * self.gesture, -0.05 + 1.75 * self.gesture)
        pose.arm_l = (0.1, 0.05)
        shake = self.emotion["shake"]
        if shake:
            pose.shake = (rng.gauss(0, shake), rng.gauss(0, shake))
        return pose


# --- Görünüşü senaryodan kurma ------------------------------------------------------------------------------------

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
HEADWEAR = ("basortusu", "yok", "kasket")
HAIR_STYLES = ("uzun", "kisa", "topuz", "kel")
AGES = ("cocuk", "genc", "orta", "yasli")


def look_from(character):
    """Claude'un yazdığı karakter kaydından görünüş; geçersiz değerler varsayılana düşer."""
    def pick(key, allowed, default):
        value = character.get(key)
        return value if value in allowed else default

    def hex_color(key, default):
        value = character.get(key) or ""
        return value if HEX.match(value) else default

    return Look(
        skin=pick("skin", tuple(SKIN), "bugday"),
        headwear=pick("headwear", HEADWEAR, "yok"),
        headwear_color=hex_color("headwear_color", "#c0392b"),
        hair=pick("hair", tuple(HAIR), "siyah"),
        hair_style=pick("hair_style", HAIR_STYLES, "kisa"),
        top=hex_color("top", "#3e4a5c"),
        shirt=hex_color("shirt", "#f2efe6"),
        age=pick("age", AGES, "genc"),
        beard=bool(character.get("beard")),
        glasses=bool(character.get("glasses")),
    )


def voice_for(character):
    """Karakterin konuşma sesi (voice.CLONES anahtarı)."""
    old = character.get("age") == "yasli"
    if character.get("age") == "cocuk":
        return "cocuk"
    if character.get("gender") == "erkek":
        return "yasli_erkek" if old else "erkek"
    return "yasli_kadin" if old else "kadin"


# --- Sahne çizimi ---------------------------------------------------------------------------------------------------

W, H, FPS = 1080, 1920, 30
POSITIONS = {"sol": 0.27, "orta": 0.5, "sag": 0.73}
# Çekim → (ölçek, baş yüksekliği oranı). Yakın çekimde yalnızca konuşan (ya da ilk karakter) görünür.
FRAMING = {"genis": (1.45, 0.6), "orta": (1.95, 0.47), "yakin": (3.4, 0.4)}
BACKGROUND_STYLE = ("flat vector illustration background, very simple shapes, few objects, large empty areas, "
                    "clean flat colors, no outlines, minimal detail, 2d cartoon animation background, no people, "
                    "nobody")
NIGHT_WORDS = ("night", "dark", "midnight", "moonlight", "gece")
TIMEOUT = 1800


def _background(path, night):
    """Mekân hafif bulanıklaşır (karakterler öne çıksın), gece sahnesinde maviye çekilip kararır."""
    import cv2
    import numpy as np
    from PIL import Image

    from . import puppet

    back = cv2.cvtColor(np.array(Image.open(path).convert("RGB")), cv2.COLOR_RGB2BGRA)
    back = cv2.GaussianBlur(puppet._cover(back, 1.1), (0, 0), 2.2)
    if night:
        grade = np.array([0.85, 0.62, 0.55, 1.0], np.float32)  # BGRA: mavi kalır, kırmızı ve yeşil kısılır
        back = np.clip(back.astype(np.float32) * grade, 0, 255).astype(np.uint8)
    return back


def _layout(cast, shot, speaker):
    """Her karakter için ekrandaki yatay konum (oran). Aynı yere düşenler eşit aralıkla dağılır."""
    if shot == "yakin":
        focus = next((c for c in cast if c["name"] == speaker), cast[0])
        return [(focus, 0.5)]
    wanted = [POSITIONS.get(c.get("position"), 0.5) for c in cast]
    if len(set(wanted)) < len(wanted):
        wanted = [0.5] if len(cast) == 1 else [0.22 + 0.56 * i / (len(cast) - 1) for i in range(len(cast))]
    return list(zip(cast, wanted))


def render_scene(background, cast, audio, seconds, output, shot="orta", effects=(), label="", speaker="",
                 move=0, seed=0, label_delay=0.0, label_until=None, night=None):
    """Bir sahnenin hareketli klibi. cast: [{"name", "look": Look, "position", "emotion"}]."""
    import subprocess

    import cv2
    import imageio_ffmpeg
    import numpy as np

    from . import puppet

    rng = np.random.default_rng(seed)
    frames = max(1, round(seconds * FPS))
    effects = set(effects or ())
    if night is None:
        night = any(word in str(background).lower() for word in NIGHT_WORDS)
    back = _background(background, night)
    scale, head_y = FRAMING.get(shot, FRAMING["orta"])
    placed = _layout(cast, shot, speaker) if cast else []
    characters = [(Character(member["look"], seed), Animator(member.get("emotion", "normal"), seed + i, FPS),
                   member, x) for i, (member, x) in enumerate(placed)]
    track = puppet.mouth_track(audio, frames) if audio and speaker else np.zeros(frames, int)
    speaker_x = next((x for member, x in placed if member["name"] == speaker), None)

    rain = puppet._rain_tile(rng) if "yagmur" in effects else None
    fog = puppet._fog_tile(rng) if "sis" in effects else None
    vignette = puppet._vignette(0.75 if "karanlik" in effects else 0.4)
    darken = 0.6 if "karanlik" in effects else 1.0
    tag = puppet._label(label)[..., [2, 1, 0, 3]] if label else None  # RGBA → BGRA
    flicker = np.ones(frames, np.float32)
    if "isik" in effects:
        index = 0
        while index < frames:
            index += int(rng.integers(FPS // 2, FPS * 2))
            for offset in range(int(rng.integers(2, 6))):
                if index + offset < frames:
                    flicker[index + offset] = rng.uniform(0.35, 0.7)
    shake = rng.normal(0, 7, (frames, 2)) if "titreme" in effects else np.zeros((frames, 2))
    direction = (-1, 1)[move % 2]

    buffer = np.zeros((H, W, 4), np.uint8)
    canvas = skia.Surface(buffer).getCanvas()
    process = subprocess.Popen(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
         "-pix_fmt", "bgra", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "18", "-pix_fmt", "yuv420p", str(output)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        for frame_index in range(frames):
            t = frame_index / FPS
            progress = frame_index / max(1, frames - 1)
            # Kamera yavaşça yaklaşır ve yana kayar; karakterler mekândan biraz daha çok hareket eder (derinlik).
            zoom = 1 + 0.05 * progress
            pan = direction * 24 * progress
            bh, bw = back.shape[:2]
            matrix = np.float32([[zoom, 0, W / 2 - bw / 2 * zoom + pan + shake[frame_index, 0]],
                                 [0, zoom, H / 2 - bh / 2 * zoom + shake[frame_index, 1]]])
            cv2.warpAffine(back, matrix, (W, H), dst=buffer, flags=cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REFLECT)
            char_zoom = scale * (1 + 0.07 * progress)
            for character, animator, member, x in characters:
                speaking = member["name"] == speaker
                pose = animator.pose(t, int(track[frame_index]) if speaking else 0)
                if speaker_x is not None and not speaking:
                    # Dinleyen konuşana bakar.
                    side = 1 if speaker_x > x else -1
                    pose.look_x, pose.head_turn = side * 0.9, side * 0.5
                cx = W / 2 + (x * W - W / 2) * (1 + 0.07 * progress) + pan * 1.5 + shake[frame_index, 0] * 1.4
                hip_y = H * head_y - HEAD_Y * char_zoom + shake[frame_index, 1] * 1.4
                character.draw(canvas, pose, cx, hip_y, char_zoom)
            image = buffer[..., :3].astype(np.float32)
            if fog is not None:
                shift = int(t * 25) % W
                image += (255 - image) * fog[:, shift:shift + W, None] * 0.3
            if rain is not None:
                shift = int(t * 1500) % H
                image += rain[shift:shift + H, :, None] * 110
            image *= vignette * (darken * flicker[frame_index])
            if tag is not None and t >= label_delay and (label_until is None or t < label_until):
                fade = min(1.0, (t - label_delay) / 0.3)
                th, tw = tag.shape[:2]
                top = puppet.LABEL_TOP
                alpha = tag[..., 3:4].astype(np.float32) / 255 * fade
                image[top:top + th, :tw] = image[top:top + th, :tw] * (1 - alpha) + tag[..., :3] * alpha
            buffer[..., :3] = np.clip(image, 0, 255).astype(np.uint8)
            process.stdin.write(buffer.tobytes())
        process.stdin.close()
        process.wait(timeout=TIMEOUT)
    except (BrokenPipeError, OSError):
        process.kill()
        process.wait()
    if process.returncode != 0:
        raise ValueError(f"Çizgi film sahnesi işlenemedi: {process.stderr.read().decode('utf-8', 'replace')[-400:]}")
    return output
