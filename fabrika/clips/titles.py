"""Her klibe içeriğine uygun başlık, açıklama ve etiket yazar.

Ücretsiz yöntem: klipte söylenen en vurucu söz, videonun oyunu ve yayıncısı, an türü (korku,
komik, genel) Shorts'ta yaygın başlık kalıplarına yerleştirilir. YouTube 60'tan fazla etiketi
yok saydığı için 15-20 etiket yazılır; başlık 100 karakteri geçmez.
"""
import re
import zlib

from .highlights import FILLER_STARTS, _is_hook, _tokens, split_sentences

TITLE_LIMIT = 100
TAG_COUNT = 18
QUOTE_WORDS = (3, 9)
QUOTE_CHARS = 45

GAMES = [
    (("gta 6", "gta vi", "gta6", "grand theft auto vi"), "GTA 6", ["gta6", "gtavi", "gta", "grandtheftauto", "rockstargames"]),
    (("gta 5", "gta v", "gta5", "gtav", "grand theft auto v"), "GTA 5", ["gta5", "gtav", "gta", "gtaonline"]),
    (("minecraft",), "Minecraft", ["minecraft", "minecraftturkiye", "minecraftshorts"]),
    (("valorant",), "Valorant", ["valorant", "valoranttr", "valorantclips"]),
    (("cs2", "counter-strike", "counter strike", "csgo"), "CS2", ["cs2", "counterstrike", "csgo"]),
    (("fortnite",), "Fortnite", ["fortnite", "fortniteturkiye"]),
    (("roblox",), "Roblox", ["roblox", "robloxturkiye"]),
    (("league of legends",), "League of Legends", ["leagueoflegends", "lol", "lolturkiye"]),
    (("pubg",), "PUBG", ["pubg", "pubgmobile"]),
    (("ea fc", "fc 25", "fc 26", "fifa"), "EA FC", ["eafc", "fifa", "futbol"]),
    (("resident evil",), "Resident Evil", ["residentevil", "survivalhorror"]),
    (("phasmophobia",), "Phasmophobia", ["phasmophobia", "hayaletavcıları"]),
    (("iblis",), "İblis", ["iblis", "türkkorkuoyunu"]),
]
KIND_TAGS = {
    "korku": ["korku", "korkuoyunu", "horror", "jumpscare", "horrorgaming"],
    "komik": ["komik", "komikanlar", "funny", "mizah", "kahkaha"],
    "genel": ["efsaneanlar", "eniyianlar", "anlar"],
}
COMMON_TAGS = [
    "keşfet", "keşfetbeniöneçıkar", "viral", "trend", "fyp", "oyun", "gaming", "gamer", "gameplay",
    "twitch", "yayın", "türkiye", "youtubeshorts", "oyunvideoları", "shortsvideo",
]
PROFANITY = {"siktir", "sik", "amk", "amına", "aq", "orospu", "piç", "yarrak", "göt", "fuck", "shit", "bitch"}

TEMPLATES = {
    "korku": {
        "quote": ['"{q}" 😱 | {game}', '{game} oynarken: "{q}" 😨', '"{q}" 😰 {game} korku anı'],
        "plain": ["{game} oynarken kalbimiz durdu 😱", "Bu jumpscare çok fenaydı 😨 | {game}",
                  "En korkunç an geldi 😰 | {game}"],
        "line": "{game} oynarken yaşanan en korkunç anlardan biri 😱",
    },
    "komik": {
        "quote": ['"{q}" 😂 | {game}', '{game} oynarken: "{q}" 🤣', '"{q}" 😂 {game} komik anlar'],
        "plain": ["{game} oynarken gülmekten öldük 😂", "Bu kadarını beklemiyorduk 🤣 | {game}",
                  "Kahkaha garantili an 😂 | {game}"],
        "line": "{game} oynarken yaşanan en komik anlardan biri 😂",
    },
    "genel": {
        "quote": ['"{q}" 🔥 | {game}', '{game} oynarken: "{q}" 😳', '"{q}" 👀 {game}'],
        "plain": ["{game} oynarken bunu kimse beklemiyordu 😳", "Efsane an 🔥 | {game}",
                  "Bu ana bakın 👀 | {game}"],
        "line": "{game} oynarken yaşanan efsane anlardan biri 🔥",
    },
}


def tr_lower(text):
    return text.replace("I", "ı").replace("İ", "i").lower()


