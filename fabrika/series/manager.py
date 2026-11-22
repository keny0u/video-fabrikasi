"""Seçimli dizinin akışı: bölüm üretilir → yayınlanır → oylanır → kazanan seçimle sonraki bölüm üretilir."""
import json
import random
import threading
import time
import traceback

from .. import config, db
from . import votes, writer

TICK_SECONDS = 60
VOTE_REFRESH_SECONDS = 600  # yorumlar 10 dakikada bir sayılır; API kotası boşa harcanmasın
YOUTUBE_TARGETS = ("youtube", "youtube_shorts")
EXTRA_TAGS = ["interaktifhikaye", "seçimlihikaye", "sensec", "dizi"]


def actor_dir(series_id):
    """Dizinin karakter çizimleri burada saklanır; bütün bölümler aynı çizimleri kullanır."""
    return config.CLIPS_DIR / f"dizi_{series_id}" / "oyuncular"


def _series(conn, series_id):
    row = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    return row, json.loads(row["settings"])


def create(premise, settings):
    """Diziyi kaydeder ve 1. bölümün üretimini başlatır; kitapçık o bölümün işinde yazılır."""
    settings = {**settings, "seed": random.randrange(2 ** 31)}
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO series (premise, settings, created_at) VALUES (?, ?, ?)",
            (premise, json.dumps(settings, ensure_ascii=False), time.time()),
        )
        start_episode(conn, cursor.lastrowid, 1, "")
        return cursor.lastrowid


def start_episode(conn, series_id, number, choice):
    series, settings = _series(conn, series_id)
    episode_id = conn.execute(
        "INSERT INTO episodes (series_id, number, choice, created_at) VALUES (?, ?, ?, ?)",
        (series_id, number, choice, time.time()),
    ).lastrowid
    job_settings = {
        "kind": "episode", "series_id": series_id, "episode_id": episode_id, "number": number,
        "voice": settings.get("voice", "kadin"), "style": settings.get("style", "cizgi"),
        "music": settings.get("music", True), "seed": settings["seed"],
        "auto_targets": settings.get("auto_targets", []), "publish_now": True,
    }
    title = f"{series['title'] or 'Yeni dizi'} · Bölüm {number}"
    job_id = conn.execute(
        "INSERT INTO clip_jobs (source, title, settings, created_at) VALUES ('dizi', ?, ?, ?)",
        (title, json.dumps(job_settings, ensure_ascii=False), time.time()),
    ).lastrowid
    conn.execute("UPDATE episodes SET clip_job_id=? WHERE id=?", (job_id, episode_id))
    return episode_id


def _history(conn, series_id, before):
    history = []
    for row in conn.execute("SELECT * FROM episodes WHERE series_id=? AND number<? ORDER BY number",
                            (series_id, before)):
        script = json.loads(row["script"] or "{}")
        history.append({"number": row["number"], "summary": script.get("summary", ""),
                        "option_a": row["option_a"], "option_b": row["option_b"], "winner": row["winner"],
                        "choice": row["option_a"] if row["winner"] == "A" else row["option_b"]})
    return history


def episode_script(settings, stage):
    """Hikaye hattının beklediği senaryo biçiminde bölüm; gerekirse önce dizi kitapçığını yazar."""
    with db.connect() as conn:
        series, series_settings = _series(conn, settings["series_id"])
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (settings["episode_id"],)).fetchone()
        history = _history(conn, series["id"], episode["number"])
    total = series_settings.get("episodes", 10)
    if series["bible"]:
        bible = json.loads(series["bible"])
    else:
        stage("Dizi planlanıyor")
        bible = writer.write_bible(series["premise"], total)
        with db.connect() as conn:
            conn.execute("UPDATE series SET title=?, bible=? WHERE id=?",
                         (bible["title"], json.dumps(bible, ensure_ascii=False), series["id"]))

    stage("Bölüm yazılıyor")
    number = episode["number"]
    script = writer.write_episode(bible, history, number, total, episode["choice"])
    final = number >= total
    with db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET script=?, option_a=?, option_b=? WHERE id=?",
            (json.dumps(script, ensure_ascii=False), "" if final else script["option_a"],
             "" if final else script["option_b"], episode["id"]),
        )
        conn.execute("UPDATE clip_jobs SET title=? WHERE id=?",
                     (f"{bible['title']} · Bölüm {number}", episode["clip_job_id"]))
    label = "FİNAL" if final else f"BÖLÜM {number}"
    return {
        "title": f"{bible['title']} · {'Final' if final else f'Bölüm {number}'}: {script['title']}"[:100],
        "hook": f"{label}\n{script['hook']}",
        "moral": "FİNAL" if final else f"A) {script['option_a']}\nB) {script['option_b']}",
        "scenes": script["scenes"],
        "characters": bible["characters"],
        "actor_dir": str(actor_dir(series["id"])),
        "description": script["caption"],
        "hashtags": [*script["hashtags"], *bible.get("hashtags", []), *EXTRA_TAGS],
    }


