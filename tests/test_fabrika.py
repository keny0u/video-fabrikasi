import io
import os
import time
from datetime import datetime
from unittest import mock

import pytest

from fabrika import config, db, intake, media, scheduler
from fabrika.uploaders import tiktok
from fabrika.uploaders.base import AuthError, Post, RateLimited, TransientError
from fabrika.uploaders.youtube import build_body
from fabrika.web import app

def make_video(path, size="1080x1920", seconds=4):
    result = media._run([
        "-y", "-f", "lavfi", "-i", f"color=c=blue:s={size}:d={seconds}:r=30",
        "-pix_fmt", "yuv420p", str(path),
    ])
    assert result.returncode == 0, result.stderr


def wait_jobs(sched, statuses, count, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        sched.tick()
        with db.connect() as conn:
            found = conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(statuses))})", statuses
            ).fetchone()[0]
        if found >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"{count} iş {statuses} durumuna gelmedi")


def enable(target, **fields):
    values = {"enabled": 1, "interval_value": 1, "interval_unit": "sn", **fields}
    assignments = ", ".join(f"{key}=?" for key in values)
    with db.connect() as conn:
        conn.execute(f"UPDATE targets SET {assignments} WHERE target=?", (*values.values(), target))


def statuses():
    with db.connect() as conn:
        return {r["target"]: r["status"] for r in conn.execute("SELECT target, status FROM jobs")}


def test_interval_units():
    assert scheduler.interval_seconds(30, "sn") == 30
    assert scheduler.interval_seconds(5, "dk") == 300
    assert scheduler.interval_seconds(2, "saat") == 7200
    assert scheduler.interval_seconds(1, "gun") == 86400


@pytest.mark.parametrize("start,end,hour,expected", [
    ("", "", 3, True),
    ("09:00", "18:00", 12, True),
    ("09:00", "18:00", 20, False),
    ("22:00", "02:00", 23, True),
    ("22:00", "02:00", 1, True),
    ("22:00", "02:00", 12, False),
])
def test_window(start, end, hour, expected):
    assert scheduler.in_window(start, end, datetime(2026, 1, 1, hour)) is expected


def test_chunk_plan_matches_tiktok_example():
    assert tiktok.chunk_plan(50_000_123) == (tiktok.CHUNK_SIZE, 4)
    size, count = 50_000_123, 5  # TikTok dokümanındaki sayılar
    ranges = list(tiktok.chunk_ranges(size, 10_000_000, count))
    assert ranges[0] == (0, 9_999_999)
    assert ranges[-1] == (40_000_000, size - 1)
    assert tiktok.chunk_plan(4_194_304) == (4_194_304, 1)


def test_chunks_cover_every_byte():
    size = 123 * 1024 * 1024 + 7
    chunk, count = tiktok.chunk_plan(size)
    ranges = list(tiktok.chunk_ranges(size, chunk, count))
    assert sum(end - start + 1 for start, end in ranges) == size
    assert max(end - start + 1 for start, end in ranges) <= 128 * 1024 * 1024


