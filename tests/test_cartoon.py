import json
import time
from pathlib import Path
from unittest import mock

import numpy as np

from fabrika import db, media
from fabrika.clips import pipeline
from fabrika.clips.transcribe import Segment, Word
from fabrika.story import cartoon, render, voice

from test_story import SCRIPT, fake_images, fake_speak

ELIF = {"name": "Elif", "gender": "kadin", "age": "genc", "skin": "bugday", "headwear": "basortusu",
        "headwear_color": "#c0392b", "hair": "siyah", "hair_style": "uzun", "top": "#2f6b4f", "shirt": "#f2efe6",
        "beard": False, "glasses": False}
NINE = {**ELIF, "name": "Babaanne", "age": "yasli", "headwear_color": "#ecebe7", "top": "#5b4636"}
CARTOON_SCRIPT = {
    **SCRIPT,
    "characters": [ELIF, NINE],
    "scenes": [
        {"narration": "Köyde gece oldu.", "background": "village at night", "cast": [], "shot": "genis",
         "effects": ["sis"], "label": "KÖY", "speaker": ""},
        {"narration": "Kapıyı açma.", "background": "village room", "shot": "orta", "effects": ["isik"],
         "cast": [{"name": "BABAANNE", "position": "sol", "emotion": "kizgin"},
                  {"name": "Elif", "position": "sag", "emotion": "korku"}], "label": "", "speaker": "Babaanne"},
        {"narration": "Kapı çaldı.", "background": "dark door", "shot": "yakin", "effects": ["titreme", "yagmur"],
         "cast": [{"name": "Elif", "position": "orta", "emotion": "korku"}, {"name": "Yok", "position": "sol",
                                                                            "emotion": "normal"}],
         "label": "ELİF", "speaker": ""},
    ],
}


def test_cartoon_story_draws_cast_and_gives_speakers_their_voice(tmp_path):
    with db.connect() as conn:
        conn.execute("INSERT INTO clip_jobs (id, source, settings, created_at) VALUES (1, 'hikaye', ?, ?)",
                     (json.dumps({"kind": "story", "story": "x" * 100, "music": False}), time.time()))
    voices, scenes = [], []

    def speak(text, voice_name, path):
        voices.append(voice_name)
        return fake_speak(text, voice_name, path)

    real_render = cartoon.render_scene

    def spy(background, cast, *args, **kwargs):
        scenes.append((cast, kwargs))
        return real_render(background, cast, *args, **kwargs)

    with mock.patch("fabrika.story.script.write", return_value=CARTOON_SCRIPT), \
            mock.patch("fabrika.story.voice.clone_available", return_value=False), \
            mock.patch("fabrika.story.voice.speak", side_effect=speak), \
            mock.patch("fabrika.story.images.generate", side_effect=fake_images) as generate, \
            mock.patch("fabrika.story.cartoon.render_scene", side_effect=spy), \
            mock.patch("fabrika.story.images.release"):
        assert pipeline.ClipWorker().run_once()
    with db.connect() as conn:
        job = conn.execute("SELECT status, error FROM clip_jobs").fetchone()
        assert job["status"] == "done", job["error"]
        clip = conn.execute("SELECT * FROM clips").fetchone()
    # Ayrı ses ortamı yoksa Chatterbox sesleri Edge'e düşer: anlatıcı kadın, yaşlı kadın karakter de kadın sesi.
    assert voices == ["emel", "emel", "emel"]
    assert "flat vector" in generate.call_args[0][0][0]
    assert scenes[0][0] == []
    names = [m["name"] for m in scenes[1][0]]
    assert names == ["Babaanne", "Elif"] and scenes[1][1]["speaker"] == "Babaanne"
    assert [m["name"] for m in scenes[2][0]] == ["Elif"]  # listede olmayan karakter atlanır
    assert scenes[2][1]["effects"] == ["titreme", "yagmur"] and scenes[2][1]["label"] == "ELİF"
    info = media.probe(Path(clip["path"]))
    assert (info.width, info.height) == (1080, 1920)
    assert abs(info.duration - (3 * (2 + render.SCENE_PAUSE) + render.TAIL)) < 0.4


