import io
import json
import time
from unittest import mock

import numpy as np

from fabrika import config, db, media
from fabrika.clips import highlights, pipeline, reframe, render, subtitles
from fabrika.clips.transcribe import Segment, Word
from fabrika.web import app

LOCAL = "http://127.0.0.1:8765"


def speech(sentences, start=0.0, pause=0.3, word_time=0.4):
    """Her cümleyi kelime zamanlı bir Whisper bölümüne çevirir."""
    segments, clock = [], start
    for sentence in sentences:
        words = []
        for token in sentence.split():
            words.append(Word(clock, clock + word_time - 0.05, token))
            clock += word_time
        segments.append(Segment(words[0].start, words[-1].end, sentence, words))
        clock += pause
    return segments


BORING = "Okay so today we check the settings menu and then we wait a little bit."
EXCITING = "Why does nobody tell you this secret? This one trick changed everything for me!"
EXCITING_2 = "Never make this mistake, it is the biggest truth nobody tells you about money!"


def test_highlights_prefer_hooks_over_filler():
    script = [BORING] * 6 + [EXCITING, EXCITING_2] + [BORING] * 6
    segments = speech(script)
    loudness = np.full(int(segments[-1].end / 0.5) + 2, -30.0)
    moments = highlights.find(segments, loudness, count=1, min_len=8, max_len=14)
    exciting_start = segments[6].start
    assert len(moments) == 1
    assert abs(moments[0].start - exciting_start) < 1
    assert moments[0].title.startswith("Why does nobody")


def test_highlights_do_not_overlap_and_respect_length():
    segments = speech([EXCITING, BORING] * 15)
    loudness = np.random.default_rng(1).normal(-30, 3, int(segments[-1].end / 0.5) + 2)
    moments = sorted(highlights.find(segments, loudness, count=4, min_len=10, max_len=20), key=lambda m: m.start)
    assert len(moments) == 4
    for moment in moments:
        assert 10 <= moment.end - moment.start <= 20 + highlights.START_PAD + highlights.END_PAD
    for first, second in zip(moments, moments[1:]):
        assert first.end <= second.start


def test_highlights_handles_turkish_suffixes():
    assert highlights._is_hook("sırrı")
    assert highlights._is_hook("inanılmazdı")
    assert not highlights._is_hook("masa")


def test_subtitles_highlight_one_word_at_a_time():
    words = speech(["bu çok önemli bir {sır}."], start=10)[0].words
    text = subtitles.build(words, offset=10)
    events = [line for line in text.splitlines() if line.startswith("Dialogue")]
    assert len(events) == len(words)
    assert events[0].startswith("Dialogue: 0,0:00:00.00,")
    assert all(line.count(subtitles.ACTIVE_COLOR) == 1 for line in events)
    assert "{sır}" not in text  # ASS komutu sanılmasın


def test_smooth_fills_gaps_and_ignores_jitter():
    centers = [(i * 0.25, None if i % 3 == 0 else 0.5 + (0.01 if i % 2 else -0.01)) for i in range(20)]
    smoothed = reframe.smooth(centers)
    assert len(smoothed) == 20
    assert all(abs(c - smoothed[0][1]) < 1e-9 for _, c in smoothed)


def test_track_render_produces_vertical_video(tmp_path):
    source = tmp_path / "yatay.mp4"
    media._run([
        "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=3",
        "-f", "lavfi", "-i", "sine=f=440:d=3", "-shortest", str(source),
    ])
    plan = reframe.Plan("track", 404, [(0.0, 0), (1.0, 400), (2.0, 876)])
    words = speech(["merhaba dünya"], start=0.5)[0].words
    output = tmp_path / "klip.mp4"
    render.render(source, output, 0.0, 3.0, plan, subtitles.build(words, 0.0), tmp_path)
    info = media.probe(output)
    assert (info.width, info.height) == (1080, 1920)
    assert abs(info.duration - 3.0) < 0.2


def test_vertical_source_is_fitted(tmp_path):
    source = tmp_path / "dikey.mp4"
    media._run(["-y", "-f", "lavfi", "-i", "testsrc2=s=720x1280:r=30:d=1", str(source)])
    assert reframe.plan(source, 0, 1).mode == "fit"


