"""Link ya da dosyadan klip üretim hattı: indir, yazıya dök, anları seç, dikey klip yap."""
import json
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .. import config, db, intake
import anthropic

from . import (ai_titles, audio, download, events, highlights, reframe, render, subtitles, titles, transcribe,
               vision)

IDLE_SECONDS = 2
PAUSED = "paused"
TRANSCRIPT_FILE = "yazi.json"


def _update(job_id, **fields):
    assignments = ", ".join(f"{name}=?" for name in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE clip_jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))


def _stage(job_id, stage, progress=0.0):
    _update(job_id, stage=stage, progress=progress)


def workdir_for(job_id):
    return config.CLIPS_DIR / str(job_id)


def source_file(job):
    """İşin kaynak videosu: indirildiyse çalışma klasöründe, yüklendiyse kendi yolunda."""
    if job["source"].startswith(("http://", "https://")):
        downloaded = sorted(workdir_for(job["id"]).glob("kaynak.*"), key=lambda p: p.stat().st_size, reverse=True)
        return downloaded[0] if downloaded else None
    path = Path(job["source"])
    return path if path.exists() else None


def send_clip(conn, clip, targets, immediate=False, run_at=0, title=None, description=None):
    """Klibin kopyasını yayın kuyruğuna verir; önizleme için asıl dosya yerinde kalır."""
    stored = intake.library_path(Path(clip["path"]).name)
    shutil.copy2(clip["path"], stored)
    meta = {
        "title": title or clip["title"],
        "description": clip["description"] if description is None else description,
        "tags": json.loads(clip["tags"] or "[]"),
    }
    item_id = intake.ingest(conn, stored, Path(clip["path"]).name, targets, meta, immediate, run_at)
    conn.execute("UPDATE items SET clip_job_id=? WHERE id=?", (clip["clip_job_id"], item_id))
    conn.execute("UPDATE clips SET sent=1, title=?, description=? WHERE id=?",
                 (meta["title"], meta["description"], clip["id"]))


def _texts(settings, segments, ranges, source_title):
    """Her (başlangıç, bitiş) aralığı için (başlık, açıklama, etiketler).

    Claude anahtarı varsa bütün klipler tek istekte yazılır; yoksa ya da istek başarısız olursa
    ücretsiz şablon yöntemi kullanılır.
    """
    channel = settings.get("channel", "")
    kind = settings.get("mode") if settings.get("mode") in ("korku", "komik") else "genel"
    results = [titles.make(segments, start, end, source_title, channel, kind) for start, end in ranges]
    if not ai_titles.is_configured() or not ranges:
        return results
    clips = [
        {"id": index, "seconds": end - start,
         "text": " ".join(w.text for w in highlights.words_between(segments, start, end))}
        for index, (start, end) in enumerate(ranges)
    ]
    try:
        written = ai_titles.write(clips, source_title, channel, kind)
    except (anthropic.APIError, ValueError, KeyError, StopIteration) as exc:
        print(f"[başlık] Claude kullanılamadı, şablon yöntemi kullanılıyor: {exc!r}")
        return results
    for index, (title, body, tags) in written.items():
        if 0 <= index < len(results) and title:
            results[index] = (title, titles.compose(body, channel, tags), tags)
    return results


def refresh_titles(job_id):
    """Klipleri yeniden üretmeden başlık ve açıklamaları yeniler; yüklenmemiş kuyruk kopyaları da güncellenir."""
    with db.connect() as conn:
        job = conn.execute("SELECT * FROM clip_jobs WHERE id=?", (job_id,)).fetchone()
    settings = json.loads(job["settings"])
    workdir = workdir_for(job_id)
    caches = sorted(workdir.glob(f"*_{TRANSCRIPT_FILE}"))
    if not caches:
        raise ValueError("Bu işin yazıya dökümü yok; önce yeniden üret.")
    segments, _ = transcribe.load(caches[0])
    if job["source"].startswith(("http://", "https://")) and not settings.get("channel"):
        settings["channel"] = download.channel_name(job["source"])
        _update(job_id, settings=json.dumps(settings))
    with db.connect() as conn:
        clips = conn.execute("SELECT * FROM clips WHERE clip_job_id=? ORDER BY start", (job_id,)).fetchall()
    texts = _texts(settings, segments, [(c["start"], c["end"]) for c in clips], job["title"])
    with db.connect() as conn:
        for clip, (title, description, tags) in zip(clips, texts):
            conn.execute("UPDATE clips SET title=?, description=?, tags=? WHERE id=?",
                         (title, description, json.dumps(tags, ensure_ascii=False), clip["id"]))
            conn.execute(
                """UPDATE items SET title=?, description=?, tags=? WHERE clip_job_id=? AND source_name=?
                   AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.item_id=items.id AND jobs.status IN ('done', 'uploading'))""",
                (title, description, json.dumps(tags, ensure_ascii=False), job_id, Path(clip["path"]).name),
            )


