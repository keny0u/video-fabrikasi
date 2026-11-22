import json
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from . import autostart, config, db, intake
from .scheduler import start_of_day
from .uploaders import get_uploader
from .uploaders.base import read_json, write_json

PANEL_DIR = Path(__file__).parent / "panel"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
TIME_FORMAT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PRIVACY_OPTIONS = {"public", "unlisted", "private"}
JOB_LIST_LIMIT = 300

app = Flask(__name__)
connect_status = {}


@app.before_request
def only_local_requests():
    """Panel yalnızca bu bilgisayardan; başka sitelerin localhost'a istek atması engellenir."""
    if request.host.split(":")[0] not in LOCAL_HOSTS:
        return "Yalnızca yerel erişim", 403
    origin = request.headers.get("Origin")
    if request.method == "POST" and origin and urlparse(origin).hostname not in LOCAL_HOSTS:
        return "Yalnızca yerel erişim", 403


def error(message, status=400):
    return jsonify({"error": message}), status


@app.get("/")
def index():
    return send_from_directory(PANEL_DIR, "index.html")


def _tiktok_settings():
    from .uploaders import tiktok

    return read_json(tiktok.CONFIG_FILE) if tiktok.CONFIG_FILE.exists() else {}


def _accounts():
    from .uploaders import instagram, youtube

    tiktok_settings = _tiktok_settings()
    instagram_settings = read_json(instagram.CONFIG_FILE) if instagram.CONFIG_FILE.exists() else {}
    return {
        "youtube": {
            "connected": get_uploader("youtube").connected(),
            "client_ready": youtube.CLIENT_FILE.exists(),
            "cookies": _youtube_cookies_ready(),
            "status": connect_status.get("youtube", ""),
        },
        "tiktok": {
            "connected": get_uploader("tiktok").connected(),
            "client_ready": "client_key" in tiktok_settings,
            "client_key": tiktok_settings.get("client_key", ""),
            "mode": tiktok_settings.get("mode", "inbox"),
            "status": connect_status.get("tiktok", ""),
        },
        "claude": {"connected": _claude_configured()},
        "instagram": {
            "connected": get_uploader("instagram_reels").connected(),
            "ig_user_id": instagram_settings.get("ig_user_id", ""),
        },
    }


@app.get("/api/state")
def state():
    today = start_of_day(datetime.now())
    with db.connect() as conn:
        targets = []
        for row in conn.execute("SELECT * FROM targets").fetchall():
            target = row["target"]
            count = lambda status: conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE target=? AND status=?", (target, status)
            ).fetchone()[0]
            targets.append({
                **dict(row),
                "label": config.TARGETS[target],
                "connected": get_uploader(target).connected(),
                "queued": count("queued"),
                "uploading": count("uploading"),
                "failed": count("failed"),
                "done_today": db.done_since(conn, target, today),
            })
        jobs = conn.execute(
            """SELECT jobs.id, jobs.target, jobs.status, jobs.immediate, jobs.attempts,
                      jobs.next_attempt_at, jobs.remote_url, jobs.error, jobs.finished_at,
                      items.title, items.source_name
               FROM jobs JOIN items ON items.id = jobs.item_id
               ORDER BY CASE jobs.status WHEN 'uploading' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                        CASE WHEN jobs.status IN ('uploading', 'queued') THEN jobs.id END,
                        jobs.finished_at DESC
               LIMIT ?""",
            (JOB_LIST_LIMIT,),
        ).fetchall()
    return jsonify({
        "now": time.time(),
        "dry_run": config.DRY_RUN,
        "targets": targets,
        "jobs": [dict(job) for job in jobs],
        "accounts": _accounts(),
        "autostart": autostart.is_enabled(),
        "clip_jobs": _clip_jobs(),
        **_clip_settings(),
        "series": _series_state(),
    })


