"""Sahne görsellerini seslendirme ve altyazıyla dikey bir Shorts videosuna dönüştürür."""
import random
import re
import subprocess

import imageio_ffmpeg

from .. import config
from ..clips import render as clip_render
from ..clips import subtitles
from ..clips.transcribe import Word
from . import cartoon, puppet

FPS = 30
TRANSITION = 0.4
SCENE_PAUSE = 0.25  # sahneler arasında anlatıma kısa nefes payı
TAIL = 1.5  # son karttan sonra video hemen kesilmesin
HOOK_SECONDS = 3.0
MORAL_SECONDS = 4.5
MUSIC_DIR = config.BASE / "muzik"
MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".ogg"}
MUSIC_VOLUME = 0.12
SUBTITLE_MARGIN = 430
TIMEOUT = 1800

# Kanca ve ders kartları: üstte, krem zemin üzerine koyu yazı.
CARD_STYLE = ("Style: Kart,Georgia,78,&H00283038,&H00283038,&H00D8E8F2,&H00D8E8F2,-1,0,0,0,100,100,0,0,3,18,0,8,"
              "80,80,230,1")

# Her sahneye sırayla farklı kamera hareketi: yakınlaş, uzaklaş, sağa kaydır, sola kaydır.
MOVES = [
    "z='1+0.12*on/{d}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
    "z='1.12-0.12*on/{d}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
    "z='1.12':x='(iw-iw/zoom)*on/{d}':y='ih/2-(ih/zoom/2)'",
    "z='1.12':x='(iw-iw/zoom)*(1-on/{d})':y='ih/2-(ih/zoom/2)'",
]


def _run(args, cwd=None):
    try:
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args],
            cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Video işleme zaman aşımına uğradı.")
    if result.returncode != 0:
        raise ValueError(f"Video işlenemedi: {result.stderr.decode('utf-8', 'replace').strip()[-400:]}")