def test_code_challenge_is_hex():
    assert tiktok.code_challenge("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_shorts_title_and_limit():
    body = build_body(Post(path=None, title="x" * 120), shorts=True)
    assert body["snippet"]["title"].endswith("#Shorts")
    assert len(body["snippet"]["title"]) <= 100
    assert "#Shorts" not in build_body(Post(path=None, title="video"), shorts=False)["snippet"]["title"]


def test_check_rejects_horizontal_shorts():
    info = media.VideoInfo(1920, 1080, 30, 30, 1000)
    assert "dikey" in media.check("youtube_shorts", info, ".mp4")
    assert media.check("youtube", info, ".mp4") is None


def test_inbox_folder_fans_out_and_interval_spaces_posts(tmp_path):
    file = config.INBOX / "dikey" / "ilk_video.mp4"
    make_video(file)
    (config.INBOX / "dikey" / "ilk_video.txt").write_text("Baslik\nAciklama", encoding="utf-8")
    os.utime(file, (time.time() - 60, time.time() - 60))

    assert intake.scan() == 1
    assert not file.exists()
    with db.connect() as conn:
        assert conn.execute("SELECT title, description FROM items").fetchone()[0] == "Baslik"
    assert set(statuses()) == {"youtube_shorts", "tiktok", "instagram_reels"}

    enable("tiktok")
    sched = scheduler.Scheduler()
    wait_jobs(sched, ("done",), 1)
    assert statuses()["tiktok"] == "done"
    assert statuses()["youtube_shorts"] == "queued"  # otomatik yayını kapalı


def test_interval_blocks_second_post():
    with db.connect() as conn:
        for name in ("a", "b"):
            item = db.add_item(conn, config.LIBRARY / name, name, {"title": name, "description": "", "tags": []})
            db.add_job(conn, item, "tiktok")
    enable("tiktok", interval_unit="gun")
    sched = scheduler.Scheduler()
    wait_jobs(sched, ("done",), 1)
    for _ in range(5):
        sched.tick()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0] == 1


def test_panel_upload_now_goes_to_all_selected_at_once(tmp_path):
    video = tmp_path / "v.mp4"
    make_video(video)
    client = app.test_client()
    response = client.post("/api/upload", data={
        "targets": ["youtube_shorts", "tiktok", "instagram_reels"],
        "when": "now",
        "files": (io.BytesIO(video.read_bytes()), "tatil_video.mp4"),
    }, content_type="multipart/form-data")
    assert response.status_code == 200
    sched = scheduler.Scheduler()
    wait_jobs(sched, ("done",), 3)
    assert set(statuses().values()) == {"done"}


def test_corrupt_image_fails_fast_with_error():
    client = app.test_client()
    response = client.post("/api/upload", data={
        "targets": ["tiktok"], "when": "queue",
        "files": (io.BytesIO(b"bu bir gorsel degil"), "bozuk.png"),
    }, content_type="multipart/form-data")
    assert response.status_code == 200
    assert statuses()["tiktok"] == "failed"


def test_panel_image_upload_makes_landscape_for_youtube_only(tmp_path):
    image = tmp_path / "kapak.png"
    result = media._run(["-y", "-f", "lavfi", "-i", "color=c=red:s=640x640:d=1", "-frames:v", "1", str(image)])
    assert result.returncode == 0, result.stderr
    client = app.test_client()
    response = client.post("/api/upload", data={
        "targets": ["youtube"], "when": "queue",
        "files": (io.BytesIO(image.read_bytes()), "kapak.png"),
    }, content_type="multipart/form-data")
    assert response.status_code == 200
    with db.connect() as conn:
        path = conn.execute("SELECT path FROM items").fetchone()["path"]
    info = media.probe(config.Path(path))
    assert (info.width, info.height) == (1920, 1080)
    assert info.duration == pytest.approx(config.IMAGE_SECONDS, abs=0.2)


def test_scheduled_time_waits():
    with db.connect() as conn:
        item = db.add_item(conn, config.LIBRARY / "x", "x", {"title": "x", "description": "", "tags": []})
        db.add_job(conn, item, "tiktok", immediate=True, run_at=time.time() + 3600)
    sched = scheduler.Scheduler()
    sched.tick()
    assert statuses()["tiktok"] == "queued"