def _series_state():
    with db.connect() as conn:
        result = []
        for row in conn.execute("SELECT * FROM series ORDER BY id DESC LIMIT 20"):
            settings = json.loads(row["settings"])
            episodes = []
            for ep in conn.execute("SELECT * FROM episodes WHERE series_id=? ORDER BY number DESC", (row["id"],)):
                script = json.loads(ep["script"] or "{}")
                episodes.append({
                    key: ep[key] for key in ("id", "number", "status", "option_a", "option_b", "votes_a", "votes_b",
                                             "manual_a", "manual_b", "winner", "deadline", "vote_note", "clip_job_id")
                } | {"title": script.get("title", ""), "question": script.get("question", "")})
            result.append({"id": row["id"], "title": row["title"] or "Dizi planlanıyor…", "status": row["status"],
                           "premise": row["premise"][:200], "settings": settings, "episodes": episodes})
    return result


def _clip_settings():
    with db.connect() as conn:
        return {
            "facecam": db.get_setting(conn, "facecam"),
            "facecam_ask": db.get_setting(conn, "facecam_ask", True),
        }


def _clip_jobs():
    with db.connect() as conn:
        jobs = [dict(row) for row in conn.execute(
            "SELECT id, source, title, status, stage, progress, error, settings FROM clip_jobs ORDER BY id DESC LIMIT 30"
        )]
        for job in jobs:
            job["settings"] = json.loads(job["settings"])
            job["clips"] = [dict(row) for row in conn.execute(
                "SELECT id, title, description, start, end, score, mode, sent FROM clips WHERE clip_job_id=? ORDER BY score DESC",
                (job["id"],),
            )]
    return jobs


def _parse_when(data):
    """Yayın zamanını (anında mı, hangi zamanda) çözer; form ya da JSON alır."""
    when = data.get("when", "queue")
    if when == "queue":
        return False, 0
    if when == "now":
        return True, 0
    if when == "at":
        return True, datetime.fromisoformat(data["at"]).timestamp()
    raise ValueError(when)


@app.post("/api/upload")
def upload():
    targets = [t for t in config.TARGETS if t in request.form.getlist("targets")]
    files = request.files.getlist("files")
    if not targets:
        return error("En az bir platform seç.")
    if not files:
        return error("En az bir dosya seç.")
    try:
        immediate, run_at = _parse_when(request.form)
    except (KeyError, ValueError):
        return error("Yayın zamanı geçersiz.")

    tags = [tag.strip().lstrip("#") for tag in request.form.get("tags", "").split(",") if tag.strip()]
    allowed = config.VIDEO_EXTS | config.IMAGE_EXTS
    rejected = [f.filename for f in files if Path(f.filename).suffix.lower() not in allowed]
    if rejected:
        return error(f"Desteklenmeyen dosya: {', '.join(rejected)}")

    with db.connect() as conn:
        for file in files:
            stored = intake.library_path(file.filename)
            file.save(stored)
            meta = {
                "title": request.form.get("title", "").strip() or Path(file.filename).stem.replace("_", " "),
                "description": request.form.get("description", "").strip(),
                "tags": tags,
            }
            intake.ingest(conn, stored, Path(file.filename).name, targets, meta, immediate, run_at)
            conn.commit()
    return jsonify({"ok": True, "files": len(files), "targets": len(targets)})


@app.post("/api/targets/<target>")
def save_target(target):
    if target not in config.TARGETS:
        return error("Bilinmeyen platform.", 404)
    data = request.get_json()
    try:
        value = float(data["interval_value"])
        limit = int(data["daily_limit"])
    except (KeyError, TypeError, ValueError):
        return error("Sayı alanları geçersiz.")
    unit, start, end = data.get("interval_unit"), data.get("window_start", ""), data.get("window_end", "")
    privacy = data.get("privacy", "public")
    if value <= 0 or limit < 0 or unit not in config.UNITS or privacy not in PRIVACY_OPTIONS:
        return error("Ayarlar geçersiz.")
    if bool(start) != bool(end) or any(t and not TIME_FORMAT.match(t) for t in (start, end)):
        return error("Saat aralığı SS:DD biçiminde olmalı, ikisi de dolu ya da boş kalmalı.")
    with db.connect() as conn:
        conn.execute(
            """UPDATE targets SET enabled=?, interval_value=?, interval_unit=?, window_start=?,
               window_end=?, daily_limit=?, privacy=? WHERE target=?""",
            (int(bool(data.get("enabled"))), value, unit, start, end, limit, privacy, target),
        )
    return jsonify({"ok": True})


def _resume(*targets):
    with db.connect() as conn:
        for target in targets:
            conn.execute("UPDATE targets SET paused_until=0, paused_reason='' WHERE target=?", (target,))


