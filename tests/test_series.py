import json
import time
from unittest import mock

from fabrika import db
from fabrika.clips import pipeline
from fabrika.series import manager, votes
from fabrika.web import app

from test_story import fake_images, fake_speak

LOCAL = "http://127.0.0.1:8765"
BIBLE = {"title": "Kule", "tagline": "t", "characters": [{"name": "Kaan", "look": "young man, blue hoodie"}],
         "setting": "s", "rules": ["r"], "outline": ["1", "2", "3"], "finale": "f", "hashtags": ["kule"]}


def episode(number):
    return {"title": f"Kapı {number}", "hook": "SEN GÖREMEZSİN", "recap": "" if number == 1 else "Siz A dediniz.",
            "scenes": [{"narration": "Kaan durdu.", "image_prompt": "a"}, {"narration": "Kapı açıldı.", "image_prompt": "b"}],
            "question": "Ne yapmalı?", "option_a": "Kapıyı aç", "option_b": "Kaç", "summary": f"özet {number}",
            "caption": "Kanca\n\n🅰️ Kapıyı aç\n🅱️ Kaç", "hashtags": ["korku"]}


def test_parse_vote_rules():
    assert votes.parse_vote("A") == "A"
    assert votes.parse_vote("bence b olsun") == "B"
    assert votes.parse_vote("🅱️🅱️") == "B"
    assert votes.parse_vote("A dedik ya nasıl B") is None  # iki şık birden: sayılmaz
    assert votes.parse_vote("abi final yap") is None  # kelimenin içindeki a/b oy değil
    assert votes.parse_vote("Şefe güvenmiyorum") is None


def test_tally_counts_each_person_once():
    comments = [("ali", "A"), ("ali", "yok ya B"), ("ayşe", "A"), ("veli", "harika bölüm")]
    assert votes.tally(comments) == {"A": 1, "B": 1}


def run_episode():
    with mock.patch("fabrika.story.voice.speak", side_effect=fake_speak), \
            mock.patch("fabrika.story.images.generate", side_effect=fake_images), \
            mock.patch("fabrika.story.images.release"):
        assert pipeline.ClipWorker().run_once()


def test_series_cycle_from_episode_to_vote_to_next_episode():
    write_episode = mock.Mock(side_effect=lambda bible, history, number, total, choice: episode(number))
    with mock.patch("fabrika.series.writer.write_bible", return_value=BIBLE), \
            mock.patch("fabrika.series.writer.write_episode", write_episode):
        series_id = manager.create("Kaan kuleye atanır ve kuralların sırrını çözmeye çalışır.",
                                   {"episodes": 3, "vote_hours": 20, "auto_targets": ["youtube_shorts"]})
        run_episode()
    with db.connect() as conn:
        job = conn.execute("SELECT status, error, title FROM clip_jobs").fetchone()
        assert job["status"] == "done", job["error"]
        assert job["title"] == "Kule · Bölüm 1: Kapı 1"
        ep = conn.execute("SELECT * FROM episodes").fetchone()
        assert ep["status"] == "ready" and ep["option_a"] == "Kapıyı aç"
        queued = conn.execute("SELECT id, immediate FROM jobs").fetchone()
        assert queued["immediate"] == 1  # bölüm hazır olur olmaz yayınlanır
        ass = (pipeline.workdir_for(1) / "hikaye.ass").read_text(encoding="utf-8")
        assert "BÖLÜM 1\\NSEN GÖREMEZSİN" in ass and "A) Kapıyı aç\\NB) Kaç" in ass
        conn.execute("UPDATE jobs SET status='done', remote_url='https://youtu.be/abcdefghijk', finished_at=?",
                     (time.time(),))

    manager.tick()
    with db.connect() as conn:
        ep = conn.execute("SELECT * FROM episodes").fetchone()
        assert ep["status"] == "voting" and ep["deadline"] > time.time() + 19 * 3600

    comments = [("yt:1", "B"), ("yt:2", "b olsun"), ("yt:3", "A"), ("yt:4", "a mı b mi")]
    with mock.patch.object(votes, "youtube_comments", return_value=comments), \
            mock.patch("fabrika.series.writer.write_bible", return_value=BIBLE), \
            mock.patch("fabrika.series.writer.write_episode", write_episode):
        manager.tick(now=time.time() + 21 * 3600)  # süre doldu
        with db.connect() as conn:
            first = conn.execute("SELECT * FROM episodes WHERE number=1").fetchone()
            assert (first["votes_a"], first["votes_b"], first["winner"]) == (1, 2, "B")
            assert conn.execute("SELECT choice FROM episodes WHERE number=2").fetchone()[0] == "B: Kaç"
        run_episode()
    history, choice = write_episode.call_args.args[1], write_episode.call_args.args[4]
    assert choice == "B: Kaç" and history[0]["summary"] == "özet 1" and history[0]["winner"] == "B"


def test_final_episode_has_no_vote_and_ends_series():
    with mock.patch("fabrika.series.writer.write_bible", return_value=BIBLE), \
            mock.patch("fabrika.series.writer.write_episode", side_effect=lambda *a: episode(a[2])):
        manager.create("Kaan kuleye atanır ve kuralların sırrını çözmeye çalışır.",
                       {"episodes": 1, "vote_hours": 20, "auto_targets": ["youtube_shorts"]})
        run_episode()
    with db.connect() as conn:
        assert conn.execute("SELECT option_a FROM episodes").fetchone()[0] == ""
        conn.execute("UPDATE jobs SET status='done', finished_at=?", (time.time(),))
    manager.tick()
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM series").fetchone()[0] == "finished"
        assert conn.execute("SELECT status FROM episodes").fetchone()[0] == "decided"


def test_series_api_validates_and_manual_votes():
    client = app.test_client()
    with mock.patch("fabrika.clips.ai_titles.is_configured", return_value=True):
        assert client.post("/api/series", json={"premise": "kısa"}, base_url=LOCAL).status_code == 400
        assert client.post("/api/series", json={"premise": "x" * 50, "episodes": 40}, base_url=LOCAL).status_code == 400
        ok = client.post("/api/series", json={"premise": "x" * 50, "episodes": 5, "vote_hours": 6}, base_url=LOCAL)
    assert ok.status_code == 200
    state = client.get("/api/state", base_url=LOCAL).get_json()
    assert state["series"][0]["settings"]["episodes"] == 5 and state["series"][0]["episodes"][0]["number"] == 1
    assert state["clip_jobs"][0]["settings"]["kind"] == "episode"
    episode_id = state["series"][0]["episodes"][0]["id"]
    with db.connect() as conn:
        conn.execute("UPDATE episodes SET status='voting', option_a='x', option_b='y'")
    client.post(f"/api/episodes/{episode_id}/votes", json={"a": 7, "b": 2}, base_url=LOCAL)
    with mock.patch.object(manager, "refresh_votes"), mock.patch.object(manager, "start_episode") as start:
        assert client.post(f"/api/episodes/{episode_id}/decide", base_url=LOCAL).status_code == 200
    assert start.call_args.args[3] == "A: x"