def _motion(path, workdir, facecam, duration, on_progress):
    """Video taraması saklanır; aynı kutuyla yeniden üretimde tekrar taranmaz."""
    boxes = reframe.camera_candidates(facecam) if facecam else []
    key = "-".join(f"{v:.3f}" for v in facecam) if facecam else "tam"
    cache = workdir / f"hareket_v2_{key}.npz"
    if cache.exists():
        data = np.load(cache)
        return vision.Motion(float(data["step"]), data["camera"], data["game"])
    motion = vision.scan(path, duration, boxes, on_progress)
    np.savez(cache, step=motion.step, camera=motion.camera, game=motion.game)
    return motion


def _transcript(samples, workdir, language, on_progress):
    """Yazıya döküm saklanır; aynı videoyu başka ayarla yeniden denemek saniyeler sürer."""
    cache = workdir / f"{language or 'oto'}_{TRANSCRIPT_FILE}"
    if cache.exists():
        return transcribe.load(cache)
    segments, detected = transcribe.transcribe(samples, language, on_progress)
    transcribe.save(segments, detected, cache)
    return segments, detected


def _moments(mode, settings, segments, language, samples, motion):
    duration = len(samples) / audio.SAMPLE_RATE
    count, min_len, max_len = settings["count"], settings["min_len"], settings["max_len"]
    if mode == "elle":
        return events.manual_moments(events.parse_ranges(settings.get("manual", ""), duration), segments)
    if mode == "kelime":
        return events.keyword_moments(segments, settings.get("keyword", ""), duration, count, min_len, max_len)
    loudness = audio.loudness(samples)
    if mode in events.KINDS:
        score = events.curve(mode, duration, loudness, 0.5, motion, segments)
        return events.find(mode, score, segments, duration, count, min_len, max_len)
    return highlights.find(segments, loudness, count, min_len, max_len, language)


NO_MOMENTS = {
    "elle": "Geçerli zaman aralığı girilmedi.",
    "kelime": "Aranan kelime konuşmada geçmiyor.",
}


def _reset_outputs(job_id, workdir):
    with db.connect() as conn:
        conn.execute("DELETE FROM clips WHERE clip_job_id=?", (job_id,))
        # Yeniden üretimde bu işten kuyruğa girip henüz yüklenmemiş klipler çıkarılır; aynı an iki kez yüklenmesin.
        stale = conn.execute(
            """SELECT id, path FROM items WHERE clip_job_id=? AND NOT EXISTS
               (SELECT 1 FROM jobs WHERE jobs.item_id=items.id AND jobs.status IN ('done', 'uploading'))""",
            (job_id,),
        ).fetchall()
        for item in stale:
            conn.execute("DELETE FROM jobs WHERE item_id=?", (item["id"],))
            conn.execute("DELETE FROM items WHERE id=?", (item["id"],))
            if Path(item["path"]).parent == config.LIBRARY:
                Path(item["path"]).unlink(missing_ok=True)
    for old in [*workdir.glob("klip_*.mp4"), *workdir.glob("hikaye_*.mp4")]:
        try:
            old.unlink()
        except PermissionError:
            pass  # panelde önizlemesi açık; yeni klipler başka adla yazılır, bu sonraki üretimde silinir


