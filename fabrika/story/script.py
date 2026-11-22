"""Hikayeyi Claude ile Shorts senaryosuna çevirir: kanca, sahneler, ders cümlesi, görsel tarifleri."""
import json

import anthropic

from ..clips import ai_titles

# Sahneler düz 2D çizgi film olarak çizilir (cartoon.py): karakterler kodla çizilen kuklalardır, mekân ayrı çizilir.
SCENE_RULES = """- Video düz 2D çizgi filmdir: karakterler her bölümde birebir aynı çizilen kuklalardır; göz kırpar, kaşlarıyla
  ifade verir, konuşurken ağızları oynar. Mekân ayrıca çizilir.
- Hikaye baştan sona anlaşılır olsun: ilk sahne yeri, zamanı ve durumu tanıtır; sahneler birbirinin devamıdır,
  izleyici hiçbir sahnede "bu ne, nereden çıktı" dememeli. Her sahne 1-3 cümle; sahneler kısa ve tempolu.
- background: İngilizce mekân tarifi, İNSANSIZ (karakterler ayrıca konacak). Sade tut: "simple", az eşya, geniş
  boş alanlar. Yer, zaman ve ışık; gece sahnelerinde "at night" yaz. Hikaye Türkiye'de geçiyorsa belirt
  ("simple anatolian village room", "simple istanbul apartment kitchen" gibi). Görselde yazı olmasın.
- cast: Sahnede görünen karakterler (en fazla 3): name (characters listesindeki ad), position ("sol", "orta",
  "sag") ve emotion ("normal", "korku", "uzgun", "kizgin", "supheli", "mutlu"). Mekânı gösteren sahnelerde boş.
  Karşılıklı konuşmada iki karakteri aynı sahneye koy (biri sol, biri sag).
- shot: "genis" (karakterler küçük, mekân görünür), "orta" (belden yukarı) ya da "yakin" (yalnız konuşanın
  yüzü). Gerilim anlarında yakın çekim kullan; çekimleri çeşitlendir.
- effects: Sahneye uyan efektler, boş olabilir: "yagmur", "sis", "isik" (lamba titremesi), "titreme" (korku
  anında sarsıntı), "karanlik" (karanlık sahne). Abartma.
- label: Ekranın sol üstünde duracak kısa, büyük harfli etiket ya da boş: kimin gözünden bakıldığı
  ("ELİF'İN GÖZÜ"), konuşanın adı ("BABAANNE"), yer ya da saat ("03:12") ya da sayaç ("KIRK GECE KALDI").
- speaker: Anlatım tamamen bir karakterin kendi sözleriyse o karakterin adı (cast içinde olmalı, ağzı oynar ve
  kendi sesiyle konuşur); anlatıcı konuşuyorsa "". speaker doluysa narration yalnızca o karakterin sözleri olsun
  ("dedi" gibi anlatıcı sözleri ve tırnak olmadan). Anlatıcı kadındır."""

SCENE_SCHEMA = {
    "type": "object",
    "properties": {
        "narration": {"type": "string"},
        "background": {"type": "string"},
        "cast": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "position": {"type": "string", "enum": ["sol", "orta", "sag"]},
                "emotion": {"type": "string", "enum": ["normal", "korku", "uzgun", "kizgin", "supheli", "mutlu"]},
            },
            "required": ["name", "position", "emotion"],
            "additionalProperties": False,
        }},
        "shot": {"type": "string", "enum": ["genis", "orta", "yakin"]},
        "effects": {"type": "array",
                    "items": {"type": "string", "enum": ["yagmur", "sis", "isik", "titreme", "karanlik"]}},
        "label": {"type": "string"},
        "speaker": {"type": "string"},
    },
    "required": ["narration", "background", "cast", "shot", "effects", "label", "speaker"],
    "additionalProperties": False,
}

CHARACTER_RULES = """  Her karakterin görünüşü şu alanlardan oluşur; tek bakışta ayırt edilsinler diye renkleri farklı seç:
  gender ("kadin"/"erkek", sesi buna göre seçilir), age ("cocuk", "genc", "orta", "yasli"), skin ("acik",
  "bugday", "esmer", "koyu"), headwear ("basortusu", "yok", "kasket"), headwear_color (#rrggbb), hair ("siyah",
  "kahve", "sari", "kizil", "gri", "beyaz"), hair_style ("uzun", "kisa", "topuz", "kel"), top (ceket/hırka rengi,
  #rrggbb), shirt (gömlek rengi, #rrggbb), beard, glasses."""