@app.post("/api/targets/<target>/resume")
def resume(target):
    if target not in config.TARGETS:
        return error("Bilinmeyen platform.", 404)
    _resume(*config.SHARED_LIMITS[target])
    return jsonify({"ok": True})


@app.post("/api/jobs/<int:job_id>/<action>")
def job_action(job_id, action):
    with db.connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return error("İş bulunamadı.", 404)
        if job["status"] == "uploading":
            return error("Yüklenen işe şu an dokunulamaz.")
        if action == "now" and job["status"] == "queued":
            conn.execute("UPDATE jobs SET immediate=1, next_attempt_at=0 WHERE id=?", (job_id,))
        elif action == "retry" and job["status"] == "failed":
            conn.execute(
                "UPDATE jobs SET status='queued', attempts=0, next_attempt_at=0, error='', finished_at=NULL WHERE id=?",
                (job_id,),
            )
        elif action == "delete" and job["status"] in ("queued", "failed"):
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            _remove_orphan_item(conn, job["item_id"])
        else:
            return error("Bu işlem bu durumda yapılamaz.")
    return jsonify({"ok": True})


def _remove_orphan_item(conn, item_id):
    """Hiç işi kalmayan dosya kütüphaneden silinir, arşive dokunulmaz."""
    if conn.execute("SELECT 1 FROM jobs WHERE item_id=?", (item_id,)).fetchone():
        return
    path = Path(conn.execute("SELECT path FROM items WHERE id=?", (item_id,)).fetchone()["path"])
    conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    if path.parent == config.LIBRARY:
        path.unlink(missing_ok=True)


def _connect_in_background(name, connect, *targets):
    """İzin ekranı kullanıcı onaylayana kadar bekler; isteği kilitlememek için ayrı çalışır."""

    def worker():
        connect_status[name] = "Tarayıcıda izin bekleniyor…"
        try:
            connect()
        except Exception as exc:
            connect_status[name] = f"Bağlanamadı: {exc}"
        else:
            connect_status[name] = ""
            _resume(*targets)

    threading.Thread(target=worker, daemon=True).start()


@app.post("/api/accounts/youtube/client")
def youtube_client():
    from .uploaders import youtube

    file = request.files.get("file")
    if file is None:
        return error("client_secret JSON dosyasını seç.")
    try:
        data = json.loads(file.read().decode("utf-8-sig"))
    except ValueError:
        return error("Dosya JSON değil.")
    if "installed" not in data:
        return error("Masaüstü uygulaması (Desktop app) türünde OAuth istemcisi gerekli.")
    write_json(youtube.CLIENT_FILE, data)
    return jsonify({"ok": True})


def _youtube_cookies_ready():
    from .clips import download

    return download.COOKIE_FILE.exists()


@app.post("/api/accounts/youtube/cookies")
def youtube_cookies():
    """Video indirmek için YouTube çerezlerini (cookies.txt) kaydeder; dosyasız gelirse siler."""
    from .clips import download

    file = request.files.get("file")
    if file is None:
        download.COOKIE_FILE.unlink(missing_ok=True)
        return jsonify({"ok": True})
    text = file.read().decode("utf-8-sig", errors="replace")
    if not download.looks_like_cookie_file(text):
        return error("Bu dosyada YouTube çerezi yok. youtube.com açıkken dışa aktarılmış bir cookies.txt seç.")
    download.COOKIE_FILE.write_text(text, encoding="utf-8")
    return jsonify({"ok": True})


@app.post("/api/accounts/youtube/connect")
def youtube_connect():
    from .uploaders import youtube

    if not youtube.CLIENT_FILE.exists():
        return error("Önce client_secret dosyasını yükle.")
    _connect_in_background("youtube", youtube.connect, "youtube", "youtube_shorts")
    return jsonify({"ok": True})


@app.post("/api/accounts/tiktok")
def tiktok_save():
    from .uploaders import tiktok

    data = request.get_json()
    settings = _tiktok_settings()
    settings["client_key"] = (data.get("client_key") or settings.get("client_key", "")).strip()
    settings["client_secret"] = (data.get("client_secret") or settings.get("client_secret", "")).strip()
    settings["mode"] = "direct" if data.get("mode") == "direct" else "inbox"
    if not settings["client_key"] or not settings["client_secret"]:
        return error("Client key ve client secret gerekli.")
    write_json(tiktok.CONFIG_FILE, settings)
    return jsonify({"ok": True})


