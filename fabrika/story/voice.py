"""Türkçe seslendirme: Microsoft Edge'in ücretsiz sesleri, cümle cümle tonlamalı ve kelime zamanlı.

Bir sahnenin metni tek parça okununca düz yazı okunuyormuş gibi tekdüze olur. Bu yüzden metin cümlelere
bölünür; her cümle noktalamasına ve başındaki ruh hali işaretine göre farklı hız, perde ve ses yüksekliğiyle
okunur, araya noktalamaya uygun sessizlik konur.
"""
import asyncio
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import edge_tts
import imageio_ffmpeg

from ..clips.transcribe import Word

VOICES = {"ahmet": "tr-TR-AhmetNeural", "emel": "tr-TR-EmelNeural"}
TICKS = 10_000_000  # Edge zamanları 100 nanosaniye biriminde
RETRIES = 6
RETRY_PAUSE = 3


@dataclass
class Delivery:
    rate: int  # yüzde
    pitch: int  # Hz
    volume: int  # yüzde


# Claude anlatıma "[fısıltı] Kapının ardından bir ses geldi." gibi işaretler koyar.
MOODS = {
    "normal": Delivery(-3, 0, 0),
    "fısıltı": Delivery(-12, -6, -30),
    "gergin": Delivery(-8, -3, -5),
    "korku": Delivery(0, 4, 5),
    "heyecan": Delivery(6, 6, 10),
    "hüzün": Delivery(-10, -5, -10),
    "sakin": Delivery(-6, -2, -5),
}
MOOD_TAG = re.compile(r"^\s*\[([a-zçğıöşü]+)\]\s*", re.IGNORECASE)
SENTENCE = re.compile(r"[^.!?…]+(?:\.\.\.|[.!?…]+)?[\"”’']?", re.UNICODE)
# Cümleden sonra bırakılan sessizlik (saniye): üç nokta gerilim için uzun, soru düşünme payı bırakır.
PAUSES = {"…": 0.6, "...": 0.6, "?": 0.4, "!": 0.3, ".": 0.22}
QUESTION_PITCH = 5
EXCLAIM_RATE, EXCLAIM_VOLUME = 4, 10


def sentences(text):
    """Metni (ruh hali, cümle) parçalarına böler; işaret bir sonraki işarete kadar geçerli kalır."""
    mood = "normal"
    parts = []
    for raw in re.split(r"(?=\[[a-zçğıöşü]+\])", text, flags=re.IGNORECASE):
        match = MOOD_TAG.match(raw)
        if match and match[1].casefold() in MOODS:
            mood = match[1].casefold()
            raw = raw[match.end():]
        for sentence in SENTENCE.findall(raw):
            if sentence.strip(" \"”’'"):
                parts.append((mood, sentence.strip()))
    return parts


def delivery_for(mood, sentence):
    base = MOODS.get(mood, MOODS["normal"])
    rate, pitch, volume = base.rate, base.pitch, base.volume
    ending = sentence.rstrip("\"”’' ")
    if ending.endswith("?"):
        pitch += QUESTION_PITCH
    elif ending.endswith("!") and mood != "fısıltı":
        rate += EXCLAIM_RATE
        volume += EXCLAIM_VOLUME
    return Delivery(rate, pitch, volume)


def pause_after(sentence):
    ending = sentence.rstrip("\"”’' ")
    for mark in ("...", "…", "?", "!", "."):
        if ending.endswith(mark):
            return PAUSES[mark]
    return PAUSES["."]


def _signed(value, unit):
    return f"{value:+d}{unit}"


async def _speak_part(text, voice, delivery, path):
    words = []
    communicate = edge_tts.Communicate(
        text, voice, rate=_signed(delivery.rate, "%"), pitch=_signed(delivery.pitch, "Hz"),
        volume=_signed(delivery.volume, "%"), boundary="WordBoundary",
    )
    with open(path, "wb") as file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / TICKS
                words.append(Word(start, start + chunk["duration"] / TICKS, chunk["text"]))
    return words


def _with_retries(text, voice, delivery, path):
    for attempt in range(RETRIES):
        try:
            words = asyncio.run(_speak_part(text, voice, delivery, path))
            if words:
                return words
            raise ValueError("Seslendirme boş döndü.")
        except Exception as exc:  # edge-tts ağ hatalarını farklı türlerde fırlatır
            if attempt == RETRIES - 1:
                raise ValueError(f"Seslendirme yapılamadı: {exc}")
            time.sleep(RETRY_PAUSE)