CHARACTER_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "gender": {"type": "string", "enum": ["kadin", "erkek"]},
        "age": {"type": "string", "enum": ["cocuk", "genc", "orta", "yasli"]},
        "skin": {"type": "string", "enum": ["acik", "bugday", "esmer", "koyu"]},
        "headwear": {"type": "string", "enum": ["basortusu", "yok", "kasket"]},
        "headwear_color": {"type": "string"},
        "hair": {"type": "string", "enum": ["siyah", "kahve", "sari", "kizil", "gri", "beyaz"]},
        "hair_style": {"type": "string", "enum": ["uzun", "kisa", "topuz", "kel"]},
        "top": {"type": "string"},
        "shirt": {"type": "string"},
        "beard": {"type": "boolean"},
        "glasses": {"type": "boolean"},
    },
    "required": ["name", "gender", "age", "skin", "headwear", "headwear_color", "hair", "hair_style", "top",
                 "shirt", "beard", "glasses"],
    "additionalProperties": False,
}

SYSTEM = """Türkçe YouTube Shorts için kısa, animasyonlu hikaye videoları hazırlıyorsun.
Kullanıcının verdiği hikayeyi 45-55 saniyelik bir anlatıma dönüştür.

Kurallar:
- narration: Türkçe anlatım. Bütün sahnelerin toplamı 110-140 kelime olsun. Sade, akıcı, merak uyandıran bir dil kullan;
  hikayedeki olayları ve gerçekleri değiştirme, uydurma ekleme. İlk cümle izleyiciyi hemen yakalasın.
- scenes: 7-12 sahne. Her sahne anlatım ve o anın nasıl görüneceğini içerir.
- narration bir seslendirme metnidir, düz yazı değil: kısa ve vurucu cümleler kur. Ses tonunu değiştirmek için
  cümlelerin başına şu işaretlerden birini koy: [sakin], [gergin], [fısıltı], [korku], [heyecan], [hüzün].
  İşaret bir sonraki işarete kadar geçerlidir. Gerilim anlarında üç nokta (...) ile duraksat, ani olayları ünlemle
  ver; noktalamayı seslendirme düşünerek kullan.
{scene_rules}
- characters: Hikayedeki ana karakterler.
{character_rules}
- hook: Videonun başında ekranda duracak 2-5 kelimelik, büyük harfli, merak uyandıran Türkçe kart yazısı.
- moral: Videonun sonunda ekranda duracak en fazla 12 kelimelik Türkçe ders ya da sonuç cümlesi.
- title: Shorts başlığı, en fazla 70 karakter, bir emoji kullanabilirsin.
- description: 3-4 kısa satır: kanca cümle, izleyiciye soru, paylaşma çağrısı. Etiket yazma.
- hashtags: 15-20 etiket, # işareti olmadan, küçük harf, boşluksuz (ör. hikaye, gerçekhikaye, hayatdersi, keşfet).
Gerçek kişilerin adları geçiyorsa hikayeyi anlatırken onları küçük düşürecek ya da iftira sayılabilecek ifadeler kullanma.""".format(scene_rules=SCENE_RULES, character_rules=CHARACTER_RULES)

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "hook": {"type": "string"},
        "moral": {"type": "string"},
        "characters": {"type": "array", "items": CHARACTER_SCHEMA},
        "scenes": {"type": "array", "items": SCENE_SCHEMA},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "hook", "moral", "characters", "scenes", "description", "hashtags"],
    "additionalProperties": False,
}


def write(story):
    """Hikaye metninden senaryo sözlüğü üretir; Claude anahtarı yoksa ValueError."""
    if not ai_titles.is_configured():
        raise ValueError("Hikaye videoları için Claude anahtarı gerekli: Hesaplar → Claude bölümüne gir.")
    client = anthropic.Anthropic(api_key=ai_titles.api_key(), max_retries=ai_titles.MAX_RETRIES)
    response = client.beta.messages.create(
        model=ai_titles.MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Hikaye:\n{story}"}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise ValueError("Claude bu hikayeyi işlemedi; metni gözden geçir.")
    data = json.loads(next(block.text for block in response.content if block.type == "text"))
    if not data["scenes"]:
        raise ValueError("Senaryoda sahne yok.")
    return data