@pytest.mark.parametrize("exception,job_status,paused", [
    (RateLimited("dolu", time.time() + 999), "queued", True),
    (AuthError("jeton"), "queued", True),
    (TransientError("ag"), "queued", False),
    (ValueError("beklenmedik"), "failed", False),
])
def test_failures_are_handled(exception, job_status, paused):
    with db.connect() as conn:
        item = db.add_item(conn, config.LIBRARY / "x", "x", {"title": "x", "description": "", "tags": []})
        db.add_job(conn, item, "youtube", immediate=True)
    fake = mock.Mock()
    fake.connected.return_value = True
    fake.publish.side_effect = exception
    sched = scheduler.Scheduler()
    with mock.patch.object(scheduler, "get_uploader", return_value=fake):
        sched.tick()
        sched._active["youtube"].join(5)
    assert statuses()["youtube"] == job_status
    with db.connect() as conn:
        rows = {r["target"]: r["paused_until"] for r in conn.execute("SELECT * FROM targets")}
    assert (rows["youtube"] > time.time()) is paused
    assert (rows["youtube_shorts"] > time.time()) is paused  # aynı kota


def test_interrupted_upload_is_not_retried_automatically():
    with db.connect() as conn:
        item = db.add_item(conn, config.LIBRARY / "x", "x", {"title": "x", "description": "", "tags": []})
        db.add_job(conn, item, "tiktok")
        conn.execute("UPDATE jobs SET status='uploading'")
    db.init()
    assert statuses()["tiktok"] == "failed"


def test_panel_rejects_foreign_origin_and_bad_settings():
    client = app.test_client()
    blocked = client.post("/api/autostart", json={"enabled": True}, headers={"Origin": "https://kotu.site"},
                          base_url="http://127.0.0.1:8765")
    assert blocked.status_code == 403
    outsider = client.get("/api/state", base_url="http://kotu.site")
    assert outsider.status_code == 403
    bad = client.post("/api/targets/tiktok", json={"interval_value": 0, "interval_unit": "sn", "daily_limit": 1},
                      base_url="http://127.0.0.1:8765")
    assert bad.status_code == 400


def fake_response(status=200, payload=None):
    response = mock.Mock(status_code=status, ok=status < 400, text="")
    response.json.return_value = payload or {}
    return response


def test_tiktok_inbox_flow_refreshes_token_and_uploads(tmp_path):
    from fabrika.uploaders.base import read_json, write_json

    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 1000)
    write_json(tiktok.CONFIG_FILE, {
        "client_key": "k", "client_secret": "s", "mode": "inbox",
        "access_token": "eski", "refresh_token": "r1", "expires_at": 0,
    })

    def post(url, **kwargs):
        if url.endswith("/oauth/token/"):
            return fake_response(payload={
                "access_token": "yeni", "refresh_token": "r2", "expires_in": 7200, "refresh_expires_in": 999,
            })
        if url.endswith("/inbox/video/init/"):
            assert kwargs["headers"]["Authorization"] == "Bearer yeni"
            assert "post_info" not in kwargs["json"]
            return fake_response(payload={"error": {"code": "ok"}, "data": {"publish_id": "p", "upload_url": "http://u"}})
        assert url.endswith("/status/fetch/")
        return fake_response(payload={"error": {"code": "ok"}, "data": {"status": "SEND_TO_USER_INBOX"}})

    with mock.patch.object(tiktok.requests, "post", side_effect=post), \
            mock.patch.object(tiktok.requests, "put", return_value=fake_response(201)) as put:
        tiktok.TikTokUploader().publish(Post(path=video, title="t"))
    assert put.call_args.kwargs["headers"]["Content-Range"] == "bytes 0-999/1000"
    assert read_json(tiktok.CONFIG_FILE)["refresh_token"] == "r2"
    tiktok.CONFIG_FILE.unlink()


def test_tiktok_daily_limit_pauses():
    from fabrika.uploaders.base import write_json

    write_json(tiktok.CONFIG_FILE, {"refresh_token": "r", "access_token": "a", "expires_at": time.time() + 9999})
    limit = fake_response(403, {"error": {"code": "spam_risk_too_many_pending_share", "message": ""}})
    with mock.patch.object(tiktok.requests, "post", return_value=limit), pytest.raises(RateLimited):
        tiktok.TikTokUploader()._call("/post/publish/inbox/video/init/", {})
    tiktok.CONFIG_FILE.unlink()


