import json
from pathlib import Path
import time
from unittest import mock

from fabrika import db, media
from fabrika.clips import pipeline
from fabrika.clips.transcribe import Word
from fabrika.story import render
from fabrika.web import app

LOCAL = "http://127.0.0.1:8765"
SCRIPT = {
    "title": "Eğri minare 😮", "hook": "BU MİNARE EĞRİ!", "moral": "Yanlış söz düzeltilmezse büyür.",
    "characters": [{"name": "Sinan", "look": "old man"}],
    "scenes": [{"narration": "Birinci sahne.", "image_prompt": "a"}, {"narration": "İkinci sahne.", "image_prompt": "b"},
               {"narration": "Üçüncü sahne.", "image_prompt": "c"}],
    "description": "Sen olsan ne yapardın?", "hashtags": ["hikaye", "#Hayat Dersi"],
}


def fake_speak(text, voice, path):
    media._run(["-y", "-f", "lavfi", "-i", "sine=f=300:d=2", str(path)])
    return [Word(0.1, 0.6, text.split()[0]), Word(0.7, 1.5, text.split()[1])]


def fake_images(prompts, style, seed, outdir, on_progress=None):
    paths = []
    for index, _ in enumerate(prompts):
        path = outdir / f"sahne_{index + 1:02}.png"
        media._run(["-y", "-f", "lavfi", "-i", f"color=c=0x{index * 40 + 50:02x}6080:s=768x1344:d=1",
                    "-frames:v", "1", str(path)])
        paths.append(path)
    return paths


def test_story_job_produces_vertical_short_with_texts(tmp_path):
    with db.connect() as conn:
        conn.execute("INSERT INTO clip_jobs (id, source, settings, created_at) VALUES (1, 'hikaye', ?, ?)",
                     (json.dumps({"kind": "story", "story": "x" * 100, "music": False,
                                  "auto_targets": ["youtube_shorts"]}), time.time()))
    with mock.patch("fabrika.story.script.write", return_value=SCRIPT), \
            mock.patch("fabrika.story.voice.speak", side_effect=fake_speak), \
            mock.patch("fabrika.story.images.generate", side_effect=fake_images), \
            mock.patch("fabrika.story.images.release"):
        assert pipeline.ClipWorker().run_once()
    with db.connect() as conn:
        job = conn.execute("SELECT status, error, title FROM clip_jobs").fetchone()
        assert job["status"] == "done", job["error"]
        assert job["title"] == "Eğri minare 😮"
        clip = conn.execute("SELECT * FROM clips").fetchone()
        assert clip["sent"] == 1 and clip["mode"] == "hikaye"
        assert json.loads(clip["tags"])[:3] == ["shorts", "hikaye", "hayatdersi"]
        assert conn.execute("SELECT target FROM jobs").fetchone()[0] == "youtube_shorts"
    info = media.probe(Path(clip["path"]))
    assert (info.width, info.height) == (1080, 1920)
    expected = 3 * (2 + render.SCENE_PAUSE) + render.TAIL
    assert abs(info.duration - expected) < 0.4
    ass = (pipeline.workdir_for(1) / "hikaye.ass").read_text(encoding="utf-8")
    assert "Style: Kart" in ass and "BU MİNARE EĞRİ!" in ass and "Yanlış söz" in ass


def test_story_needs_claude_and_sane_length():
    client = app.test_client()
    assert client.post("/api/stories", json={"story": "kısa"}, base_url=LOCAL).status_code == 400
    with mock.patch("fabrika.clips.ai_titles.is_configured", return_value=False):
        response = client.post("/api/stories", json={"story": "x" * 200}, base_url=LOCAL)
    assert response.status_code == 400 and "Claude" in response.get_json()["error"]
    with mock.patch("fabrika.clips.ai_titles.is_configured", return_value=True):
        ok = client.post("/api/stories", json={"story": "x" * 200, "voice": "emel", "style": "kotu"}, base_url=LOCAL)
    assert ok.status_code == 200
    job = client.get("/api/state", base_url=LOCAL).get_json()["clip_jobs"][0]
    assert job["settings"]["kind"] == "story" and job["settings"]["voice"] == "emel" and job["settings"]["style"] == "cizgi"


def test_voice_splits_sentences_with_moods_and_pauses():
    from fabrika.story import voice

    parts = voice.sentences("[sakin] Gece oldu. Kapı çaldı... [fısıltı] Elif, aç! Kim o?")
    assert parts == [("sakin", "Gece oldu."), ("sakin", "Kapı çaldı..."), ("fısıltı", "Elif, aç!"),
                     ("fısıltı", "Kim o?")]
    assert voice.pause_after("Kapı çaldı...") > voice.pause_after("Gece oldu.")
    question = voice.delivery_for("normal", "Kim o?")
    assert question.pitch > voice.MOODS["normal"].pitch
    whisper = voice.delivery_for("fısıltı", "Elif, aç!")
    assert whisper.volume == voice.MOODS["fısıltı"].volume  # fısıltı ünlemde bağırmaya dönmez
    assert voice.sentences("[bilinmeyen] Merhaba.") == [("normal", "[bilinmeyen] Merhaba.")]