@app.post("/api/accounts/tiktok/connect")
def tiktok_connect():
    from .uploaders import tiktok

    if "client_key" not in _tiktok_settings():
        return error("Önce client key ve secret kaydet.")
    _connect_in_background("tiktok", tiktok.connect, "tiktok")
    return jsonify({"ok": True})


@app.post("/api/accounts/instagram")
def instagram_save():
    from .uploaders import instagram

    data = request.get_json()
    ig_user_id = str(data.get("ig_user_id", "")).strip()
    token = str(data.get("access_token", "")).strip()
    if not ig_user_id.isdigit() or not token:
        return error("Instagram hesap kimliği (sayı) ve erişim anahtarı gerekli.")
    write_json(instagram.CONFIG_FILE, {"ig_user_id": ig_user_id, "access_token": token})
    _resume("instagram_reels")
    return jsonify({"ok": True})


CLIP_LIMITS = {"count": (1, 20), "min_len": (5, 170), "max_len": (10, 180)}
LAYOUTS = ("auto", "split", "split_fit")
MODES = ("genel", "korku", "komik", "elle", "kelime")
CAMERA_SIZES = ("kucuk", "orta", "buyuk")
FRAME_POINTS = (0.1, 0.3, 0.5, 0.7, 0.9)


@app.post("/api/clips")
def create_clip_job():
    """Link ya da dosyadan klip üretim işi açar."""
    try:
        settings = {name: int(request.form[name]) for name in CLIP_LIMITS}
    except (KeyError, ValueError):
        return error("Klip ayarları geçersiz.")
    if any(not low <= settings[name] <= high for name, (low, high) in CLIP_LIMITS.items()):
        return error("Klip ayarları izin verilen aralığın dışında.")
    if settings["min_len"] >= settings["max_len"]:
        return error("En kısa süre en uzun süreden küçük olmalı.")
    settings["language"] = request.form.get("language", "") if request.form.get("language") in ("tr", "en") else ""
    settings["subtitles"] = request.form.get("subtitles") == "1"
    settings["auto_targets"] = [t for t in config.TARGETS if t in request.form.getlist("auto_targets")]
    settings["layout"] = request.form.get("layout") if request.form.get("layout") in LAYOUTS else "auto"
    settings["camera_size"] = request.form.get("camera_size") if request.form.get("camera_size") in CAMERA_SIZES else "orta"
    settings["mode"] = request.form.get("mode") if request.form.get("mode") in MODES else "genel"
    if settings["mode"] == "elle":
        from .clips.events import parse_ranges

        settings["manual"] = request.form.get("manual", "")
        try:
            if not parse_ranges(settings["manual"]):
                return error("Zaman aralıklarını 12:30-13:10 biçiminde yaz.")
        except ValueError as exc:
            return error(str(exc))
    if settings["mode"] == "kelime":
        settings["keyword"] = request.form.get("keyword", "").strip()
        if not settings["keyword"]:
            return error("Aranacak kelimeyi yaz.")

    url = request.form.get("url", "").strip()
    file = request.files.get("file")
    if url:
        if not url.startswith(("http://", "https://")):
            return error("Geçerli bir link gir.")
        source = url
    elif file:
        if Path(file.filename).suffix.lower() not in config.VIDEO_EXTS | {".mkv"}:
            return error("Desteklenmeyen video dosyası.")
        stored = config.CLIPS_DIR / "gelen" / intake.library_path(file.filename).name
        file.save(stored)
        source = str(stored)
    else:
        return error("Link gir ya da video dosyası seç.")

    from .clips.pipeline import workdir_for

    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO clip_jobs (source, settings, created_at) VALUES (?, ?, ?)",
            (source, json.dumps(settings), time.time()),
        )
        # Silinen işin numarası yeniden verilebilir; eski indirme ve yazıya döküm karışmasın.
        shutil.rmtree(workdir_for(cursor.lastrowid), ignore_errors=True)
    return jsonify({"ok": True})