def test_instagram_flow_and_uncertain_publish_is_not_retried(tmp_path):
    from fabrika.uploaders import instagram
    from fabrika.uploaders.base import UploadError, write_json

    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 10)
    write_json(instagram.CONFIG_FILE, {"ig_user_id": "1", "access_token": "t"})

    def request(method, url, **kwargs):
        if url.endswith("/1/media"):
            assert kwargs["data"]["upload_type"] == "resumable"
            return fake_response(payload={"id": "c"})
        if "rupload" in url:
            assert kwargs["headers"]["file_size"] == "10"
            return fake_response(payload={"success": True})
        if url.endswith("/c"):
            return fake_response(payload={"status_code": "FINISHED"})
        if url.endswith("/media_publish"):
            return fake_response(payload={"id": "m"})
        return fake_response(payload={"permalink": "https://instagram.com/reel/x"})

    with mock.patch.object(instagram.requests, "request", side_effect=request), \
            mock.patch.object(instagram.time, "sleep"):
        url = instagram.InstagramUploader().publish(Post(path=video, title="t"))
        assert url == ("https://instagram.com/reel/x", "m")

    def flaky(method, url, **kwargs):
        if url.endswith("/media_publish"):
            raise instagram.requests.ConnectionError("koptu")
        return request(method, url, **kwargs)

    with mock.patch.object(instagram.requests, "request", side_effect=flaky), \
            mock.patch.object(instagram.time, "sleep"), pytest.raises(UploadError) as caught:
        instagram.InstagramUploader().publish(Post(path=video, title="t"))
    assert not isinstance(caught.value, TransientError)
    instagram.CONFIG_FILE.unlink()


def test_youtube_quota_error_pauses():
    from fabrika.uploaders import youtube

    error = mock.Mock(resp=mock.Mock(status=403), content=b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}')
    translated = youtube.translate_error(error)
    assert isinstance(translated, RateLimited)
    assert translated.retry_at > time.time()


def test_youtube_token_refresh_survives_flaky_connection(tmp_path):
    from google.auth.exceptions import TransportError as GoogleTransportError

    from fabrika.uploaders import youtube

    credentials = mock.Mock(valid=False)
    credentials.refresh.side_effect = [GoogleTransportError("INVALID_SESSION_ID")] * 3 + [None]
    credentials.to_json.return_value = "{}"
    with mock.patch.object(youtube, "TOKEN_FILE", tmp_path / "token.json"), \
            mock.patch.object(youtube.Credentials, "from_authorized_user_file", return_value=credentials), \
            mock.patch.object(youtube.time, "sleep"):
        (tmp_path / "token.json").write_text("{}")
        assert youtube.YouTubeUploader(shorts=True)._credentials() is credentials
    assert credentials.refresh.call_count == 4

    credentials.refresh.side_effect = GoogleTransportError("INVALID_SESSION_ID")
    with mock.patch.object(youtube, "TOKEN_FILE", tmp_path / "token.json"), \
            mock.patch.object(youtube.Credentials, "from_authorized_user_file", return_value=credentials), \
            mock.patch.object(youtube.time, "sleep"), pytest.raises(TransientError):
        youtube.YouTubeUploader(shorts=True)._credentials()


def test_network_error_is_retried_not_failed():
    with db.connect() as conn:
        item = db.add_item(conn, config.LIBRARY / "x", "x", {"title": "x", "description": "", "tags": []})
        db.add_job(conn, item, "youtube", immediate=True)
    fake = mock.Mock()
    fake.connected.return_value = True
    fake.publish.side_effect = ConnectionResetError("koptu")
    sched = scheduler.Scheduler()
    with mock.patch.object(scheduler, "get_uploader", return_value=fake):
        sched.tick()
        sched._active["youtube"].join(5)
    assert statuses()["youtube"] == "queued"
