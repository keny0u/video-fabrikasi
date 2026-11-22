"""Claude ile klip başlığı, açıklaması ve etiketleri yazar.

Bir videonun bütün klipleri tek istekte gönderilir: Claude her klibin konuşmasını okur,
Whisper'ın yanlış duyduğu sözleri anlamdan düzeltir ve Shorts'a uygun metin yazar.
Anahtar yoksa ya da istek başarısız olursa çağıran taraf ücretsiz yönteme döner.
"""
import json

import anthropic

from .. import config

KEY_FILE = config.SECRETS / "anthropic.json"
MODEL = "claude-opus-5-5"
MAX_RETRIES = 6  # trafiği değiştiren programlar bağlantı kopartabiliyor
TITLE_LIMIT = 100
TAG_RANGE = (15, 20)

SYSTEM = """Türkçe YouTube Shorts, TikTok ve Instagram Reels için oyun yayını kliplerine metin yazıyorsun.

Her klip için şunları yaz:
- title: İzleyiciyi durduran, merak uyandıran Türkçe başlık. Oyunun adı geçsin. En fazla 70 karakter hedefle,
  asla 100'ü geçme. Bir ya da iki uygun emoji kullan. Tamamı büyük harf yazma. Başlıkta küfür kullanma.
  Klipte söylenen gerçekten vurucu bir söz varsa onu tırnak içinde kullanabilirsin.
- description: 2-3 kısa satır: klipte ne olduğunu merak uyandıracak şekilde anlat, sonra izleyiciyi
  yorum yapmaya ya da abone olmaya çağır. Etiket yazma; etiketleri ayrı alanda ver.
- hashtags: 15-20 etiket, # işareti olmadan, küçük harf, boşluksuz. Oyuna, yayıncıya, anın türüne ve
  Türkiye'de keşfete düşmeye uygun olsun (ör. keşfet, viral, oyun). "shorts" etiketini ekleme, sistem ekliyor.

Konuşma otomatik yazıya döküldüğü için bazı kelimeler yanlış duyulmuş olabilir; anlamdan doğrusunu çıkar,
anlamsız sözleri başlıkta kullanma. Kliplerde oyun karakterlerinin diyalogları da olabilir; başlığı
yayıncının tepkisine ve klipteki olaya göre kur. Her klibin başlığı birbirinden farklı olsun."""

SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "title", "description", "hashtags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

KINDS = {"korku": "korku anları (jumpscare, irkilme)", "komik": "komik anlar", "genel": "öne çıkan anlar"}


def api_key():
    if KEY_FILE.exists():
        return json.loads(KEY_FILE.read_text(encoding="utf-8")).get("api_key", "")
    return ""


def is_configured():
    return bool(api_key())


def _request(clips, source_title, channel, kind):
    lines = [f"Video başlığı: {source_title}", f"Yayıncı: {channel or 'bilinmiyor'}",
             f"Bu kliplerin türü: {KINDS.get(kind, KINDS['genel'])}", ""]
    for clip in clips:
        lines.append(f"Klip {clip['id']} ({clip['seconds']:.0f} sn) konuşması:\n{clip['text'] or '(konuşma yok)'}\n")
    return "\n".join(lines)


def write(clips, source_title, channel="", kind="genel"):
    """clips: [{"id", "text", "seconds"}] -> {id: (başlık, açıklama gövdesi, etiketler)}.

    Hata durumunda anthropic istisnaları yukarı fırlatılır; çağıran ücretsiz yönteme döner.
    """
    client = anthropic.Anthropic(api_key=api_key(), max_retries=MAX_RETRIES)
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": _request(clips, source_title, channel, kind)}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise ValueError("Claude bu isteği yanıtlamadı.")
    text = next(block.text for block in response.content if block.type == "text")
    results = {}
    for item in json.loads(text)["clips"]:
        tags = []
        for tag in ["shorts", *item["hashtags"]]:
            tag = tag.strip().lstrip("#").replace(" ", "").lower()
            if tag and tag not in tags:
                tags.append(tag)
        results[item["id"]] = (item["title"].strip()[:TITLE_LIMIT], item["description"].strip(), tags[: TAG_RANGE[1]])
    return results