def process(job):
    job_id = job["id"]
    settings = json.loads(job["settings"])
    workdir = workdir_for(job_id)
    workdir.mkdir(parents=True, exist_ok=True)
    _reset_outputs(job_id, workdir)

    if settings.get("kind") in ("story", "episode"):
        from ..story import pipeline as story_pipeline

        clip, _ = story_pipeline.process(job, workdir, lambda text, value=0.0: _stage(job_id, text, value),
                                         int(time.time()))
        if settings.get("auto_targets"):
            # Dizi bölümleri oylama süresi işlesin diye hazır olur olmaz yayınlanır.
            with db.connect() as conn:
                send_clip(conn, clip, settings["auto_targets"], immediate=settings.get("publish_now", False))
        return None

    source = job["source"]
    downloaded = sorted(workdir.glob("kaynak.*"), key=lambda p: p.stat().st_size, reverse=True)
    if source.startswith(("http://", "https://")) and downloaded and job["title"]:
        path, title = downloaded[0], job["title"]  # yeniden üretimde tekrar indirilmez
        if not settings.get("channel"):
            settings["channel"] = download.channel_name(source)
            _update(job_id, settings=json.dumps(settings))
    elif source.startswith(("http://", "https://")):
        _stage(job_id, "İndiriliyor")
        path, title, settings["channel"] = download.download(source, workdir, lambda p: _stage(job_id, "İndiriliyor", p))
        _update(job_id, settings=json.dumps(settings))
    else:
        path, title = Path(source), Path(source).stem.split("_", 1)[-1]
    _update(job_id, title=title)

    # Kamera her videoda başka yerde olabilir; kutu onaylanana kadar iş bekler.
    layout = settings.get("layout", "auto")
    facecam = settings.get("facecam")
    if layout in reframe.SPLIT_LAYOUTS and not facecam:
        with db.connect() as conn:
            default = db.get_setting(conn, "facecam")
            ask = db.get_setting(conn, "facecam_ask", True)
        if default and not ask:
            facecam = default
        else:
            _update(job_id, status="waiting_box", stage="Kamera kutusunu işaretle", progress=0)
            return PAUSED

    _stage(job_id, "Ses okunuyor")
    samples = audio.load(path)
    mode = settings.get("mode", "genel")
    motion = None
    if mode in events.KINDS:
        # Görüntü taraması ile yazıya dökme kartın ayrı birimlerini kullanır; aynı anda yapılır.
        progress = {"video": 0.0, "yazi": 0.0}

        def report(part, value):
            progress[part] = value
            _stage(job_id, "Video izleniyor ve yazıya dökülüyor", sum(progress.values()) / 2)

        duration = len(samples) / audio.SAMPLE_RATE
        motion_box = facecam if layout in reframe.SPLIT_LAYOUTS else None
        with ThreadPoolExecutor(max_workers=1) as pool:
            scanning = pool.submit(_motion, path, workdir, motion_box, duration, lambda p: report("video", p))
            segments, language = _transcript(samples, workdir, settings.get("language"),
                                             lambda p: report("yazi", p))
            motion = scanning.result()
    else:
        _stage(job_id, "Yazıya dökülüyor")
        segments, language = _transcript(samples, workdir, settings.get("language"),
                                         lambda p: _stage(job_id, "Yazıya dökülüyor", p))
    if mode in ("genel", "kelime") and not any(segment.words for segment in segments):
        raise ValueError("Videoda konuşma bulunamadı.")

    _stage(job_id, "Önemli anlar seçiliyor")
    moments = _moments(mode, settings, segments, language, samples, motion)
    if not moments:
        raise ValueError(NO_MOMENTS.get(mode, "Bu uzunlukta uygun an bulunamadı. Klip uzunluk aralığını genişlet."))

    run_id = int(time.time())  # önizlemesi açık eski kliplerle aynı adı taşımasın
    _stage(job_id, "Başlıklar yazılıyor")
    texts = _texts(settings, segments, [(m.start, m.end) for m in moments], title)
    for number, (moment, (clip_title, description, tags)) in enumerate(zip(moments, texts), start=1):
        _stage(job_id, f"Klip {number}/{len(moments)} hazırlanıyor", (number - 1) / len(moments))
        camera_height = render.CAMERA_SIZES.get(settings.get("camera_size"), render.CAMERA_SIZES["orta"])
        plan = reframe.plan(path, moment.start, moment.end, layout, facecam, camera_height)
        words = highlights.words_between(segments, moment.start, moment.end)
        margin = render.subtitle_margin(plan) if plan.mode == "split" else subtitles.MARGIN_BOTTOM
        text = subtitles.build(words, moment.start, margin) if settings.get("subtitles", True) else None
        output = workdir / f"klip_{run_id}_{number:02}.mp4"
        render.render(path, output, moment.start, moment.end, plan, text, workdir)
        with db.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO clips (clip_job_id, path, title, start, end, score, mode, description, tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, str(output), clip_title, moment.start, moment.end, moment.score, plan.mode,
                 description, json.dumps(tags, ensure_ascii=False)),
            )
            if settings.get("auto_targets"):
                clip = conn.execute("SELECT * FROM clips WHERE id=?", (cursor.lastrowid,)).fetchone()
                send_clip(conn, clip, settings["auto_targets"])


class ClipWorker:
    """Ekran kartı tek olduğu için klip işleri sırayla, tek tek yapılır."""

    def __init__(self):
        self._stop = threading.Event()

    def run_once(self):
        with db.connect() as conn:
            job = conn.execute(
                "SELECT * FROM clip_jobs WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if job is None:
                return False
            conn.execute("UPDATE clip_jobs SET status='running', error='' WHERE id=?", (job["id"],))
        try:
            result = process(job)
        except Exception as exc:
            if not isinstance(exc, ValueError):
                traceback.print_exc()
            _update(job["id"], status="failed", error=str(exc) or repr(exc), finished_at=time.time())
        else:
            if result != PAUSED:
                _update(job["id"], status="done", stage="Bitti", progress=1.0, finished_at=time.time())
        finally:
            # Whisper boşta ekran kartında kalırsa hikaye görselleri belleğe sığmaz.
            transcribe.release()
        return True

    def run(self):
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception as exc:  # bir hata klip hattını durdurmasın
                print(f"[klip] hata: {exc!r}")
                worked = False
            if not worked:
                self._stop.wait(IDLE_SECONDS)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self._stop.set()
