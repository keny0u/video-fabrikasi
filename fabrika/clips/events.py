"""Korku ve komik anları sesin, görüntünün ve konuşmanın ani değişimlerinden bulur.

Korku anı: ses aniden patlar (çığlık, jumpscare sesi), kameradaki kişi irkilir,
oyun ekranı birden değişir, "siktir, korktum, anne" gibi tepkiler gelir.
Komik an: gülme kelimeleri, kameradaki kişinin gülerken hareketlenmesi ve ses patlamaları.
Klip anın biraz öncesinden başlar ki gerilim ya da espri kurulsun, tepkiyle biter.
"""
import re

import numpy as np

from .highlights import END_PAD, Highlight, _tokens, split_sentences

STEP = 0.25
BASELINE_SECONDS = 6
SIGNAL_CAP = 6.0
STRONG = 3.0
TOGETHER_BONUS = 2.5
INTRO_SECONDS = 30
SMOOTH_SECONDS = 1.0
SNAP_SECONDS = 3.0

# "ne, yok, lan" gibi her cümlede geçen kelimeler bilerek yok; yanlış alarm üretirler.
SCARE_WORDS = {
    "aa", "aaa", "aaaa", "ahh", "siktir", "hassiktir", "allahım", "korktum", "korkuttu", "korkuyorum",
    "anne", "anneciğim", "anam", "kalbim", "ödüm", "kaç", "koş", "oha", "imdat", "noluyor", "neydi",
    "bismillah", "korkunç", "fuck", "shit", "run",
}
LAUGH_WORDS = {
    "haha", "hahaha", "hahahaha", "ahaha", "ahahah", "hehe", "kahkaha", "güldüm", "öldüm", "komik", "şaka",
    "gülmekten", "gülüyorum", "kopuyorum", "lol", "funny", "laugh",
}
LAUGH_PATTERN = re.compile(r"^(ha|ah|he|hi|ja|js){2,}h?$")

KINDS = {
    # ağırlıklar: ses patlaması, kamera hareketi, ekran değişimi, tepki kelimeleri
    "korku": {"audio": 1.0, "camera": 1.0, "game": 0.8, "words": 1.5, "before": 10.0},
    "komik": {"audio": 0.6, "camera": 0.8, "game": 0.2, "words": 1.8, "before": 15.0},
}
TITLES = {"korku": "Korkunç an", "komik": "Komik an"}


def _robust_z(values):
    """Aykırı değerlere dayanıklı standartlaştırma; yalnızca ortalamanın üstü sayılır."""
    if len(values) == 0:
        return values
    median = np.median(values)
    spread = np.median(np.abs(values - median)) * 1.4826 + 1e-6
    return np.clip((values - median) / spread, 0, 10)


def _resample(values, source_step, length):
    if len(values) == 0:
        return np.zeros(length)
    positions = np.minimum((np.arange(length) * STEP / source_step).astype(int), len(values) - 1)
    return values[positions]


def _sudden_rise(levels):
    """Sesin son birkaç saniyelik ortalamasına göre ne kadar aniden yükseldiği."""
    window = int(BASELINE_SECONDS / STEP)
    cumulative = np.concatenate([[0.0], np.cumsum(levels)])
    index = np.arange(len(levels))
    begin = np.maximum(index - window, 0)
    counts = np.maximum(index - begin, 1)
    baseline = (cumulative[index] - cumulative[begin]) / counts
    baseline[0] = levels[0]
    return levels - baseline


def _is_reaction(token, kind):
    if kind == "komik":
        return token in LAUGH_WORDS or bool(LAUGH_PATTERN.match(token))
    return token in SCARE_WORDS or bool(re.match(r"^a{3,}h*$", token))


def curve(kind, duration, loudness_db, loudness_step, motion, segments):
    """Videonun her çeyrek saniyesi için 'ne kadar korku / komik anı' puanı."""
    weights = KINDS[kind]
    length = int(duration / STEP) + 1
    # Hepsi son birkaç saniyeye göre ani artış olarak ölçülür: sürekli kıpırdanan yayıncı
    # ya da hep hareketli oyun değil, birden irkilme ve birden değişen ekran puan alır.
    audio = _robust_z(_sudden_rise(_resample(loudness_db, loudness_step, length)))
    if motion:
        camera = _robust_z(_sudden_rise(_resample(motion.camera, motion.step, length)))
        game = _robust_z(_sudden_rise(_resample(motion.game, motion.step, length)))
    else:
        camera = game = np.zeros(length)
    words = np.zeros(length)
    for word in (w for segment in segments for w in segment.words):
        if any(_is_reaction(t, kind) for t in _tokens(word.text)):
            words[min(int(word.start / STEP), length - 1)] += 1
    window = np.ones(int(SMOOTH_SECONDS / STEP))
    words = np.minimum(np.convolve(words, window, "same"), 3)
    signals = [audio, camera, game, words]
    score = sum(weights[name] * np.minimum(signal, SIGNAL_CAP)
                for name, signal in zip(("audio", "camera", "game", "words"), signals))
    # Gerçek an birkaç işaretin aynı anda patlamasıdır; tek başına bir sıçrama yetmez.
    together = sum((np.convolve(signal, window, "same") > STRONG).astype(float) for signal in signals)
    score += TOGETHER_BONUS * np.maximum(together - 1, 0)
    score[: int(INTRO_SECONDS / STEP)] = 0  # girişteki kurgu kesmeleri korku anı sayılmasın
    return np.convolve(score, window / len(window), "same")