def audio_duration(path):
    output = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        raise ValueError(f"Ses süresi okunamadı: {path.name}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _encode(args, output, cwd=None):
    """Ekran kartıyla kodlar; olmazsa işlemciyle."""
    error = None
    for encoder in (clip_render.GPU_ENCODER, clip_render.CPU_ENCODER):
        try:
            _run([*args, *encoder, str(output)], cwd)
            return
        except ValueError as exc:
            error = exc
    raise error


def scene_clip(image, seconds, move, output):
    """Durağan görseli verilen sürede yavaşça hareket eden bir klibe çevirir."""
    frames = max(1, round(seconds * FPS))
    # Yakınlaştırmada titreme olmasın diye görsel iki kat büyütülüp sonra küçültülür.
    zoom = MOVES[move % len(MOVES)].format(d=frames)
    _encode([
        "-i", str(image),
        "-vf", (f"scale=2160:3840:force_original_aspect_ratio=increase,crop=2160:3840,"
                f"zoompan={zoom}:d={frames}:s=1080x1920:fps={FPS},format=yuv420p"),
        "-frames:v", str(frames), "-r", str(FPS),
    ], output)


def _clock(seconds):
    return subtitles._clock(seconds)


def _card(text):
    """Kart yazısı birden çok satır olabilir (ör. "BÖLÜM 3" ve altında kanca)."""
    return "\\N".join(subtitles._escape(line.strip()) for line in text.splitlines() if line.strip())


def build_subtitles(scene_words, starts, hook, moral, total):
    """Anlatım altyazısı ile kanca ve ders kartlarını tek ASS dosyasında birleştirir."""
    words = [Word(w.start + start, w.end + start, w.text) for words_, start in zip(scene_words, starts) for w in words_]
    text = subtitles.build(words, 0, SUBTITLE_MARGIN)
    text = text.replace("\n\n[Events]", f"\n{CARD_STYLE}\n\n[Events]")
    cards = []
    if hook:
        cards.append(f"Dialogue: 1,{_clock(0)},{_clock(HOOK_SECONDS)},Kart,,0,0,0,,{_card(hook)}\n")
    if moral:
        cards.append(f"Dialogue: 1,{_clock(max(0, total - MORAL_SECONDS))},{_clock(total)},Kart,,0,0,0,,"
                     f"{_card(moral)}\n")
    return text + "".join(cards)


def pick_music():
    if not MUSIC_DIR.is_dir():
        return None
    tracks = [p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in MUSIC_EXTS]
    return random.choice(tracks) if tracks else None


def build(scenes, hook, moral, music, output, workdir):
    """scenes: [{"image", "audio", "words", "puppet"?}] -> output mp4. Anlatımla görüntü kare kare eşleşir.
    puppet varsa sahne, image mekân olmak üzere kukla canlandırmasıyla üretilir."""
    durations = [audio_duration(s["audio"]) + SCENE_PAUSE for s in scenes]
    starts = [sum(durations[:i]) for i in range(len(scenes))]
    total = sum(durations) + TAIL

    clips = []
    for index, scene in enumerate(scenes):
        # Geçiş bir sonraki sahnenin anlatımı başlarken yapılsın diye her klip geçiş süresi kadar uzun tutulur.
        seconds = durations[index] + (TRANSITION if index < len(scenes) - 1 else TAIL)
        clip = workdir / f"sahne_{index + 1:02}.mp4"
        if scene.get("cartoon"):
            delay = HOOK_SECONDS if index == 0 and hook else 0.0
            until = max(0.0, total - MORAL_SECONDS - starts[index]) if moral and index == len(scenes) - 1 else None
            cartoon.render_scene(scene["image"], audio=scene["audio"], seconds=seconds, output=clip, move=index,
                                 seed=index, label_delay=delay, label_until=until, **scene["cartoon"])
        elif scene.get("puppet"):
            # İlk sahnenin etiketi üstteki kanca kartıyla çakışmasın; kart kalkınca belirir.
            delay = HOOK_SECONDS if index == 0 and hook else 0.0
            # Son sahnede etiket, sonuç kartı (ör. A/B seçenekleri) belirince kalkar.
            until = max(0.0, total - MORAL_SECONDS - starts[index]) if moral and index == len(scenes) - 1 else None
            puppet.render_scene(scene["image"], audio=scene["audio"], seconds=seconds, output=clip, move=index,
                                seed=index, label_delay=delay, label_until=until, **scene["puppet"])
        else:
            scene_clip(scene["image"], seconds, index, clip)
        clips.append(clip)

    (workdir / "hikaye.ass").write_text(
        build_subtitles([s["words"] for s in scenes], starts, hook, moral, total), encoding="utf-8")

    inputs, video = [], ""
    for clip in clips:
        inputs += ["-i", clip.name]
    previous = "[0:v]"
    for index in range(1, len(clips)):
        label = f"[x{index}]"
        video += f"{previous}[{index}:v]xfade=transition=fade:duration={TRANSITION}:offset={starts[index]:.3f}{label};"
        previous = label
    video += f"{previous}ass=hikaye.ass,format=yuv420p[v]"

    audio_inputs = []
    for scene in scenes:
        audio_inputs += ["-i", str(scene["audio"])]
    first_audio = len(clips)
    audio = "".join(f"[{first_audio + i}:a]apad=pad_dur={SCENE_PAUSE}[a{i}];" for i in range(len(scenes)))
    audio += "".join(f"[a{i}]" for i in range(len(scenes))) + f"concat=n={len(scenes)}:v=0:a=1,apad=pad_dur={TAIL}[anlatim];"
    if music:
        audio_inputs += ["-stream_loop", "-1", "-i", str(music)]
        audio += (f"[{first_audio + len(scenes)}:a]volume={MUSIC_VOLUME},atrim=0:{total:.3f},"
                  f"afade=t=out:st={max(0, total - 2):.3f}:d=2[muzik];"
                  "[anlatim][muzik]amix=inputs=2:duration=first:normalize=0[karisik];[karisik]")
    else:
        audio += "[anlatim]"
    audio += f"atrim=0:{total:.3f},loudnorm=I=-14:TP=-1.5:LRA=11[a]"

    _encode([
        *inputs, *audio_inputs,
        "-filter_complex", video + ";" + audio,
        "-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-r", str(FPS), "-t", f"{total:.3f}", "-movflags", "+faststart",
    ], output.resolve(), cwd=workdir)
    return total