def test_pipeline_with_fake_transcript(tmp_path):
    source = tmp_path / "kaynak.mp4"
    media._run([
        "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=40",
        "-f", "lavfi", "-i", "sine=f=300:d=40", "-shortest", str(source),
    ])
    segments = speech([BORING] * 3 + [EXCITING, EXCITING_2] + [BORING] * 3)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO clip_jobs (source, settings, created_at) VALUES (?, ?, ?)",
            (str(source), json.dumps({"count": 1, "min_len": 8, "max_len": 14, "subtitles": True,
                                      "language": "", "auto_targets": ["tiktok"]}), time.time()),
        )
    with mock.patch.object(pipeline.transcribe, "transcribe", return_value=(segments, "en")):
        assert pipeline.ClipWorker().run_once()
    with db.connect() as conn:
        job = conn.execute("SELECT * FROM clip_jobs").fetchone()
        assert job["status"] == "done", job["error"]
        clip = conn.execute("SELECT * FROM clips").fetchone()
        assert clip["mode"] == "blur" and clip["sent"] == 1
        assert tuple(conn.execute("SELECT target, status FROM jobs").fetchone()) == ("tiktok", "queued")


def test_pipeline_reports_silent_video(tmp_path):
    source = tmp_path / "sessiz.mp4"
    media._run(["-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2", str(source)])
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO clip_jobs (source, settings, created_at) VALUES (?, ?, ?)",
            (str(source), json.dumps({"count": 1, "min_len": 5, "max_len": 10}), time.time()),
        )
    with mock.patch.object(pipeline.audio, "load", return_value=np.zeros(32000, dtype=np.float32)), \
            mock.patch.object(pipeline.transcribe, "transcribe", return_value=([], "en")):
        pipeline.ClipWorker().run_once()
    with db.connect() as conn:
        job = conn.execute("SELECT status, error FROM clip_jobs").fetchone()
    assert job["status"] == "failed" and "konuşma" in job["error"]


def test_clip_api_validates_and_queues():
    client = app.test_client()
    base = {"count": "3", "min_len": "20", "max_len": "60", "subtitles": "1"}
    assert client.post("/api/clips", data={**base, "url": "ftp://x"}, base_url=LOCAL).status_code == 400
    assert client.post("/api/clips", data={**base, "min_len": "70", "url": "https://youtu.be/x"},
                       base_url=LOCAL).status_code == 400
    ok = client.post("/api/clips", data={**base, "url": "https://youtu.be/x", "auto_targets": ["tiktok", "kotu"]},
                     base_url=LOCAL)
    assert ok.status_code == 200
    state = client.get("/api/state", base_url=LOCAL).get_json()
    assert state["clip_jobs"][0]["settings"]["auto_targets"] == ["tiktok"]
    upload = client.post("/api/clips", data={**base, "file": (io.BytesIO(b"x"), "video.mp4")},
                         content_type="multipart/form-data", base_url=LOCAL)
    assert upload.status_code == 200
    assert len(list((config.CLIPS_DIR / "gelen").iterdir())) >= 1


