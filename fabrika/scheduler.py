import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, db, intake
from .uploaders import get_uploader
from .uploaders.base import AuthError, Post, RateLimited, TransientError, UploadError

FOREVER = 4102444800.0  # 2100; elle devam edilene kadar duraklatır


def interval_seconds(value, unit):
    return max(1.0, float(value) * config.UNITS[unit])


def in_window(start, end, moment):
    """Yayın saat aralığında mı bakar; gece yarısını aşan aralıkları da destekler."""
    if not start or not end:
        return True
    current = moment.strftime("%H:%M")
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def start_of_day(moment):
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def is_due(row, now, done_today):
    return bool(
        row["enabled"]
        and now >= row["next_run_at"]
        and now >= row["paused_until"]
        and done_today < row["daily_limit"]
        and in_window(row["window_start"], row["window_end"], datetime.fromtimestamp(now))
    )


class Scheduler:
    def __init__(self):
        self._active = {}
        self._stop = threading.Event()

    def tick(self):
        intake.scan()
        now = time.time()
        with db.connect() as conn:
            intake.archive_finished(conn)
            today = start_of_day(datetime.fromtimestamp(now))
            for row in conn.execute("SELECT * FROM targets").fetchall():
                target = row["target"]
                if target in self._active and self._active[target].is_alive():
                    continue
                if now < row["paused_until"] or not get_uploader(target).connected():
                    continue
                job = db.next_job(conn, target, now, immediate=True)
                if job is None and is_due(row, now, db.done_since(conn, target, today)):
                    job = db.next_job(conn, target, now)
                    if job is not None:
                        conn.execute(
                            "UPDATE targets SET next_run_at=? WHERE target=?",
                            (now + interval_seconds(row["interval_value"], row["interval_unit"]), target),
                        )
                if job is None:
                    continue
                conn.execute("UPDATE jobs SET status='uploading' WHERE id=?", (job["id"],))
                conn.commit()
                worker = threading.Thread(target=self._publish, args=(dict(job), row["privacy"]), daemon=True)
                self._active[target] = worker
                worker.start()

    def _publish(self, job, privacy):
        target = job["target"]
        post = Post(
            path=Path(job["path"]),
            title=job["title"],
            description=job["description"],
            tags=json.loads(job["tags"]),
            privacy=privacy,
        )
        try:
            result = get_uploader(target).publish(post)
        except RateLimited as exc:
            self._requeue(job, str(exc))
            self._pause(target, exc.retry_at, str(exc))
        except AuthError as exc:
            self._requeue(job, str(exc))
            self._pause(target, FOREVER, str(exc))
        except TransientError as exc:
            self._retry_or_fail(job, str(exc))
        except UploadError as exc:
            self._fail(job, str(exc))
        except OSError as exc:  # bağlantı kopması kalıcı hata değildir, sonra yeniden denenir
            self._retry_or_fail(job, f"Bağlantı hatası: {exc}")
        except Exception as exc:  # beklenmeyen hata iş parçacığını sessizce öldürmesin
            self._fail(job, f"Beklenmeyen hata: {exc!r}")
        else:
            # Yorum okumak için platformdaki kimlik gerekir; adresi olmayan platformlar boş döner.
            url, remote_id = result if isinstance(result, tuple) else (result, "")
            with db.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status='done', remote_url=?, remote_id=?, error='', finished_at=? WHERE id=?",
                    (url or "", remote_id or "", time.time(), job["id"]),
                )

    def _requeue(self, job, error):
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET status='queued', error=? WHERE id=?", (error, job["id"]))

    def _retry_or_fail(self, job, error):
        attempts = job["attempts"] + 1
        if attempts >= config.MAX_ATTEMPTS:
            self._fail(job, f"{attempts} denemeden sonra vazgeçildi: {error}")
            return
        with db.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='queued', attempts=?, next_attempt_at=?, error=? WHERE id=?",
                (attempts, time.time() + 60 * 2 ** attempts, error, job["id"]),
            )

    def _fail(self, job, error):
        with db.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                (error, time.time(), job["id"]),
            )

    def _pause(self, target, until, reason):
        with db.connect() as conn:
            for shared in config.SHARED_LIMITS[target]:
                conn.execute(
                    "UPDATE targets SET paused_until=?, paused_reason=? WHERE target=?",
                    (until, reason, shared),
                )

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # tek bir tur hatası fabrikayı durdurmasın
                print(f"[zamanlayıcı] hata: {exc!r}")
            self._stop.wait(config.TICK_SECONDS)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self._stop.set()
