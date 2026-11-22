"""Claude ile seçimli dizi yazar: önce dizi kitapçığı, sonra izleyicinin seçimine göre her bölüm."""
import json

import anthropic

from ..clips import ai_titles
from ..story import script

BIBLE_SYSTEM = """Türkçe Instagram Reels / YouTube Shorts için seçimli (interaktif) kurgu bir dizi tasarlıyorsun.
Her bölüm 50-70 saniyelik animasyonlu bir video olacak; bölüm sonunda izleyiciler iki seçenekten birini seçecek
ve sonraki bölüm kazanan seçimle devam edecek.

Kullanıcının verdiği konudan dizi kitapçığını yaz:
- title: Dizinin adı (kısa, akılda kalıcı, Türkçe).
- tagline: Tek cümlelik merak uyandıran tanıtım.
- characters: En fazla 4 ana karakter. Her karakter bütün bölümlerde birebir aynı çizilecek.
{character_rules}
- setting: Mekân ve atmosfer (Türkçe, 2-3 cümle).
- rules: Dünyanın 2-4 kuralı ya da gizemi (Türkçe). İzleyicinin merakını taşıyan şey bunlar.
- outline: İstenen bölüm sayısı kadar madde; her biri o bölümün kaba ana fikri. Seçimler ayrıntıları değiştirecek
  ama dizi bu omurgada ilerleyecek. Son bölüm net bir final olsun; hikaye tekrara düşmesin, her bölüm yeni bir
  bilgi ya da gerilim getirsin.
- finale: Finalde çözülecek ana gizem (Türkçe).
- hashtags: 8-10 diziye özgü etiket, # olmadan, küçük harf, boşluksuz.""".format(character_rules=script.CHARACTER_RULES)

EPISODE_SYSTEM = """Türkçe seçimli bir dizinin tek bölümünü yazıyorsun. Bölüm 50-70 saniyelik animasyonlu bir video olacak.

Kurallar:
- recap: Bölüm 2 ve sonrasında ilk cümle, izleyicinin seçimini hatırlatır: "Siz B dediniz: Kaan kapıyı açmadı."
  gibi. Bölüm 1'de boş bırak.
- scenes: 8-12 sahne. Her sahnede Türkçe anlatım (narration) ve sahnenin nasıl görüneceği.
  Bütün anlatımın toplamı 130-170 kelime. Recap varsa ilk sahnenin anlatımı onunla başlasın.
- narration bir seslendirme metnidir, düz yazı değil: kısa ve vurucu cümleler kur. Ses tonunu değiştirmek için
  cümlelerin başına şu işaretlerden birini koy: [sakin], [gergin], [fısıltı], [korku], [heyecan], [hüzün].
  İşaret bir sonraki işarete kadar geçerlidir. Gerilim anlarında üç nokta (...) ile duraksat, ani olayları ünlemle
  ver; noktalamayı seslendirme düşünerek kullan.
  Bölüm 2 ve sonrasında hikaye, izleyicinin seçtiği şıkla gerçekten devam etsin; seçilmeyen şık olmamış sayılır.
{scene_rules}
  cast ve speaker alanlarında karakterin kitapçıktaki adını aynen kullan.
- Son sahne bir gerilim anında bitsin ve anlatımı izleyiciye seçimi sorsun:
  "...Sen olsan ne yapardın? A: ..., B: ... Yorumlara yaz." gibi.
- question: Bölüm sonundaki seçim sorusu (Türkçe, kısa).
- option_a, option_b: İki seçenek, her biri en fazla 6 kelime, ikisi de hikayeyi farklı ama ilginç bir yöne götürsün.
- hook: Kapakta duracak 2-5 kelimelik, büyük harfli merak cümlesi.
- title: Bölüm başlığı, en fazla 70 karakter, "Bölüm N" ifadesini içermesin (sistem ekliyor).
- summary: Bu bölümde olanların 2-3 cümlelik özeti (sonraki bölümleri yazarken hafıza olarak kullanılacak).
- caption: Instagram/YouTube açıklaması: bir kanca satırı, bölümün kısa metni, boş satır, "🅰️ ..." ve "🅱️ ..."
  satırları, ardından "Yorumlara A mı B mi yaz, sonraki bölümü sen belirle 👇". Etiket yazma.
- hashtags: 8-12 etiket, # olmadan.
Final bölümünde seçim yoktur: question, option_a, option_b boş olsun; hikayeyi kitapçıktaki finale bağlayıp
kapat ve son sahnede izleyicilere teşekkür et.""".format(scene_rules=script.SCENE_RULES)

BIBLE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "tagline": {"type": "string"},
        "characters": {"type": "array", "items": script.CHARACTER_SCHEMA},
        "setting": {"type": "string"},
        "rules": {"type": "array", "items": {"type": "string"}},
        "outline": {"type": "array", "items": {"type": "string"}},
        "finale": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "tagline", "characters", "setting", "rules", "outline", "finale", "hashtags"],
    "additionalProperties": False,
}

EPISODE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "hook": {"type": "string"},
        "recap": {"type": "string"},
        "scenes": {"type": "array", "items": script.SCENE_SCHEMA},
        "question": {"type": "string"},
        "option_a": {"type": "string"},
        "option_b": {"type": "string"},
        "summary": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "hook", "recap", "scenes", "question", "option_a", "option_b", "summary", "caption",
                 "hashtags"],
    "additionalProperties": False,
}


def _ask(system, content, schema):
    if not ai_titles.is_configured():
        raise ValueError("Dizi için Claude anahtarı gerekli: Hesaplar → Claude bölümüne gir.")
    client = anthropic.Anthropic(api_key=ai_titles.api_key(), max_retries=ai_titles.MAX_RETRIES)
    response = client.beta.messages.create(
        model=ai_titles.MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise ValueError("Claude bu isteği yanıtlamadı; konuyu gözden geçir.")
    return json.loads(next(block.text for block in response.content if block.type == "text"))


def write_bible(premise, episodes):
    return _ask(BIBLE_SYSTEM, f"Bölüm sayısı: {episodes}\n\nKonu:\n{premise}", BIBLE_SCHEMA)


def write_episode(bible, history, number, total, choice):
    """history: önceki bölümler [{"number", "summary", "option_a", "option_b", "winner", "choice"}]."""
    lines = [f"Dizi kitapçığı:\n{json.dumps(bible, ensure_ascii=False, indent=1)}", ""]
    for past in history:
        picked = f"İzleyiciler {past['winner']} seçti: {past['choice']}" if past.get("winner") else ""
        lines.append(f"Bölüm {past['number']}: {past['summary']} Seçenekler: A) {past['option_a']} "
                     f"B) {past['option_b']}. {picked}")
    lines.append("")
    final = number >= total
    lines.append(f"Şimdi Bölüm {number} / {total} yazılacak.{' Bu FİNAL bölümü.' if final else ''}")
    if choice:
        lines.append(f"Kazanan seçim: {choice}")
    return _ask(EPISODE_SYSTEM, "\n".join(lines), EPISODE_SCHEMA)