def test_send_clip_copies_into_queue(tmp_path):
    clip_file = tmp_path / "klip_01.mp4"
    media._run(["-y", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=4", str(clip_file)])
    with db.connect() as conn:
        conn.execute("INSERT INTO clip_jobs (id, source, settings, created_at) VALUES (1, 'x', '{}', 0)")
        conn.execute(
            "INSERT INTO clips (id, clip_job_id, path, title, start, end, score) VALUES (1, 1, ?, 'eski', 0, 4, 1)",
            (str(clip_file),),
        )
    client = app.test_client()
    response = client.post("/api/clips/1/send", json={"targets": ["youtube_shorts", "instagram_reels"],
                                                       "when": "now", "title": "Yeni başlık"}, base_url=LOCAL)
    assert response.status_code == 200
    assert clip_file.exists()
    with db.connect() as conn:
        assert conn.execute("SELECT title FROM items").fetchone()[0] == "Yeni başlık"
        assert {r[0] for r in conn.execute("SELECT target FROM jobs WHERE immediate=1")} == {
            "youtube_shorts", "instagram_reels"}
    assert client.get("/klip/1.mp4", base_url=LOCAL).status_code == 200


FACECAM = {"x": 0.0, "y": 0.05, "w": 0.24, "h": 0.24}


def _camera_at_drawn_box():
    return mock.patch.object(reframe, "locate_camera", side_effect=lambda path, s, e, box, w, h: tuple(box))


def test_split_layout_renders_camera_on_top(tmp_path):
    source = tmp_path / "oyun.mp4"
    media._run([
        "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=2",
        "-f", "lavfi", "-i", "sine=f=440:d=2", "-shortest", str(source),
    ])
    with _camera_at_drawn_box():
        plan = reframe.plan(source, 0, 2, "split", list(FACECAM.values()))
    assert plan.mode == "split"
    camera, top = render.split_layout(plan)
    assert top == render.CAMERA_SIZES["orta"] and camera[2] == 0
    margin = render.subtitle_margin(plan)
    output = tmp_path / "klip.mp4"
    words = speech(["merhaba"], start=0.2)[0].words
    render.render(source, output, 0, 2, plan, subtitles.build(words, 0, margin), tmp_path)
    info = media.probe(output)
    assert (info.width, info.height) == (1080, 1920)
    assert f",{margin},1" in (tmp_path / "altyazi.ass").read_text(encoding="utf-8")


def test_split_fit_keeps_whole_game_visible(tmp_path):
    source = tmp_path / "oyun.mp4"
    media._run(["-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=1", str(source)])
    with _camera_at_drawn_box():
        plan = reframe.plan(source, 0, 1, "split_fit", [0.0, 0.0, 0.185, 0.365])
    assert plan.mode == "split" and plan.game_fit
    game_w, game_h, camera_y, game_y = render.fit_layout(plan)
    _, top = render.split_layout(plan)
    (gx, gy, gw, gh), hide_needed = render.game_area(plan)
    assert not hide_needed and gx > 0 and gw < 1920  # sol kenardaki kamera şeridi atıldı
    assert game_w == 1080 and game_y == camera_y + top and game_y + game_h <= 1920
    assert render.subtitle_margin(plan) >= render.MIN_SUBTITLE_MARGIN
    render.render(source, tmp_path / "klip.mp4", 0, 1, plan, None, tmp_path)
    info = media.probe(tmp_path / "klip.mp4")
    assert (info.width, info.height) == (1080, 1920)


def _split_job(source, ask, default_box=None):
    with db.connect() as conn:
        db.set_setting(conn, "facecam_ask", ask)
        db.set_setting(conn, "facecam", default_box)
        conn.execute(
            "INSERT INTO clip_jobs (id, source, settings, created_at) VALUES (1, ?, ?, ?)",
            (str(source), json.dumps({"count": 1, "min_len": 8, "max_len": 14, "subtitles": False,
                                      "layout": "split"}), time.time()),
        )


def _talking_video(tmp_path):
    source = tmp_path / "oyun.mp4"
    media._run([
        "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=40",
        "-f", "lavfi", "-i", "sine=f=300:d=40", "-shortest", str(source),
    ])
    return source, speech([BORING] * 3 + [EXCITING, EXCITING_2] + [BORING] * 3)


def test_split_job_waits_for_camera_box_then_continues(tmp_path):
    source, segments = _talking_video(tmp_path)
    _split_job(source, ask=True, default_box=list(FACECAM.values()))
    client = app.test_client()
    with mock.patch.object(pipeline.transcribe, "transcribe", return_value=(segments, "en")), _camera_at_drawn_box():
        pipeline.ClipWorker().run_once()
        with db.connect() as conn:
            assert conn.execute("SELECT status FROM clip_jobs").fetchone()[0] == "waiting_box"
        assert client.get("/api/clip-jobs/1/kare.jpg?at=0.5", base_url=LOCAL).status_code == 200
        assert client.post("/api/clip-jobs/1/facecam", json={"x": 0.9, "y": 0, "w": 0.01, "h": 0.1},
                           base_url=LOCAL).status_code == 400
        box = {"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.35}
        assert client.post("/api/clip-jobs/1/facecam", json=box, base_url=LOCAL).status_code == 200
        pipeline.ClipWorker().run_once()
    with db.connect() as conn:
        job = conn.execute("SELECT status, settings FROM clip_jobs").fetchone()
        assert job["status"] == "done"
        assert json.loads(job["settings"])["facecam"] == list(box.values())
        assert db.get_setting(conn, "facecam") == list(box.values())  # sonraki videolar için varsayılan
        assert conn.execute("SELECT mode FROM clips").fetchone()[0] == "split"


def test_split_job_uses_last_box_without_asking(tmp_path):
    source, segments = _talking_video(tmp_path)
    _split_job(source, ask=False, default_box=list(FACECAM.values()))
    with mock.patch.object(pipeline.transcribe, "transcribe", return_value=(segments, "en")):
        pipeline.ClipWorker().run_once()
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM clip_jobs").fetchone()[0] == "done"


def test_rerun_can_switch_layout():
    client = app.test_client()
    with db.connect() as conn:
        conn.execute("INSERT INTO clip_jobs (id, source, status, settings, created_at) VALUES (1, 'x', 'done', '{}', 0)")
    assert client.post("/api/clip-jobs/1/rerun", json={"layout": "split_fit"}, base_url=LOCAL).status_code == 200
    with db.connect() as conn:
        job = conn.execute("SELECT status, settings FROM clip_jobs").fetchone()
    assert job["status"] == "queued" and json.loads(job["settings"])["layout"] == "split_fit"
    assert client.post("/api/settings", json={"facecam_ask": False}, base_url=LOCAL).status_code == 200
    assert client.get("/api/state", base_url=LOCAL).get_json()["facecam_ask"] is False


def test_scare_curve_peaks_where_everything_jumps():
    from fabrika.clips import events, vision

    duration = 120
    loudness = np.full(int(duration / 0.5), -35.0)
    loudness[160:164] = -8  # 80. saniyede çığlık
    camera = np.full(duration * 4, 1.0)
    camera[320:324] = 25  # irkilme
    game = np.full(duration * 4, 2.0)
    game[319:322] = 60  # ekran birden değişiyor
    segments = speech(["siktir korktum anne"], start=80.5)
    score = events.curve("korku", duration, loudness, 0.5, vision.Motion(0.25, camera, game), segments)
    peak = int(np.argmax(score)) * events.STEP
    assert 79 <= peak <= 82
    moments = events.find("korku", score, segments, duration, count=1, min_len=20, max_len=40)
    assert moments[0].start < 80 < moments[0].end
    assert moments[0].start >= 80 - events.KINDS["korku"]["before"] - events.SNAP_SECONDS


def test_scan_measures_camera_and_game_separately(tmp_path):
    from fabrika.clips import vision

    source = tmp_path / "v.mp4"
    media._run(["-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2", str(source)])
    motion = vision.scan(source, 2, [(0.0, 0.0, 0.25, 0.25)])
    assert len(motion.camera) == len(motion.game) >= 7
    assert motion.game.max() > 0


def test_manual_ranges_and_keyword():
    from fabrika.clips import events

    assert events.parse_ranges("12:30-13:10, 1:02:05-1:02:40") == [(750, 790), (3725, 3760)]
    for bad in ("5:00-4:00", "1:00-5:00"):
        try:
            events.parse_ranges(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    segments = speech([BORING, "of course I was really scared there", BORING, "I got scared again"], start=30)
    found = events.keyword_moments(segments, "scared", 200, 5, 20, 40)
    assert len(found) == 1  # ikinci geçiş ilkine çok yakın


def test_clip_api_validates_modes():
    client = app.test_client()
    base = {"count": "3", "min_len": "20", "max_len": "60", "url": "https://youtu.be/x"}
    assert client.post("/api/clips", data={**base, "mode": "elle", "manual": "ne zaman"}, base_url=LOCAL).status_code == 400
    assert client.post("/api/clips", data={**base, "mode": "kelime"}, base_url=LOCAL).status_code == 400
    assert client.post("/api/clips", data={**base, "mode": "korku", "camera_size": "kucuk"},
                       base_url=LOCAL).status_code == 200
    job = client.get("/api/state", base_url=LOCAL).get_json()["clip_jobs"][0]
    assert job["settings"]["mode"] == "korku" and job["settings"]["camera_size"] == "kucuk"


def test_camera_candidates_mirror_corners():
    candidates = reframe.camera_candidates((0.73, 0.13, 0.27, 0.29))
    assert candidates[0] == (0.73, 0.13, 0.27, 0.29)
    assert any(abs(x - 0.0) < 1e-9 and abs(y - 0.13) < 1e-9 for x, y, _, _ in candidates)  # sol karşılığı
    assert len(reframe.camera_candidates((0.4, 0.4, 0.2, 0.2))) == 1  # ortadaki kutunun aynası kendisi


def _overlay_frames(x, y, w, h, count=8, width=960, height=540):
    """Oyun her karede başka parlaklıkta, kamera kutusu sabit: gerçek yayın görüntüsü gibi."""
    rng = np.random.default_rng(3)
    frames = np.empty((count, height, width), np.uint8)
    for index in range(count):
        frames[index] = rng.integers(0, 255)
        frames[index, int(y * height): int((y + h) * height), int(x * width): int((x + w) * width)] = 60
    return frames


def test_locate_camera_finds_moved_box_by_its_border():
    drawn = (0.73, 0.13, 0.27, 0.29)
    frames = _overlay_frames(0.0, 0.24, 0.27, 0.29)  # kamera sola ve daha aşağı taşınmış
    with mock.patch.object(reframe, "_clip_frames", return_value=frames),             mock.patch.object(reframe, "_face_points", return_value=[(0.12, 0.38)]):
        x, y, w, h = reframe.locate_camera("x", 0, 10, drawn, 1920, 1080)
    assert x < 0.03 and abs(y - 0.24) < 0.02 and abs(w - 0.25) < 0.02


def test_locate_camera_trusts_strong_border_without_faces():
    frames = _overlay_frames(0.72, 0.13, 0.27, 0.29)  # kameradakiler başını çevirmiş, yüz bulunamıyor
    with mock.patch.object(reframe, "_clip_frames", return_value=frames),             mock.patch.object(reframe, "_face_points", return_value=[]):
        x, y, _, _ = reframe.locate_camera("x", 0, 10, (0.72, 0.13, 0.27, 0.29), 1920, 1080)
    assert abs(x - 0.73) < 0.02 and abs(y - 0.14) < 0.02


def test_locate_camera_reports_missing_camera():
    frames = np.random.default_rng(5).integers(0, 255, (8, 540, 960), dtype=np.uint8)  # kamera kutusu yok
    with mock.patch.object(reframe, "_clip_frames", return_value=frames),             mock.patch.object(reframe, "_face_points", return_value=[(0.5, 0.5)]):
        assert reframe.locate_camera("x", 0, 10, (0.73, 0.13, 0.27, 0.29), 1920, 1080) is None


def test_game_area_drops_edge_strip_or_hides_floating_camera():
    right = reframe.Plan("split", box=(0.73, 0.13, 0.27, 0.29), width=1920, height=1080)
    (gx, gy, gw, gh), hide = render.game_area(right)
    assert not hide and gx == 0 and gw < 0.73 * 1920 and gh == 1080
    floating = reframe.Plan("split", box=(0.4, 0.4, 0.2, 0.2), width=1920, height=1080)
    assert render.game_area(floating) == ((0, 0, 1920, 1080), True)


def test_rerun_removes_only_unsent_auto_queued_clips(tmp_path):
    clip_file = tmp_path / "klip_1_01.mp4"
    media._run(["-y", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=4", str(clip_file)])
    with db.connect() as conn:
        conn.execute("INSERT INTO clip_jobs (id, source, settings, created_at) VALUES (1, 'x', '{}', 0)")
        for clip_id in (1, 2):
            conn.execute(
                "INSERT INTO clips (id, clip_job_id, path, title, start, end, score) VALUES (?, 1, ?, 't', 0, 4, 1)",
                (clip_id, str(clip_file)),
            )
            clip = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
            pipeline.send_clip(conn, clip, ["youtube_shorts"])
        conn.execute("UPDATE jobs SET status='done' WHERE id=1")  # ilki yüklenmiş
    pipeline._reset_outputs(1, tmp_path)
    with db.connect() as conn:
        assert [tuple(r) for r in conn.execute("SELECT id, status FROM jobs")] == [(1, "done")]


def test_old_database_gets_clip_link(tmp_path):
    with db.connect() as conn:
        conn.executescript("DROP TABLE jobs; DROP TABLE items;")
        conn.execute("""CREATE TABLE items (id INTEGER PRIMARY KEY, path TEXT NOT NULL, source_name TEXT NOT NULL,
                        title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
                        created_at REAL NOT NULL)""")
        conn.execute("INSERT INTO clip_jobs (id, source, settings, created_at) VALUES (7, 'x', '{}', 0)")
        conn.execute("INSERT INTO clips (clip_job_id, path, title, start, end, score) VALUES (7, 'C:\\k\\7\\klip_9_01.mp4', 't', 0, 1, 1)")
        conn.execute("INSERT INTO items (path, source_name, title, created_at) VALUES ('a', 'klip_9_01.mp4', 't', 0)")
    db.init()
    with db.connect() as conn:
        assert conn.execute("SELECT clip_job_id FROM items").fetchone()[0] == 7


def test_titles_use_quote_game_and_channel():
    from fabrika.clips import titles

    segments = speech(["Okay so we start.", "Neden kimse bunu söylemedi?", "siktir bu ne be!", BORING])
    title, description, tags = titles.make(segments, 0, 60, "YILLARCA BU ANI BEKLEDİK | GRAND THEFT AUTO VI | BÖLÜM 1",
                                           "Elraenn", "komik")
    assert "Neden kimse bunu söylemedi?" in title and "GTA 6" in title and len(title) <= 100
    assert "siktir" not in title.casefold()
    assert 15 <= len(tags) <= 20 and len(set(tags)) == len(tags)
    assert {"shorts", "gta6", "elraenn", "komik"} <= set(tags)
    assert description.count("#") == len(tags) and "Elraenn" in description


def test_titles_without_good_quote_and_unknown_game():
    from fabrika.clips import titles

    title, _, tags = titles.make(speech(["a"]), 0, 5, "GERÇEK HİKAYEDEN ESİNLENEN TÜRK KORKU OYUNU ! İblis3: Phenomena",
                                 "", "korku")
    assert "İblis" in title and "korku" in tags
    assert titles.detect_game("KORKUNÇ DAĞDA ÇOBANLIK YAPMAK ! Scary Game 2 The Mad Shepherd")[0] == "Scary Game 2 The Mad Shepherd"


def test_caption_does_not_repeat_hashtags():
    from fabrika.uploaders.base import Post

    post = Post(path=None, title="Başlık", description="Metin\n\n#shorts #gta6", tags=["shorts", "gta6", "yeni"])
    assert post.caption.count("#shorts") == 1 and "#yeni" in post.caption


def test_claude_titles_used_and_fallback_on_error():
    import anthropic

    from fabrika.clips import ai_titles

    segments = speech(["Neden kimse bunu söylemedi?", BORING])
    settings = {"channel": "Elraenn", "mode": "komik"}
    written = {0: ("GTA 6'da efsane an 😂", "Bu sahneye bakın!", ["shorts", "gta6", "komik"])}
    with mock.patch.object(ai_titles, "is_configured", return_value=True), \
            mock.patch.object(ai_titles, "write", return_value=written) as write:
        (title, description, tags), = pipeline._texts(settings, segments, [(0, 12)], "GTA VI")
    assert title == "GTA 6'da efsane an 😂" and tags == ["shorts", "gta6", "komik"]
    assert "🎥 Yayın: Elraenn" in description and description.endswith("#shorts #gta6 #komik")
    assert "Neden kimse" in write.call_args.args[0][0]["text"]

    error = anthropic.APIConnectionError(request=mock.Mock())
    with mock.patch.object(ai_titles, "is_configured", return_value=True), \
            mock.patch.object(ai_titles, "write", side_effect=error):
        (title, _, tags), = pipeline._texts(settings, segments, [(0, 12)], "GTA VI")
    assert "GTA 6" in title and len(tags) >= 15  # şablon yöntemine döndü


def test_claude_request_shape_and_tag_cleanup():
    from fabrika.clips import ai_titles

    reply = json.dumps({"clips": [{"id": 0, "title": "Başlık 😱", "description": "Açıklama",
                                   "hashtags": ["#GTA6", "keşfet", "shorts", "Viral Anlar"]}]})
    response = mock.Mock(stop_reason="end_turn", content=[mock.Mock(type="text", text=reply)])
    client = mock.Mock()
    client.beta.messages.create.return_value = response
    with mock.patch.object(ai_titles, "api_key", return_value="sk-ant-test"), \
            mock.patch.object(ai_titles.anthropic, "Anthropic", return_value=client):
        result = ai_titles.write([{"id": 0, "text": "aaa", "seconds": 20}], "GTA VI", "Elraenn", "korku")
    assert result[0] == ("Başlık 😱", "Açıklama", ["shorts", "gta6", "keşfet", "viralanlar"])
    request = client.beta.messages.create.call_args.kwargs
    assert request["model"] == "claude-opus-5-5" and request["fallbacks"] == "default"
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_claude_key_endpoint_validates():
    import anthropic

    from fabrika.clips import ai_titles

    client = app.test_client()
    assert client.post("/api/accounts/claude", json={"api_key": "abc"}, base_url=LOCAL).status_code == 400
    with mock.patch.object(anthropic, "Anthropic") as factory:
        factory.return_value.models.retrieve.return_value = mock.Mock()
        assert client.post("/api/accounts/claude", json={"api_key": "sk-ant-deneme"}, base_url=LOCAL).status_code == 200
    assert ai_titles.is_configured()
    assert client.get("/api/state", base_url=LOCAL).get_json()["accounts"]["claude"]["connected"] is True
    client.post("/api/accounts/claude", json={"api_key": ""}, base_url=LOCAL)
    assert not ai_titles.is_configured()


def test_download_errors_are_readable_turkish():
    from fabrika.clips import download

    raw = "ERROR: \x1b[0;31m[youtube] abc: Sign in to confirm you’re not a bot. Use --cookies"
    assert "bot sandı" in download.friendly_error(raw) and "\x1b" not in download.friendly_error(raw)
    assert "kaldırılmış" in download.friendly_error("ERROR: [youtube] x: This video is unavailable")
    assert download.friendly_error("\x1b[0;31mbaşka hata") == "başka hata"


def test_youtube_cookie_upload_and_use():
    from fabrika.clips import download

    client = app.test_client()
    bad = client.post("/api/accounts/youtube/cookies", data={"file": (io.BytesIO(b"merhaba"), "c.txt")},
                      content_type="multipart/form-data", base_url=LOCAL)
    assert bad.status_code == 400
    cookie = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
    ok = client.post("/api/accounts/youtube/cookies", data={"file": (io.BytesIO(cookie), "c.txt")},
                     content_type="multipart/form-data", base_url=LOCAL)
    assert ok.status_code == 200 and download._options()["cookiefile"] == str(download.COOKIE_FILE)
    assert client.get("/api/state", base_url=LOCAL).get_json()["accounts"]["youtube"]["cookies"] is True
    client.post("/api/accounts/youtube/cookies", data={}, content_type="multipart/form-data", base_url=LOCAL)
    assert "cookiefile" not in download._options()


def test_download_retries_dropped_connections_only():
    import yt_dlp

    from fabrika.clips import download

    calls = {"n": 0}

    class Flaky:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download, process):
            calls["n"] += 1
            if calls["n"] < 3:
                raise yt_dlp.utils.DownloadError("Unable to download API page: [SSL: INVALID_SESSION_ID]")
            return {"channel": "Elraenn"}

    with mock.patch.object(download.yt_dlp, "YoutubeDL", Flaky), mock.patch.object(download.time, "sleep"):
        assert download.channel_name("https://youtu.be/x") == "Elraenn"
    assert calls["n"] == 3

    class Blocked(Flaky):
        def extract_info(self, url, download, process):
            calls["n"] += 1
            raise yt_dlp.utils.DownloadError("Sign in to confirm you're not a bot")

    calls["n"] = 0
    with mock.patch.object(download.yt_dlp, "YoutubeDL", Blocked), mock.patch.object(download.time, "sleep"):
        assert download.channel_name("https://youtu.be/x") == ""
    assert calls["n"] == 1  # bot engeli yeniden denenmez
