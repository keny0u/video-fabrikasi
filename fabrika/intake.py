import json
import shutil
import time
import uuid
from pathlib import Path

from . import config, db, media

SIDECAR_EXTS = (".json", ".txt")


def _read_sidecar(file):
    """Dosyayla aynı adlı .json ya da .txt varsa başlık bilgilerini okur."""
    meta = {"title": file.stem.replace("_", " ").strip(), "description": "", "tags": []}
    json_path = file.with_suffix(".json")
    txt_path = file.with_suffix(".txt")
    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8-sig"))
        meta["title"] = str(data.get("title") or meta["title"])
        meta["description"] = str(data.get("description") or "")
        meta["tags"] = [str(tag) for tag in data.get("tags") or []]
    elif txt_path.exists():
        lines = txt_path.read_text(encoding="utf-8-sig").splitlines()
        if lines and lines[0].strip():
            meta["title"] = lines[0].strip()
        meta["description"] = "\n".join(lines[1:]).strip()
    return meta


def _is_stable(file, now):
    return now - file.stat().st_mtime >= config.STABLE_SECONDS


def library_path(name):
    return config.LIBRARY / f"{uuid.uuid4().hex[:8]}_{Path(name).name}"


def ingest(conn, stored, source_name, targets, meta, immediate=False, run_at=0):
    """Kütüphanedeki dosyayı gerekirse videoya çevirir, her hedef için iş açar."""
    error = None
    if stored.suffix.lower() in config.IMAGE_EXTS:
        video = stored.with_suffix(".mp4")
        try:
            media.image_to_video(stored, video, vertical=targets != ["youtube"])
            stored.unlink()
            stored = video
        except ValueError as exc:
            error = f"Görsel videoya çevrilemedi: {exc}"

    info = None if error else media.probe(stored)
    item_id = db.add_item(conn, stored, source_name, meta)
    for target in targets:
        problem = error or media.check(target, info, stored.suffix.lower())
        db.add_job(conn, item_id, target, problem, immediate, run_at)
    return item_id


def _ingest_from_inbox(conn, file, targets):
    try:
        meta = _read_sidecar(file)
    except (ValueError, OSError) as exc:
        print(f"[gelen] {file.name}: yan dosya okunamadı, bekliyor ({exc})")
        return False

    stored = library_path(file.name)
    try:
        shutil.move(str(file), stored)
    except PermissionError:
        return False  # dosya hâlâ başka programda açık, sonraki turda denenir
    for ext in SIDECAR_EXTS:
        file.with_suffix(ext).unlink(missing_ok=True)
    ingest(conn, stored, file.name, targets, meta)
    return True


def scan():
    """inbox klasörlerini tarar, hazır dosyaları kuyruğa alır; alınan dosya sayısını döner."""
    added = 0
    now = time.time()
    with db.connect() as conn:
        for folder, targets in config.FOLDER_TARGETS.items():
            directory = config.INBOX / folder
            if not directory.is_dir():
                continue
            for file in sorted(directory.iterdir()):
                suffix = file.suffix.lower()
                if suffix not in config.VIDEO_EXTS | config.IMAGE_EXTS or not _is_stable(file, now):
                    continue
                if _ingest_from_inbox(conn, file, targets):
                    conn.commit()
                    added += 1
    return added


def archive_finished(conn):
    """Bütün işleri başarıyla biten dosyaları arsiv klasörüne taşır."""
    rows = conn.execute(
        """SELECT id, path FROM items
           WHERE path NOT LIKE ? AND NOT EXISTS
               (SELECT 1 FROM jobs WHERE jobs.item_id = items.id AND jobs.status != 'done')""",
        (str(config.ARCHIVE) + "%",),
    ).fetchall()
    for row in rows:
        destination = config.ARCHIVE / Path(row["path"]).name
        try:
            shutil.move(row["path"], destination)
        except OSError as exc:
            print(f"[arşiv] {row['path']}: taşınamadı ({exc})")
            continue
        conn.execute("UPDATE items SET path = ? WHERE id = ?", (str(destination), row["id"]))