def _slug(text):
    """Etiket yazımı: küçük harf, boşluk ve noktalama yok, Türkçe harfler korunur."""
    return re.sub(r"[^\wçğıöşü]", "", tr_lower(text))


def _sentence_case(text):
    text = tr_lower(text).strip()
    if not text:
        return text
    first = {"i": "İ", "ı": "I"}.get(text[0], text[0].upper())
    return first + text[1:]


def detect_game(source_title):
    """Videonun başlığından oyunu bulur; bilinen oyun yoksa başlığın en olası ad kısmını alır."""
    # Türkçe küçültmede "VI" → "vı" olur; ı/i farkı yok sayılmazsa GTA VI, GTA V sanılır.
    lowered = f" {tr_lower(source_title or '')} ".replace("ı", "i")
    for keys, name, tags in GAMES:
        if any(key in lowered for key in keys):
            return name, tags
    parts = [p.strip() for p in re.split(r"[|!:–—]| - ", source_title or "") if p.strip()]
    parts = [p for p in parts if not re.search(r"(?i)\b(bölüm|part|ep)\s*\d+", p) and 2 < len(p) <= 30]
    if not parts:
        return "", []
    # Tamamı büyük harfli parçalar genelde başlık sözüdür; karışık harfli kısa parça çoğunlukla oyunun adıdır.
    name = min(parts, key=lambda p: (p.isupper(), len(p)))
    return name, [_slug(name)] if _slug(name) else []


def _quote(sentences):
    """Klipteki en vurucu kısa söz: soru, ünlem ya da merak kelimesi içeren, küfürsüz bir cümle."""
    best, best_score = None, 0.0
    seen = set()
    for sentence in sentences:
        words = sentence.text.split()
        tokens = _tokens(sentence.text)
        key = sentence.text.casefold()
        if key in seen or not tokens or any(t in PROFANITY for t in tokens):
            continue
        seen.add(key)
        if not QUOTE_WORDS[0] <= len(words) <= QUOTE_WORDS[1] or len(sentence.text) > QUOTE_CHARS:
            continue
        score = 1.0 + sum(_is_hook(t) for t in tokens)
        score += 1.0 if sentence.text.rstrip().endswith(("!", "?")) else 0.0
        score -= 1.0 if tokens[0] in FILLER_STARTS else 0.0
        if score > best_score:
            best, best_score = sentence.text, score
    if not best:
        return ""
    return _sentence_case(best.rstrip(" .,…"))


def _pick(options, seed):
    return options[zlib.crc32(seed.encode()) % len(options)]


def _fit(title, extra):
    """Yer varsa sona yayıncı adı eklenir; başlık hiçbir zaman 100 karakteri geçmez."""
    if extra and len(title) + len(extra) + 3 <= TITLE_LIMIT:
        title = f"{title} | {extra}"
    return title[:TITLE_LIMIT].rstrip()


def make(segments, start, end, source_title, channel="", kind="genel"):
    """Klibin konuşmasından başlık, açıklama ve etiketler: (başlık, açıklama, etiket listesi)."""
    kind = kind if kind in TEMPLATES else "genel"
    templates = TEMPLATES[kind]
    game, game_tags = detect_game(source_title)
    game = game or "Oyun"
    words_in = [w for s in segments for w in s.words if w.start >= start and w.end <= end]
    sentences = split_sentences([type(segments[0])(start, end, "", words_in)]) if segments and words_in else []
    quote = _quote(sentences)
    seed = f"{source_title}-{start:.1f}"
    if quote:
        title = _pick(templates["quote"], seed).format(q=quote, game=game)
    else:
        title = _pick(templates["plain"], seed).format(game=game)
    title = _fit(title, channel)

    tags = ["shorts", *game_tags]
    if channel:
        tags.append(_slug(channel))
    tags += KIND_TAGS[kind] + COMMON_TAGS
    unique = []
    for tag in tags:
        if tag and tag not in unique:
            unique.append(tag)
    unique = unique[:TAG_COUNT]

    lines = []
    if quote:
        lines.append(f'"{quote}"')
    lines.append(templates["line"].format(game=game))
    lines.append("Beğenmeyi ve abone olmayı unutma! 🔔")
    return title, compose("\n".join(lines), channel, unique), unique


def compose(body, channel, tags):
    """Açıklamanın son hali: metin, yayıncıya kaynak satırı, en altta etiketler."""
    parts = [body.strip()]
    if channel:
        parts.append(f"🎥 Yayın: {channel}")
    return "\n".join(parts) + "\n\n" + " ".join(f"#{t}" for t in tags)
