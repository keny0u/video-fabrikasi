import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    target TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    interval_value REAL NOT NULL DEFAULT 1,
    interval_unit TEXT NOT NULL DEFAULT 'gun',
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    daily_limit INTEGER NOT NULL DEFAULT 10,
    privacy TEXT NOT NULL DEFAULT 'public',
    next_run_at REAL NOT NULL DEFAULT 0,
    paused_until REAL NOT NULL DEFAULT 0,
    paused_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    source_name TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    clip_job_id INTEGER
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    target TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    immediate INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    remote_url TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_target_status ON jobs(target, status);
CREATE TABLE IF NOT EXISTS clip_jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    clip_job_id INTEGER NOT NULL REFERENCES clip_jobs(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    score REAL NOT NULL,
    mode TEXT NOT NULL DEFAULT '',
    sent INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    premise TEXT NOT NULL,
    settings TEXT NOT NULL,
    bible TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    clip_job_id INTEGER,
    status TEXT NOT NULL DEFAULT 'producing',
    script TEXT NOT NULL DEFAULT '',
    choice TEXT NOT NULL DEFAULT '',
    option_a TEXT NOT NULL DEFAULT '',
    option_b TEXT NOT NULL DEFAULT '',
    votes_a INTEGER NOT NULL DEFAULT 0,
    votes_b INTEGER NOT NULL DEFAULT 0,
    manual_a INTEGER NOT NULL DEFAULT 0,
    manual_b INTEGER NOT NULL DEFAULT 0,
    winner TEXT NOT NULL DEFAULT '',
    vote_note TEXT NOT NULL DEFAULT '',
    voting_started REAL,
    deadline REAL,
    counted_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Yarım kalan yükleme tekrar denenmez: platforma ulaşmışsa ikinci kez yayınlanırdı.
INTERRUPTED = "Program yükleme sırasında kapandı. Platformu kontrol et, yayınlanmadıysa tekrar dene."


@contextmanager
def connect():
    """Bağlantıyı açar, hata yoksa kaydeder ve her durumda kapatır."""
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    config.ensure_dirs()
    with connect() as conn:
        conn.executescript(SCHEMA)
        _add_clip_link(conn)
        _add_clip_texts(conn)
        if "remote_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}:
            conn.execute("ALTER TABLE jobs ADD COLUMN remote_id TEXT NOT NULL DEFAULT ''")
        for target in config.TARGETS:
            conn.execute(
                "INSERT OR IGNORE INTO targets (target, daily_limit) VALUES (?, ?)",
                (target, config.DEFAULT_DAILY_LIMITS[target]),
            )
        conn.execute(
            "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE status='uploading'",
            (INTERRUPTED, time.time()),
        )
        # Klip üretimi baştan yapılabilir; yarım kalan iş yeniden sıraya girer.
        conn.execute("UPDATE clip_jobs SET status='queued' WHERE status='running'")


def _add_clip_link(conn):
    """Eski veritabanlarına kuyruktaki klibin hangi klip işinden geldiği bilgisini ekler."""
    if "clip_job_id" in {row["name"] for row in conn.execute("PRAGMA table_info(items)")}:
        return
    conn.execute("ALTER TABLE items ADD COLUMN clip_job_id INTEGER")
    clips = conn.execute("SELECT clip_job_id, path FROM clips").fetchall()
    for item in conn.execute("SELECT id, source_name FROM items").fetchall():
        owners = {c["clip_job_id"] for c in clips if c["path"].replace("\\", "/").endswith("/" + item["source_name"])}
        if len(owners) == 1:  # aynı adlı klipler birden çok işte varsa emin olunamaz, dokunulmaz
            conn.execute("UPDATE items SET clip_job_id=? WHERE id=?", (owners.pop(), item["id"]))


def _add_clip_texts(conn):
    """Eski veritabanlarındaki kliplere açıklama ve etiket alanı ekler."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(clips)")}
    if "description" not in columns:
        conn.execute("ALTER TABLE clips ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if "tags" not in columns:
        conn.execute("ALTER TABLE clips ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")


def add_item(conn, path, source_name, meta):
    cursor = conn.execute(
        "INSERT INTO items (path, source_name, title, description, tags, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            str(path),
            source_name,
            meta["title"],
            meta["description"],
            json.dumps(meta["tags"], ensure_ascii=False),
            time.time(),
        ),
    )
    return cursor.lastrowid


def add_job(conn, item_id, target, error=None, immediate=False, run_at=0):
    """immediate işler aralığı beklemez; run_at verilirse o zamana kadar bekler."""
    conn.execute(
        """INSERT INTO jobs (item_id, target, status, immediate, next_attempt_at, error, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            target,
            "failed" if error else "queued",
            int(immediate),
            run_at,
            error or "",
            time.time() if error else None,
        ),
    )


def next_job(conn, target, now, immediate=False):
    return conn.execute(
        """SELECT jobs.*, items.path, items.title, items.description, items.tags
           FROM jobs JOIN items ON items.id = jobs.item_id
           WHERE jobs.target = ? AND jobs.status = 'queued' AND jobs.immediate = ?
               AND jobs.next_attempt_at <= ?
           ORDER BY jobs.id LIMIT 1""",
        (target, int(immediate), now),
    ).fetchone()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
    else:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def done_since(conn, target, since):
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE target = ? AND status = 'done' AND finished_at >= ?",
        (target, since),
    ).fetchone()[0]