def test_look_and_voice_come_from_character_fields():
    look = cartoon.look_from({**ELIF, "skin": "mor", "top": "yesil", "hair_style": "afro"})
    assert look.skin == "bugday" and look.top == "#3e4a5c" and look.hair_style == "kisa"
    assert look.headwear == "basortusu" and look.headwear_color == "#c0392b"
    assert cartoon.voice_for(NINE) == "yasli_kadin"
    assert cartoon.voice_for({"gender": "erkek", "age": "orta"}) == "erkek"
    assert cartoon.voice_for({"gender": "erkek", "age": "cocuk"}) == "cocuk"


def test_layout_spreads_and_close_up_keeps_speaker():
    cast = [{"name": "A", "position": "orta"}, {"name": "B", "position": "orta"}]
    xs = [x for _, x in cartoon._layout(cast, "orta", "")]
    assert xs[0] < 0.5 < xs[1]
    assert [(m["name"], x) for m, x in cartoon._layout(cast, "yakin", "B")] == [("B", 0.5)]


def test_animator_blinks_and_talks():
    animator = cartoon.Animator("normal", seed=1)
    poses = [animator.pose(i / 30, 3 if 30 <= i < 60 else 0) for i in range(300)]
    assert any(p.blink > 0.9 for p in poses)
    assert all(p.mouth == 0 for p in poses[:29])
    assert any(p.mouth > 0 for p in poses[30:60])


def test_align_spreads_heard_times_over_written_words():
    heard = [Word(0.0, 0.5, "39"), Word(0.6, 1.0, "gece"), Word(1.1, 1.6, "kaldı.")]
    words = voice.align("Otuz dokuz gece kaldı.", heard, 2.0)
    assert [w.text for w in words] == ["Otuz", "dokuz", "gece", "kaldı."]
    assert words[0].start == 0.0 and abs(words[-1].end - 1.6) < 1e-6
    assert all(a.end <= b.start + 1e-6 for a, b in zip(words, words[1:]))
    assert voice.align("İki kelime", [], 1.0)[1].start == 0.5


def test_clone_voice_retries_garbled_sentence_and_lowers_old_voice(tmp_path):
    runs = []

    def run_clone(jobs, workdir):
        runs.append([job["text"] for job in jobs])
        for job in jobs:
            media._run(["-y", "-f", "lavfi", "-i", "sine=f=220:d=1", "-ar", "24000", job["out"]])

    calls = {"n": 0}

    def transcribe(samples, language=None, on_progress=None):
        calls["n"] += 1
        # İlk turda ikinci cümle bozuk duyulur; yeniden okununca düzelir.
        text = "bambaşka bir şey oldu" if calls["n"] == 2 else "kapıyı kimseye açma sakın"
        return [Segment(0, 1, text, [Word(0.1 * i, 0.1 * i + 0.1, t) for i, t in enumerate(text.split())])], "tr"

    with mock.patch("fabrika.story.voice.clone_available", return_value=True), \
            mock.patch("fabrika.story.voice._reference", return_value=Path("ornek.wav")), \
            mock.patch("fabrika.story.voice._run_clone", side_effect=run_clone), \
            mock.patch("fabrika.clips.transcribe.transcribe", side_effect=transcribe), \
            mock.patch("fabrika.clips.transcribe.release"), \
            mock.patch("fabrika.story.voice.speak") as edge:
        out = tmp_path / "ses_01.mp3"
        words = voice.speak_all([("Kapıyı kimseye açma sakın. Kapıyı kimseye açma sakın!", "yasli_kadin", out)],
                                tmp_path)[0]
    edge.assert_not_called()
    assert runs == [["Kapıyı kimseye açma sakın.", "Kapıyı kimseye açma sakın!"], ["Kapıyı kimseye açma sakın!"]]
    assert [w.text for w in words][:4] == ["Kapıyı", "kimseye", "açma", "sakın."] and len(words) == 8
    duration = render.audio_duration(out)
    assert abs(duration - (2 + voice.pause_after("."))) < 0.15  # perde kaydırma süreyi değiştirmez
    assert not list(tmp_path.glob("ses_01_*.wav"))
    assert np.isclose(voice.CLONES["yasli_kadin"][1], 0.86)


def test_voice_check_catches_wrong_word_but_not_digits():
    assert not voice._matches("Gülmüştüm.", "Görmüştüm.")
    assert voice._matches("Doksan sekiz kışında ablam doğurdu.", "98 kışında ablam doğurdu.")
    assert voice._matches("Saat üçü geçerken kapı tıklandı.", "Saat 3'ü geçerken, kapı tıklandı.")
    assert voice.number_words(1998) == "bindokuzyüzdoksansekiz" and voice.number_words(100) == "yüz"
