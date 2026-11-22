"""Hikaye metninden Shorts videosu: senaryo → seslendirme → sahne görselleri → kurgu."""
import json
import random
from pathlib import Path

from .. import db
from ..clips import titles
from . import cartoon, images, puppet, render, script, voice

SCRIPT_FILE = "senaryo.json"
VOICE_BY_GENDER = {"kadin": "emel", "erkek": "ahmet"}


def process(job, workdir, stage, run_id):
    """Klip hattının iş akışı içinde çalışır; üretilen videoyu klip olarak kaydeder."""
    settings = json.loads(job["settings"])

    # Senaryo saklanır: yeniden üretimde aynı hikaye için Claude'a tekrar para ödenmez, yalnızca görseller yenilenir.
    cache = workdir / SCRIPT_FILE
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
    elif settings.get("kind") == "episode":
        from ..series import manager

        data = manager.episode_script(settings, stage)
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        stage("Senaryo yazılıyor")
        data = script.write(settings["story"])
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    db_title = data["title"][:titles.TITLE_LIMIT]
    with db.connect() as conn:
        conn.execute("UPDATE clip_jobs SET title=? WHERE id=?", (db_title, job["id"]))

    # Senaryo türü: yeni senaryolar düz 2D çizgi film (cast), önceki sürümün kayıtlı senaryoları kukla
    # (background + character) ya da tek görselli sahnelerle (image_prompt) aynen üretilmeye devam eder.
    if all("cast" in scene for scene in data["scenes"]):
        mode = "cizgi"
    elif all("background" in scene for scene in data["scenes"]):
        mode = "kukla"
    else:
        mode = "gorsel"
    cast = {puppet.slug(c["name"]): c for c in data.get("characters", [])}
    narrator = settings.get("voice", "kadin")
    items = []
    for index, scene in enumerate(data["scenes"], start=1):
        speaker = cast.get(puppet.slug(scene.get("speaker") or "-")) if mode != "gorsel" else None
        # Karakter konuşuyorsa kendi sesiyle (cinsiyet ve yaşına göre); yoksa anlatıcı sesiyle.
        if speaker and mode == "cizgi":
            voice_name = cartoon.voice_for(speaker)
        elif speaker:
            voice_name = VOICE_BY_GENDER.get(speaker.get("gender"), narrator)
        else:
            voice_name = narrator
        items.append((scene["narration"], voice_name, workdir / f"ses_{index:02}.mp3"))
    stage("Seslendiriliyor")
    spoken = voice.speak_all(items, workdir, lambda p: stage("Seslendiriliyor", p))
    scenes = [{"audio": path, "words": words} for (_, _, path), words in zip(items, spoken)]

    # Dizide aynı tohum bütün bölümlerde kullanılır; çizim tarzı ve karakterler daha tutarlı kalır.
    seed = settings.get("seed") or random.randrange(2 ** 31)
    style = settings.get("style", "cizgi")
    actors = {}
    try:
        if mode == "cizgi":
            stage("Mekânlar çiziliyor")
            style = "cizgi"
            prompts = [f"{scene['background']}, {cartoon.BACKGROUND_STYLE}" for scene in data["scenes"]]
        elif mode == "kukla":
            stage("Karakterler hazırlanıyor")
            # Dizinin karakterleri bir kez çizilip dizi klasöründe saklanır; her bölümde aynı çizim kullanılır.
            folder = Path(data["actor_dir"]) if data.get("actor_dir") else workdir / "oyuncular"
            used = {puppet.slug(scene.get("character") or "-") for scene in data["scenes"]}
            for order, (key, character) in enumerate(cast.items(), start=1):
                if key in used:
                    actors[key] = puppet.make_actor(character["name"], character["look"], style,
                                                    seed + 1000 * order, folder)
            stage("Mekânlar çiziliyor")
            prompts = [puppet.BACKGROUND_PROMPT.format(place=scene["background"]) for scene in data["scenes"]]
        else:
            stage("Görseller çiziliyor")
            prompts = [scene["image_prompt"] for scene in data["scenes"]]
        paths = images.generate(prompts, style, seed, workdir, lambda p: stage("Görseller çiziliyor", p))
    finally:
        images.release()
    for scene, path, source in zip(scenes, paths, data["scenes"]):
        scene["image"] = path
        if mode == "cizgi":
            members = []
            for member in source["cast"][:3]:
                character = cast.get(puppet.slug(member.get("name") or "-"))
                if character:
                    members.append({"name": character["name"], "look": cartoon.look_from(character),
                                    "position": member.get("position"), "emotion": member.get("emotion", "normal")})
            speaker = cast.get(puppet.slug(source.get("speaker") or "-"))
            scene["cartoon"] = {
                "cast": members,
                "shot": source.get("shot") if source.get("shot") in cartoon.FRAMING else "orta",
                "effects": [e for e in source.get("effects", []) if e in puppet.EFFECTS],
                "label": source.get("label", ""),
                "speaker": speaker["name"] if speaker and any(m["name"] == speaker["name"] for m in members) else "",
            }
        elif mode == "kukla":
            character = puppet.slug(source.get("character") or "-")
            scene["puppet"] = {
                "actor": actors.get(character),
                "shot": source.get("shot") if source.get("shot") in puppet.SHOTS else "orta",
                "position": source.get("position") if source.get("position") in puppet.POSITIONS else "orta",
                "effects": [e for e in source.get("effects", []) if e in puppet.EFFECTS],
                "label": source.get("label", ""),
                "speaking": character in actors and puppet.slug(source.get("speaker") or "-") == character,
            }

    stage("Video birleştiriliyor")
    output = workdir / f"hikaye_{run_id}.mp4"
    music = render.pick_music() if settings.get("music", True) else None
    render.build(scenes, data["hook"], data["moral"], music, output, workdir)

    tags = []
    for tag in ["shorts", *data["hashtags"]]:
        tag = tag.strip().lstrip("#").replace(" ", "").lower()
        if tag and tag not in tags:
            tags.append(tag)
    tags = tags[:20]
    description = titles.compose(data["description"], "", tags)
    with db.connect() as conn:
        cursor = conn.execute(
            """INSERT INTO clips (clip_job_id, path, title, start, end, score, mode, description, tags)
               VALUES (?, ?, ?, 0, ?, 0, 'hikaye', ?, ?)""",
            (job["id"], str(output), db_title, render.audio_duration(output), description,
             json.dumps(tags, ensure_ascii=False)),
        )
        clip = conn.execute("SELECT * FROM clips WHERE id=?", (cursor.lastrowid,)).fetchone()
    if settings.get("kind") == "episode":
        from ..series import manager

        manager.on_ready(settings["episode_id"])
    return clip, settings