def _duration(path):
    output = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            encoding="utf-8", errors="replace").stderr
    hours, minutes, seconds = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", output).groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def speak(text, voice, path):
    """Metni cümle cümle tonlayarak seslendirip tek mp3'e yazar; kelime zamanlarını döner."""
    voice_name = VOICES.get(voice, VOICES["ahmet"])
    parts = sentences(text) or [("normal", text)]
    words, inputs, chain, clock = [], [], "", 0.0
    for index, (mood, sentence) in enumerate(parts):
        raw_path = path.with_name(f"{path.stem}_{index:02}.mp3")
        part_words = _with_retries(sentence, voice_name, delivery_for(mood, sentence), raw_path)
        part_path = _trim_tail(raw_path)
        words += [Word(w.start + clock, w.end + clock, w.text) for w in part_words]
        gap = pause_after(sentence) if index < len(parts) - 1 else 0.0
        inputs += ["-i", str(part_path)]
        chain += f"[{index}:a]aresample=24000,apad=pad_dur={gap}[p{index}];"
        clock += _duration(part_path) + gap
    chain += "".join(f"[p{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]"
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *inputs,
         "-filter_complex", chain, "-map", "[a]", "-c:a", "libmp3lame", "-b:a", "128k", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Ses parçaları birleştirilemedi: {result.stderr.decode('utf-8', 'replace')[-300:]}")
    for index in range(len(parts)):
        path.with_name(f"{path.stem}_{index:02}.mp3").unlink(missing_ok=True)
        path.with_name(f"{path.stem}_{index:02}.wav").unlink(missing_ok=True)
    return words


def _trim_tail(path):
    """Edge her cümlenin sonuna sessizlik ekler; cümleler arası boşluk yalnızca noktalamadan gelsin diye kırpılır.

    Baştaki sessizliğe dokunulmaz ki kelime zamanları kaymasın.
    """
    trimmed = path.with_suffix(".wav")
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
         "-af", "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse",
         str(trimmed)],
        stdin=subprocess.DEVNULL, capture_output=True,
    )
    return trimmed if result.returncode == 0 else path


# --- Chatterbox: duygulu, doğal okuma ----------------------------------------------------------------------------
#
# Chatterbox Türkçeyi Edge'den çok daha doğal ve duygulu okuyor ama ana ortamın paketleriyle çakıştığı için ayrı
# ortamda (.venv-ses) ayrı süreç olarak çalışır. Sesin rengini kısa bir örnek kayıttan alır; örnekler Edge'in
# Emel/Ahmet seslerinden bir kez üretilir, yaşlı ve çocuk karakterler için perdesi kaydırılır.
# Kelime zamanı vermediği için her cümle Whisper ile yazıya dökülür: hem altyazı zamanları buradan gelir hem de
# yutulan ya da bozuk okunan cümle anlaşılıp yeniden okutulur.

CLONES = {
    "kadin": ("emel", 1.0),
    "yasli_kadin": ("emel", 0.86),
    "erkek": ("ahmet", 1.0),
    "yasli_erkek": ("ahmet", 0.88),
    "cocuk": ("emel", 1.12),
}
EDGE_FALLBACK = {"kadin": "emel", "yasli_kadin": "emel", "erkek": "ahmet", "yasli_erkek": "ahmet", "cocuk": "emel"}
REFERENCE_TEXT = ("Bu hikayeyi size anlatmak istiyorum. Yıllar önce küçük bir köyde yaşanmıştı. "
                  "Kimse inanmadı ama ben her şeyi kendi gözlerimle gördüm.")
# Ruh hali → (abartı, cfg). Abartı arttıkça okuma duygulanır; cfg düşükse daha yavaş ve dramatik okur.
CLONE_MOODS = {
    "normal": (0.5, 0.5), "sakin": (0.4, 0.5), "fısıltı": (0.35, 0.35), "gergin": (0.6, 0.4),
    "korku": (0.8, 0.35), "heyecan": (0.8, 0.45), "hüzün": (0.45, 0.45),
}
CLONE_ROUNDS = 3  # bozuk okunan cümle en fazla iki kez daha okutulur
MATCH_LIMIT = 0.85  # harf düzeyinde; tek harf farkı geçer, "gülmüştüm" → "görmüştüm" geçmez
CLONE_TIMEOUT = 3600


# Ayrı ortam programın kendi klasöründedir (veri klasörü FABRIKA_HOME ile başka yerde olabilir).
CLONE_PYTHON = Path(__file__).resolve().parents[2] / ".venv-ses" / "Scripts" / "python.exe"