STORY_LIMITS = (80, 8000)
STORY_VOICES = ("kadin", "erkek", "ahmet", "emel")
STORY_STYLES = ("cizgi", "gercekci", "korku", "anime")


@app.post("/api/stories")
def create_story():
    """Hikaye metninden Shorts videosu üretme işi açar."""
    from .clips import ai_titles
    from .clips.pipeline import workdir_for

    data = request.get_json()
    story = str(data.get("story", "")).strip()
    if not STORY_LIMITS[0] <= len(story) <= STORY_LIMITS[1]:
        return error(f"Hikaye {STORY_LIMITS[0]}-{STORY_LIMITS[1]} karakter arası olmalı.")
    if not ai_titles.is_configured():
        return error("Hikaye videoları için Claude anahtarı gerekli: Hesaplar → Claude bölümüne gir.")
    settings = {
        "kind": "story",
        "story": story,
        "voice": data.get("voice") if data.get("voice") in STORY_VOICES else "kadin",
        "style": data.get("style") if data.get("style") in STORY_STYLES else "cizgi",
        "music": bool(data.get("music", True)),
        "auto_targets": [t for t in config.TARGETS if t in data.get("auto_targets", [])],
    }
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO clip_jobs (source, title, settings, created_at) VALUES ('hikaye', ?, ?, ?)",
            (story[:60], json.dumps(settings, ensure_ascii=False), time.time()),
        )
        shutil.rmtree(workdir_for(cursor.lastrowid), ignore_errors=True)
    return jsonify({"ok": True})


SERIES_LIMITS = {"episodes": (3, 20), "vote_hours": (1, 72)}


@app.post("/api/series")
def create_series():
    """Seçimli dizi açar; kitapçık ve 1. bölüm arka planda yazılıp üretilir."""
    from .clips import ai_titles
    from .series import manager

    data = request.get_json()
    premise = str(data.get("premise", "")).strip()
    if not 30 <= len(premise) <= 4000:
        return error("Dizi konusu 30-4000 karakter arası olmalı.")
    if not ai_titles.is_configured():
        return error("Dizi için Claude anahtarı gerekli: Hesaplar → Claude bölümüne gir.")
    try:
        numbers = {name: int(data.get(name, default)) for name, default in (("episodes", 10), ("vote_hours", 20))}
    except (TypeError, ValueError):
        return error("Bölüm sayısı ve oylama süresi sayı olmalı.")
    if any(not low <= numbers[name] <= high for name, (low, high) in SERIES_LIMITS.items()):
        return error("Bölüm sayısı 3-20, oylama süresi 1-72 saat arası olmalı.")
    settings = {
        **numbers,
        "voice": data.get("voice") if data.get("voice") in STORY_VOICES else "kadin",
        "style": data.get("style") if data.get("style") in STORY_STYLES else "korku",
        "music": bool(data.get("music", True)),
        "auto_targets": [t for t in config.TARGETS if t in data.get("auto_targets", [])],
    }
    return jsonify({"ok": True, "id": manager.create(premise, settings)})


@app.post("/api/series/<int:series_id>/<action>")
def series_action(series_id, action):
    from .series import manager

    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM series WHERE id=?", (series_id,)).fetchone() is None:
            return error("Dizi bulunamadı.", 404)
        if action == "stop":
            conn.execute("UPDATE series SET status='stopped' WHERE id=?", (series_id,))
        elif action == "resume":
            conn.execute("UPDATE series SET status='active' WHERE id=? AND status='stopped'", (series_id,))
        elif action == "delete":
            conn.execute("DELETE FROM episodes WHERE series_id=?", (series_id,))
            conn.execute("DELETE FROM series WHERE id=?", (series_id,))
            shutil.rmtree(manager.actor_dir(series_id).parent, ignore_errors=True)
        else:
            return error("Bilinmeyen işlem.")
    return jsonify({"ok": True})


