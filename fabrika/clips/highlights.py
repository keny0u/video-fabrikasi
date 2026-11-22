"""Yapay zekâ API'si olmadan önemli anları bulur.

Her aday bölüm; konuşma hızı, ses yüksekliği, merak uyandıran kelimeler, soru ile
başlama ve sessizlik gibi ölçülerle puanlanır. Ölçüler bütün adaylar arasında
standartlaştırılır, böylece sessiz bir podcast ile bağıran bir vlog aynı ayarla çalışır.
"""
import re
from dataclasses import dataclass

import numpy as np

from .transcribe import Segment

SENTENCE_END = re.compile(r"[.!?…]$")
LONG_GAP = 1.0
START_PAD = 0.15
END_PAD = 0.4
MIN_SEPARATION = 2.0
TITLE_LIMIT = 80

HOOK_WORDS = {
    # Türkçe; ekleri yakalamak için kök olarak eşleşir (sırrı, nasıl, inanılmazdı...)
    "neden", "niye", "nasıl", "sır", "sırr", "asla", "kimse", "inanılmaz", "şok", "aslında",
    "gerçek", "hata", "dikkat", "bakın", "bekle", "dur", "önemli", "işte", "para", "milyon",
    "bedava", "kesinlikle", "tehlike", "yasak", "itiraf", "hayatım", "değişt", "kimsenin",
    # İngilizce
    "why", "how", "secret", "never", "nobody", "shocking", "trick", "biggest", "mistake",
    "truth", "actually", "wait", "insane", "crazy", "million", "money", "free", "best", "worst",
    "stop", "changed", "everything", "important", "warning",
}
FILLER_STARTS = {
    "tamam", "evet", "şey", "yani", "şimdi", "hani", "ee", "ıı", "okay", "ok", "so", "alright",
    "um", "uh", "yes", "well", "anyway",
}
# Oyun ve film videolarında arka plandaki başka dilde konuşmalar ayıklanır.
LANGUAGE_WORDS = {
    "tr": {
        "bir", "bu", "ne", "ve", "da", "de", "mi", "mı", "mu", "çok", "var", "yok", "ben", "sen", "biz",
        "şu", "için", "ama", "gibi", "oldu", "olur", "hadi", "abi", "değil", "evet", "hayır", "neden",
        "nasıl", "şimdi", "burada", "şey", "yani", "bak", "işte", "kadar", "daha", "her", "bunu", "onu",
    },
    "en": {
        "the", "you", "is", "are", "what", "come", "on", "man", "yeah", "really", "okay", "it", "that",
        "this", "do", "don't", "get", "go", "i'm", "we", "they", "and", "to", "of", "a", "in", "with",
        "your", "my", "me", "no", "yes", "fuck", "shit", "hey", "right", "know", "just", "can", "was",
    },
}
FOREIGN_LIMIT = 0.3
TURKISH_LETTERS = set("çğışöü")

WEIGHTS = {
    "rate": 0.8,
    "loudness": 1.0,
    "dynamics": 0.5,
    "keywords": 1.2,
    "hook": 1.0,
    "silence": -1.0,
    "filler": -0.6,
    "length": 0.4,
    "repeat": -0.8,
}


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    words: list


@dataclass
class Highlight:
    start: float
    end: float
    score: float
    title: str
    text: str


def _tokens(text):
    return [t for t in re.findall(r"[\wçğıöşüâîû]+", text.casefold().replace("i̇", "i"))]


def _is_hook(token):
    if token in HOOK_WORDS:
        return True
    return any(len(word) >= 4 and token.startswith(word) for word in HOOK_WORDS)


def split_sentences(segments):
    """Whisper bölümleri cümle ortasında kesebilir; kelimelerden gerçek cümleleri kurar."""
    sentences, current = [], []
    for word in (w for segment in segments for w in segment.words):
        if current and word.start - current[-1].end > LONG_GAP:
            sentences.append(current)
            current = []
        current.append(word)
        if SENTENCE_END.search(word.text):
            sentences.append(current)
            current = []
    if current:
        sentences.append(current)
    return [Sentence(s[0].start, s[-1].end, " ".join(w.text for w in s), s) for s in sentences]