def _snap(sentences, start, end, max_len):
    """Klibi cümle ortasından başlatmamak ve bitirmemek için en yakın cümle sınırına kaydırır."""
    for sentence in sentences:
        if sentence.start < start < sentence.end and start - sentence.start <= SNAP_SECONDS:
            start = sentence.start
            break
    for sentence in sentences:
        if sentence.start < end < sentence.end and sentence.end - end <= SNAP_SECONDS:
            if sentence.end - start <= max_len:
                end = sentence.end + END_PAD
            break
    return start, end


def _title(sentences, moment, fallback):
    nearby = [s.text for s in sentences if s.end >= moment - 3 and s.start <= moment + 3]
    title = " ".join(nearby).strip()
    if not title:
        return fallback
    return title if len(title) <= 80 else title[:79].rsplit(" ", 1)[0] + "…"


def windows_around(peaks, segments, duration, before, min_len, max_len, fallback):
    """Her önemli anın biraz öncesinden başlayıp tepkiyle biten klip aralıkları."""
    sentences = split_sentences(segments)
    length = max(min_len, min(max_len, before + 15))
    moments = []
    for peak, score in peaks:
        start = max(0.0, peak - before)
        end = min(duration, start + length)
        start, end = _snap(sentences, start, end, max_len)
        moments.append(Highlight(start, end, float(score), _title(sentences, peak, fallback), ""))
    return moments


def find(kind, score_curve, segments, duration, count, min_len, max_len):
    separation = max(min_len, 20)
    peaks = []
    for index in np.argsort(-score_curve):
        moment = index * STEP
        if score_curve[index] <= 0:
            break
        if all(abs(moment - other) >= separation for other, _ in peaks):
            peaks.append((moment, score_curve[index]))
        if len(peaks) == count:
            break
    return windows_around(peaks, segments, duration, KINDS[kind]["before"], min_len, max_len, TITLES[kind])


def keyword_moments(segments, keyword, duration, count, min_len, max_len):
    """Konuşmada aranan kelimenin geçtiği yerlerden klip çıkarır."""
    wanted = _tokens(keyword)
    if not wanted:
        return []
    words = [w for segment in segments for w in segment.words]
    peaks = []
    for index, word in enumerate(words):
        following = [t for w in words[index: index + len(wanted)] for t in _tokens(w.text)][: len(wanted)]
        if len(following) == len(wanted) and all(t.startswith(k) for t, k in zip(following, wanted)):
            if all(abs(word.start - other) >= max(min_len, 20) for other, _ in peaks):
                peaks.append((word.start, 1.0))
        if len(peaks) == count:
            break
    return windows_around(peaks, segments, duration, 6.0, min_len, max_len, keyword)


TIME_RANGE = re.compile(r"(\d{1,2}(?::\d{1,2}){0,2})\s*[-–]\s*(\d{1,2}(?::\d{1,2}){0,2})")


def _seconds(text):
    value = 0
    for part in text.split(":"):
        value = value * 60 + int(part)
    return value


def parse_ranges(text, duration=None):
    """'12:30-13:10, 1:02:05-1:02:40' gibi elle girilen zaman aralıklarını okur."""
    ranges = []
    for begin, finish in TIME_RANGE.findall(text or ""):
        start, end = _seconds(begin), _seconds(finish)
        if end <= start:
            raise ValueError(f"{begin}-{finish}: bitiş başlangıçtan sonra olmalı.")
        if end - start > 180:
            raise ValueError(f"{begin}-{finish}: bir klip en fazla 3 dakika olabilir.")
        if duration is not None and start >= duration:
            raise ValueError(f"{begin}: video bu kadar uzun değil.")
        ranges.append((start, min(end, duration) if duration else end))
    return ranges


def manual_moments(ranges, segments):
    sentences = split_sentences(segments)
    return [
        Highlight(start, end, 0.0, _title(sentences, start + 2, f"{int(start // 60)}:{int(start % 60):02}"), "")
        for start, end in ranges
    ]