@app.post("/api/episodes/<int:episode_id>/<action>")
def episode_action(episode_id, action):
    from .series import manager

    with db.connect() as conn:
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if episode is None:
            return error("Bölüm bulunamadı.", 404)
        if action == "votes":
            data = request.get_json()
            try:
                add_a, add_b = int(data.get("a", 0)), int(data.get("b", 0))
            except (TypeError, ValueError):
                return error("Oy sayısı geçersiz.")
            conn.execute("UPDATE episodes SET manual_a=MAX(0, manual_a+?), manual_b=MAX(0, manual_b+?) WHERE id=?",
                         (add_a, add_b, episode_id))
            return jsonify({"ok": True})
        if action == "start-voting":
            if episode["status"] != "ready":
                return error("Oylama yalnızca hazır bölümde başlatılabilir.")
            settings = json.loads(conn.execute("SELECT settings FROM series WHERE id=?",
                                               (episode["series_id"],)).fetchone()[0])
            now = time.time()
            conn.execute("UPDATE episodes SET status='voting', voting_started=?, deadline=? WHERE id=?",
                         (now, now + settings.get("vote_hours", 20) * 3600, episode_id))
            return jsonify({"ok": True})
    try:
        if action == "count":
            manager.refresh_votes(episode_id)
        elif action in ("decide", "decide-a", "decide-b"):
            manager.decide(episode_id, {"decide-a": "A", "decide-b": "B"}.get(action))
        else:
            return error("Bilinmeyen işlem.")
    except ValueError as exc:
        return error(str(exc))
    return jsonify({"ok": True})