def on_ready(episode_id):
    with db.connect() as conn:
        conn.execute("UPDATE episodes SET status='ready' WHERE id=? AND status='producing'", (episode_id,))


def _published(conn, episode):
    return conn.execute(
        """SELECT jobs.target, jobs.remote_url, jobs.remote_id, jobs.finished_at
           FROM jobs JOIN items ON items.id = jobs.item_id
           WHERE items.clip_job_id=? AND jobs.status='done'""",
        (episode["clip_job_id"],),
    ).fetchall()


def refresh_votes(episode_id):
    """Yayınlandığı platformlardaki yorumları okuyup oyları günceller; okunamayanları not düşer."""
    with db.connect() as conn:
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        published = _published(conn, episode)
    comments, problems = [], []
    for post in published:
        try:
            if post["target"] in YOUTUBE_TARGETS:
                comments += votes.youtube_comments(post["remote_url"])
            elif post["target"] == "instagram_reels":
                comments += votes.instagram_comments(post["remote_id"])
        except Exception as exc:  # bir platform okunamasa da diğerleri sayılsın
            name = "YouTube" if post["target"] in YOUTUBE_TARGETS else "Instagram"
            hint = " (yorum izni için Hesaplar'dan YouTube'u yeniden bağla)" if name == "YouTube" else ""
            problems.append(f"{name} yorumları okunamadı{hint}: {str(exc)[:120]}")
    counted = votes.tally(comments)
    with db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET votes_a=?, votes_b=?, counted_at=?, vote_note=? WHERE id=?",
            (counted["A"], counted["B"], time.time(), " | ".join(problems), episode_id),
        )


def decide(episode_id, forced=None):
    """Kazananı belirler (eşitlikte A) ve sonraki bölümü başlatır; son bölümse diziyi bitirir."""
    if not forced:
        refresh_votes(episode_id)
    with db.connect() as conn:
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if episode["status"] not in ("ready", "voting"):
            raise ValueError("Bu bölümün oylaması zaten bitti.")
        series, settings = _series(conn, episode["series_id"])
        a = episode["votes_a"] + episode["manual_a"]
        b = episode["votes_b"] + episode["manual_b"]
        winner = forced or ("A" if a >= b else "B")
        choice = episode["option_a"] if winner == "A" else episode["option_b"]
        conn.execute("UPDATE episodes SET winner=?, status='decided' WHERE id=?", (winner, episode_id))
        if episode["number"] >= settings.get("episodes", 10):
            conn.execute("UPDATE series SET status='finished' WHERE id=?", (series["id"],))
            return None
        return start_episode(conn, series["id"], episode["number"] + 1, f"{winner}: {choice}")


def tick(now=None):
    """Yayınlanan bölümün oylamasını başlatır, oyları tazeler, süresi dolanı sonuçlandırır."""
    now = now or time.time()
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT episodes.*, series.settings AS series_settings FROM episodes
               JOIN series ON series.id = episodes.series_id
               WHERE episodes.status IN ('ready', 'voting') AND series.status='active'""").fetchall()
    for episode in rows:
        settings = json.loads(episode["series_settings"])
        if episode["status"] == "ready":
            with db.connect() as conn:
                published = _published(conn, episode)
                if not published:
                    continue
                start = min(p["finished_at"] for p in published)
                if episode["number"] >= settings.get("episodes", 10):
                    # Finalde oylama yok; yayınlanınca dizi tamamlanır.
                    conn.execute("UPDATE episodes SET status='decided', voting_started=? WHERE id=?",
                                 (start, episode["id"]))
                    conn.execute("UPDATE series SET status='finished' WHERE id=?", (episode["series_id"],))
                    continue
                conn.execute("UPDATE episodes SET status='voting', voting_started=?, deadline=? WHERE id=?",
                             (start, start + settings.get("vote_hours", 20) * 3600, episode["id"]))
            continue
        if not episode["counted_at"] or now - episode["counted_at"] >= VOTE_REFRESH_SECONDS:
            refresh_votes(episode["id"])
        if episode["deadline"] and now >= episode["deadline"]:
            decide(episode["id"])


class SeriesWorker:
    def __init__(self):
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                tick()
            except Exception:  # bir hata dizi döngüsünü durdurmasın
                traceback.print_exc()
            self._stop.wait(TICK_SECONDS)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self._stop.set()