def sentence_language(text):
    """Cümlenin dilini sık kelimelerden tahmin eder; emin değilse None."""
    tokens = _tokens(text)
    scores = {lang: sum(t in words for t in tokens) for lang, words in LANGUAGE_WORDS.items()}
    if TURKISH_LETTERS & set(text.casefold()):
        scores["tr"] += 2
    best = max(scores, key=scores.get)
    others = max(v for lang, v in scores.items() if lang != best)
    return best if scores[best] >= 2 and scores[best] > others else None


def _foreign_share(window, language):
    if language not in LANGUAGE_WORDS:
        return 0.0
    words = sum(len(s.words) for s in window)
    foreign = sum(len(s.words) for s in window if sentence_language(s.text) not in (None, language))
    return foreign / max(words, 1)


def _features(window, loudness_db, step, voiced_median):
    start, end = window[0].start, window[-1].end
    duration = max(end - start, 0.1)
    words = [w for sentence in window for w in sentence.words]
    tokens = [t for sentence in window for t in _tokens(sentence.text)]
    gaps = sum(max(0.0, b.start - a.end - 0.3) for a, b in zip(words, words[1:]))
    level = loudness_db[int(start / step): int(end / step) + 1]
    first_tokens = _tokens(window[0].text)
    return {
        "rate": len(words) / duration,
        "loudness": float(np.mean(level)) - voiced_median if len(level) else 0.0,
        "dynamics": float(np.std(level)) if len(level) else 0.0,
        "keywords": sum(_is_hook(t) for t in tokens) / duration * 60,
        "hook": float(window[0].text.endswith("?") or any(_is_hook(t) for t in first_tokens[:6])),
        "silence": gaps / duration,
        "filler": float(bool(first_tokens) and first_tokens[0] in FILLER_STARTS),
        "length": duration,
        "repeat": 1 - len({s.text.casefold() for s in window}) / len(window),
    }


def _title(window):
    title = ""
    for sentence in window:
        title = f"{title} {sentence.text}".strip()
        if len(title) >= 25:
            break
    if len(title) > TITLE_LIMIT:
        title = title[: TITLE_LIMIT - 1].rsplit(" ", 1)[0] + "…"
    return title


def find(segments, loudness_db, count, min_len, max_len, language=None, step=0.5):
    """En yüksek puanlı, birbiriyle çakışmayan en fazla count anı döner.

    language verilirse ağırlıklı olarak başka dilde konuşulan bölümler elenir.
    """
    sentences = split_sentences(segments)
    voiced = loudness_db[loudness_db > np.percentile(loudness_db, 30)] if len(loudness_db) else loudness_db
    voiced_median = float(np.median(voiced)) if len(voiced) else 0.0

    windows = []
    for i in range(len(sentences)):
        for j in range(i, len(sentences)):
            duration = sentences[j].end - sentences[i].start
            if duration > max_len:
                break
            if duration >= min_len:
                windows.append(sentences[i: j + 1])
    native = [w for w in windows if _foreign_share(w, language) <= FOREIGN_LIMIT]
    windows = native or windows
    if not windows:
        return []

    table = [_features(w, loudness_db, step, voiced_median) for w in windows]
    scores = np.zeros(len(windows))
    for name, weight in WEIGHTS.items():
        values = np.array([row[name] for row in table])
        spread = values.std()
        if spread > 1e-9:
            scores += weight * (values - values.mean()) / spread
    # Cümle sonunda biten bölüm yarım kalmış hissi vermez.
    scores += np.array([0.3 if SENTENCE_END.search(w[-1].text) else 0.0 for w in windows])

    chosen = []
    for index in np.argsort(-scores):
        window = windows[index]
        start, end = window[0].start, window[-1].end
        if all(end + MIN_SEPARATION <= c.start or start >= c.end + MIN_SEPARATION for c in chosen):
            chosen.append(Highlight(
                start=max(0.0, start - START_PAD),
                end=end + END_PAD,
                score=float(scores[index]),
                title=_title(window),
                text=" ".join(s.text for s in window),
            ))
        if len(chosen) == count:
            break
    return chosen


def words_between(segments, start, end):
    return [w for segment in segments for w in segment.words if w.start >= start and w.end <= end]


__all__ = ["Highlight", "Segment", "find", "split_sentences", "words_between"]