def clone_available():
    return CLONE_PYTHON.exists()


def _reference(key):
    """Chatterbox'a sesin rengini veren örnek kayıt; yoksa Edge sesinden üretilir."""
    from .. import config

    folder = config.BASE / "sesler"
    folder.mkdir(exist_ok=True)
    path = folder / f"{key}.wav"
    if path.exists():
        return path
    base, factor = CLONES[key]
    raw = folder / f"{base}_ham.mp3"
    if not raw.exists():
        speak(REFERENCE_TEXT, base, raw)
    chain = "anull"  # perde örnekte değil, okumadan sonra kaydırılır (Chatterbox örneğin perdesini korumuyor)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                    "-af", chain, "-ac", "1", "-ar", "24000", str(path)], stdin=subprocess.DEVNULL, check=True)
    return path


def _clone_settings(mood, sentence):
    exaggeration, cfg = CLONE_MOODS.get(mood, CLONE_MOODS["normal"])
    if sentence.rstrip("\"”’' ").endswith("!") and mood != "fısıltı":
        exaggeration = min(1.0, exaggeration + 0.15)
    return exaggeration, cfg


def _run_clone(jobs, workdir):
    job_file = workdir / "seslendirme.json"
    job_file.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8")
    script = Path(__file__).with_name("chatterbox_job.py")
    try:
        result = subprocess.run([str(CLONE_PYTHON), str(script), str(job_file)], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=CLONE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError("Seslendirme zaman aşımına uğradı.")
    if result.returncode != 0:
        raise ValueError(f"Seslendirme yapılamadı: {result.stderr.decode('utf-8', 'replace').strip()[-400:]}")


ONES = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
TENS = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]


def number_words(number):
    """Whisper sayıları rakamla yazar ("98"); metindeki "doksan sekiz" ile karşılaştırmak için yazıya çevrilir."""
    if number == 0:
        return "sıfır"
    if number >= 1_000_000:
        return str(number)
    thousands, rest = divmod(number, 1000)
    hundreds, rest = divmod(rest, 100)
    tens, ones = divmod(rest, 10)
    words = ""
    if thousands:
        words += ("" if thousands == 1 else number_words(thousands)) + "bin"
    if hundreds:
        words += ("" if hundreds == 1 else ONES[hundreds]) + "yüz"
    return words + TENS[tens] + ONES[ones]


def _letters(text):
    """Karşılaştırma için yalnız harfler: büyük/küçük, noktalama ve boşluk farkı sayılmaz, rakamlar yazıya döner."""
    text = re.sub(r"\d+", lambda m: number_words(int(m[0])), text.replace("İ", "i").replace("I", "ı").casefold())
    return re.sub(r"[^a-zçğıöşüâîû]", "", text)


def _matches(expected, heard):
    """Okunan cümle metinle harf harf yeterince örtüşüyor mu? Tek kelimelik cümleler de denetlenir
    ("Gülmüştüm" yerine "Görmüştüm" okunması anlamı değiştirir)."""
    import difflib

    expected, heard = _letters(expected), _letters(heard)
    if len(expected) < 4:
        return True
    return difflib.SequenceMatcher(None, expected, heard).ratio() >= MATCH_LIMIT


def _samples(path):
    import numpy as np

    raw = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                          "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], stdin=subprocess.DEVNULL,
                         capture_output=True).stdout
    return np.frombuffer(raw, np.float32)


def align(sentence, heard, duration):
    """Cümlenin asıl kelimelerine Whisper'ın duyduğu zamanları dağıtır.

    Whisper "otuz dokuz"u "39" yazabilir; kelimeler birebir eşleşmez. Bu yüzden konum harf sayısına göre
    oranlanır: asıl metnin %40'ındaki kelime, duyulan metnin %40'ındaki zamana denk gelir."""
    import numpy as np

    tokens = sentence.split()
    if not tokens:
        return []
    if not heard:
        step = duration / len(tokens)
        return [Word(i * step, (i + 1) * step, token) for i, token in enumerate(tokens)]
    lengths = np.array([len(w.text) + 1 for w in heard], float)
    edges = np.concatenate([[0], np.cumsum(lengths)]) / lengths.sum()
    times = [heard[0].start] + [w.end for w in heard]
    own = np.array([len(t) + 1 for t in tokens], float)
    own_edges = np.concatenate([[0], np.cumsum(own)]) / own.sum()
    stamps = np.interp(own_edges, edges, times)
    return [Word(float(stamps[i]), float(stamps[i + 1]), token) for i, token in enumerate(tokens)]