PUPPET_SCRIPT = {
    **SCRIPT,
    "characters": [{"name": "Elif", "look": "young woman, red headscarf", "gender": "kadin"}],
    "scenes": [
        {"narration": "Gece yağmur yağıyordu.", "background": "village house at night", "character": "",
         "shot": "genis", "position": "orta", "effects": ["yagmur"], "label": "OTUZ DOKUZ GECE KALDI", "speaker": ""},
        {"narration": "Abla uyuma.", "background": "village bedroom", "character": "ELİF", "shot": "orta",
         "position": "sag", "effects": ["isik", "bilinmeyen"], "label": "ELİF", "speaker": "Elif"},
        {"narration": "Kapı tıklandı.", "background": "dark door", "character": "Elif", "shot": "yakin",
         "position": "orta", "effects": ["titreme", "karanlik", "sis"], "label": "", "speaker": ""},
    ],
}


def fake_actor(name, look, style, seed, folder):
    """Yüzü ve dudağı belli, arka planı saydam basit bir karakter çizimi."""
    from PIL import Image, ImageDraw

    folder.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (400, 800), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle([60, 300, 340, 800], fill=(40, 90, 60, 255))
    draw.ellipse([100, 60, 300, 300], fill=(240, 205, 185, 255))
    draw.ellipse([170, 225, 230, 255], fill=(200, 60, 60, 255))
    draw.line([170, 240, 230, 240], fill=(80, 20, 20, 255), width=2)
    path = folder / "elif.png"
    image.save(path)
    path.with_suffix(".json").write_text(json.dumps(
        {"name": name, "face": [100, 60, 200, 240], "lips": {"left": 170, "right": 230, "seam": 240, "height": 15}}),
        encoding="utf-8")
    return path


def test_puppet_story_animates_cast_over_backgrounds(tmp_path):
    with db.connect() as conn:
        conn.execute("INSERT INTO clip_jobs (id, source, settings, created_at) VALUES (1, 'hikaye', ?, ?)",
                     (json.dumps({"kind": "story", "story": "x" * 100, "music": False, "voice": "ahmet"}),
                      time.time()))
    voices = []

    def speak(text, voice, path):
        voices.append(voice)
        return fake_speak(text + " x", voice, path)

    with mock.patch("fabrika.story.script.write", return_value=PUPPET_SCRIPT), \
            mock.patch("fabrika.story.voice.speak", side_effect=speak), \
            mock.patch("fabrika.story.images.generate", side_effect=fake_images) as generate, \
            mock.patch("fabrika.story.puppet.make_actor", side_effect=fake_actor) as make_actor, \
            mock.patch("fabrika.story.images.release"):
        assert pipeline.ClipWorker().run_once()
    with db.connect() as conn:
        job = conn.execute("SELECT status, error FROM clip_jobs").fetchone()
        assert job["status"] == "done", job["error"]
        clip = conn.execute("SELECT * FROM clips").fetchone()
    # Anlatıcı sahneleri dizinin sesiyle, Elif'in kendi sözleri kadın sesiyle okunur.
    assert voices == ["ahmet", "emel", "ahmet"]
    assert make_actor.call_count == 1
    prompts = generate.call_args[0][0]
    assert "no people" in prompts[0] and prompts[1].startswith("village bedroom")
    info = media.probe(Path(clip["path"]))
    assert (info.width, info.height) == (1080, 1920)
    assert abs(info.duration - (3 * (2 + render.SCENE_PAUSE) + render.TAIL)) < 0.4


def test_puppet_mouth_and_lip_detection(tmp_path):
    import numpy as np
    from PIL import Image

    from fabrika.story import puppet

    sprite_path = fake_actor("Elif", "", "", 0, tmp_path)
    sprite, info = puppet.load_actor(sprite_path)
    frames = puppet.mouth_frames(sprite, info["lips"])
    assert len(frames) == len(puppet.MOUTH_LEVELS) and frames[0] is sprite
    # Ağız açıldıkça dudak çizgisinin altı daha çok değişir; alnı hiç değişmez.
    changes = [int(np.abs(f.astype(int) - sprite).sum()) for f in frames]
    assert changes[0] == 0 < changes[1] < changes[3]
    assert all((f[:200] == sprite[:200]).all() for f in frames)
    assert puppet.mouth_frames(sprite, None) == [sprite] * len(puppet.MOUTH_LEVELS)

    flat = puppet._over(sprite, (200, 200, 200))
    lips = puppet._find_lips(flat, info["face"])
    assert lips and abs(lips["seam"] - 240) <= 2 and lips["left"] <= 172 and lips["right"] >= 228

    audio = tmp_path / "ses.wav"
    media._run(["-y", "-f", "lavfi", "-i", "sine=f=300:d=1", "-f", "lavfi", "-i", "anullsrc=d=1",
                "-filter_complex", "[0][1]concat=n=2:v=0:a=1", str(audio)])
    track = puppet.mouth_track(audio, 60)
    assert len(track) == 60 and track[:25].min() > 0 and track[35:].max() == 0
    assert puppet.slug("ELİF'in Gözü") == "elif_in_gozu"
    Image.fromarray(puppet._label("elif")).save(tmp_path / "etiket.png")