@app.post("/api/clip-jobs/<int:job_id>/titles")
def clip_job_titles(job_id):
    from .clips.pipeline import refresh_titles

    with db.connect() as conn:
        job = conn.execute("SELECT status FROM clip_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        return error("İş bulunamadı.", 404)
    if job["status"] == "running":
        return error("Çalışan işe şu an dokunulamaz.")
    try:
        refresh_titles(job_id)
    except ValueError as exc:
        return error(str(exc))
    return jsonify({"ok": True})


@app.post("/api/clip-jobs/<int:job_id>/<action>")
def clip_job_action(job_id, action):
    from .clips.pipeline import workdir_for

    with db.connect() as conn:
        job = conn.execute("SELECT * FROM clip_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return error("İş bulunamadı.", 404)
        if job["status"] == "running":
            return error("Çalışan işe şu an dokunulamaz.")
        if action in ("retry", "rerun") and job["status"] in ("failed", "done"):
            settings = json.loads(job["settings"])
            changes = request.get_json(silent=True) or {}
            if changes.get("layout") in LAYOUTS:
                settings["layout"] = changes["layout"]
            if changes.get("camera_size") in CAMERA_SIZES:
                settings["camera_size"] = changes["camera_size"]
            if changes.get("mode") in ("genel", "korku", "komik"):
                settings["mode"] = changes["mode"]
            conn.execute(
                "UPDATE clip_jobs SET status='queued', error='', progress=0, settings=? WHERE id=?",
                (json.dumps(settings), job_id),
            )
        elif action == "delete":
            conn.execute("DELETE FROM clips WHERE clip_job_id=?", (job_id,))
            conn.execute("DELETE FROM clip_jobs WHERE id=?", (job_id,))
            shutil.rmtree(workdir_for(job_id), ignore_errors=True)
            if Path(job["source"]).parent == config.CLIPS_DIR / "gelen":
                Path(job["source"]).unlink(missing_ok=True)
        else:
            return error("Bu işlem bu durumda yapılamaz.")
    return jsonify({"ok": True})


def _parse_box(data):
    """Paneldeki kutuyu (oranlar) doğrular; geçersizse None."""
    try:
        x, y, w, h = (float(data[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= x < 1 and 0 <= y < 1 and 0.03 <= w and 0.03 <= h and x + w <= 1.001 and y + h <= 1.001):
        return None
    return [x, y, min(w, 1 - x), min(h, 1 - y)]


@app.post("/api/clip-jobs/<int:job_id>/facecam")
def save_job_facecam(job_id):
    """Bu videonun kamera kutusunu kaydeder; bekleyen iş kaldığı yerden devam eder."""
    box = _parse_box(request.get_json())
    if box is None:
        return error("Kutu çok küçük ya da görüntünün dışında.")
    with db.connect() as conn:
        job = conn.execute("SELECT * FROM clip_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return error("İş bulunamadı.", 404)
        if job["status"] == "running":
            return error("Çalışan işe şu an dokunulamaz.")
        settings = json.loads(job["settings"])
        settings["facecam"] = box
        status = "queued" if job["status"] == "waiting_box" else job["status"]
        conn.execute("UPDATE clip_jobs SET settings=?, status=? WHERE id=?", (json.dumps(settings), status, job_id))
        db.set_setting(conn, "facecam", box)  # sonraki videolar için varsayılan
    return jsonify({"ok": True})


@app.post("/api/settings")
def save_settings():
    data = request.get_json()
    with db.connect() as conn:
        if "facecam_ask" in data:
            db.set_setting(conn, "facecam_ask", bool(data["facecam_ask"]))
    return jsonify({"ok": True})


@app.get("/api/clip-jobs/<int:job_id>/kare.jpg")
def clip_job_frame(job_id):
    """Kamera kutusunu işaretlemek için kaynak videodan bir kare; at: videonun neresi (0-1)."""
    from . import media
    from .clips.pipeline import source_file, workdir_for

    with db.connect() as conn:
        job = conn.execute("SELECT * FROM clip_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        abort(404)
    at = min(FRAME_POINTS, key=lambda p: abs(p - request.args.get("at", 0.3, type=float)))
    frame = workdir_for(job_id) / f"kare_{int(at * 100)}.jpg"
    if not frame.exists():
        source = source_file(job)
        info = media.probe(source) if source else None
        if info is None:
            return error("Bu işin videosu henüz hazır değil.", 404)
        frame.parent.mkdir(parents=True, exist_ok=True)
        media._run([
            "-y", "-ss", f"{info.duration * at:.1f}", "-i", str(source),
            "-frames:v", "1", "-vf", "scale=1280:-2", str(frame),
        ])
        if not frame.exists():
            return error("Kare alınamadı.", 500)
    return send_file(frame, mimetype="image/jpeg")


def _clip_or_404(conn, clip_id):
    clip = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if clip is None:
        abort(404)
    return clip


@app.get("/klip/<int:clip_id>.mp4")
def clip_file(clip_id):
    with db.connect() as conn:
        path = _clip_or_404(conn, clip_id)["path"]
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.post("/api/clips/<int:clip_id>/send")
def send_clip(clip_id):
    from .clips.pipeline import send_clip as queue_clip

    data = request.get_json()
    targets = [t for t in config.TARGETS if t in data.get("targets", [])]
    if not targets:
        return error("En az bir platform seç.")
    try:
        immediate, run_at = _parse_when(data)
    except (KeyError, ValueError):
        return error("Yayın zamanı geçersiz.")
    title = str(data.get("title", "")).strip()
    description = data.get("description")
    with db.connect() as conn:
        queue_clip(conn, _clip_or_404(conn, clip_id), targets, immediate, run_at, title or None,
                   None if description is None else str(description))
    return jsonify({"ok": True})


@app.post("/api/clips/<int:clip_id>/delete")
def delete_clip(clip_id):
    with db.connect() as conn:
        clip = _clip_or_404(conn, clip_id)
        conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))
    Path(clip["path"]).unlink(missing_ok=True)
    return jsonify({"ok": True})


def _claude_configured():
    from .clips import ai_titles

    return ai_titles.is_configured()


@app.post("/api/accounts/claude")
def claude_save():
    """Claude API anahtarını ücretsiz bir istekle doğrular ve kaydeder; boş gelirse siler."""
    import anthropic

    from .clips import ai_titles

    key = str(request.get_json().get("api_key", "")).strip()
    if not key:
        ai_titles.KEY_FILE.unlink(missing_ok=True)
        return jsonify({"ok": True})
    if not key.startswith("sk-ant-"):
        return error("Bu bir Claude API anahtarına benzemiyor (sk-ant- ile başlamalı).")
    try:
        anthropic.Anthropic(api_key=key, max_retries=ai_titles.MAX_RETRIES).models.retrieve(ai_titles.MODEL)
    except anthropic.AuthenticationError:
        return error("Anahtar geçersiz.")
    except anthropic.APIError as exc:
        return error(f"Anahtar doğrulanamadı: {exc}")
    write_json(ai_titles.KEY_FILE, {"api_key": key})
    return jsonify({"ok": True})


@app.post("/api/autostart")
def set_autostart():
    if request.get_json().get("enabled"):
        autostart.enable()
    else:
        autostart.disable()
    return jsonify({"ok": True})