def speak_all(items, workdir, on_progress=None):
    """items: [(metin, ses, yol)]. Her metni seslendirir; her biri için kelime zamanlarını döner.

    ses bir Edge sesi ("emel", "ahmet") ya da Chatterbox sesi ("kadin", "yasli_kadin", "erkek", ...) olabilir."""
    results = [None] * len(items)
    clone_items = []
    for index, (text, voice, path) in enumerate(items):
        if voice in CLONES and clone_available():
            clone_items.append(index)
        else:
            results[index] = speak(text, EDGE_FALLBACK.get(voice, voice), path)
    if not clone_items:
        return results

    from ..clips import transcribe

    # Her cümle ayrı okunur: ruh haline göre farklı ayar, noktalamaya göre ara sessizliği.
    parts = []
    for index in clone_items:
        text, voice, path = items[index]
        for number, (mood, sentence) in enumerate(sentences(text) or [("normal", text)]):
            exaggeration, cfg = _clone_settings(mood, sentence)
            parts.append({"item": index, "sentence": sentence, "reference": str(_reference(voice)),
                          "exaggeration": exaggeration, "cfg": cfg,
                          "out": str(path.with_name(f"{path.stem}_{number:02}.wav")), "heard": []})
    pending = parts
    for round_number in range(CLONE_ROUNDS):
        _run_clone([{"text": p["sentence"], "reference": p["reference"], "exaggeration": p["exaggeration"],
                     "cfg": p["cfg"], "seed": round_number * 1000 + i, "out": p["out"]}
                    for i, p in enumerate(pending)], workdir)
        failed = []
        for part in pending:
            segments, _ = transcribe.transcribe(_samples(part["out"]), "tr")
            part["heard"] = [w for s in segments for w in s.words]
            if not _matches(part["sentence"], " ".join(w.text for w in part["heard"])):
                failed.append(part)
        if on_progress:
            on_progress((round_number + 1) / CLONE_ROUNDS)
        if not failed:
            break
        pending = failed
    transcribe.release()

    for index in clone_items:
        text, voice, path = items[index]
        own = [p for p in parts if p["item"] == index]
        words, inputs, chain, clock = [], [], "", 0.0
        for number, part in enumerate(own):
            trimmed = _trim_clone(Path(part["out"]))
            seconds = _duration(trimmed)
            words += [Word(w.start + clock, w.end + clock, w.text)
                      for w in align(part["sentence"], part["heard"], seconds)]
            gap = pause_after(part["sentence"]) if number < len(own) - 1 else 0.0
            inputs += ["-i", str(trimmed)]
            chain += f"[{number}:a]aresample=24000,apad=pad_dur={gap}[p{number}];"
            clock += seconds + gap
        chain += "".join(f"[p{i}]" for i in range(len(own))) + f"concat=n={len(own)}:v=0:a=1"
        # Chatterbox örnek sesin perdesini korumuyor; yaşlı ve çocuk sesleri anlatıcıdan ayrılsın diye perde sonradan
        # kaydırılır. atempo süreyi geri düzelttiği için kelime zamanları kaymaz.
        factor = CLONES[voice][1]
        if factor != 1:
            chain += f",asetrate={round(24000 * factor)},aresample=24000,atempo={1 / factor:.4f}"
        chain += "[a]"
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *inputs,
             "-filter_complex", chain, "-map", "[a]", "-c:a", "libmp3lame", "-b:a", "160k", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True,
        )
        if result.returncode != 0:
            raise ValueError(f"Ses parçaları birleştirilemedi: {result.stderr.decode('utf-8', 'replace')[-300:]}")
        for part in own:
            Path(part["out"]).unlink(missing_ok=True)
            Path(part["out"]).with_name(Path(part["out"]).stem + "_kirpik.wav").unlink(missing_ok=True)
        results[index] = words
    return results


def _trim_clone(path):
    """Chatterbox cümlenin sonuna sessizlik ve nefes bırakır; kuyruk kırpılır (baş kalır ki zamanlar kaymasın)."""
    trimmed = path.with_name(path.stem + "_kirpik.wav")
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
         "-af", "areverse,silenceremove=start_periods=1:start_threshold=-40dB:start_silence=0.05,areverse",
         str(trimmed)],
        stdin=subprocess.DEVNULL, capture_output=True,
    )
    return trimmed if result.returncode == 0 else path
