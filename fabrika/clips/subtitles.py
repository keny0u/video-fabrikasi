"""Kelime kelime yanan, kısa öbeklerden oluşan altyazıyı ASS biçiminde üretir."""

FONT = "Arial Black"
FONT_SIZE = 82
MARGIN_BOTTOM = 560  # platformların alt düğmeleri altyazının üstüne binmesin
MAX_WORDS = 3
MAX_CHARS = 18
CHUNK_GAP = 0.6
LINGER = 0.3
ACTIVE_COLOR = "&H0000D7FF&"  # ASS renkleri BGR: altın sarısı

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Klip,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _clock(seconds):
    centiseconds = max(0, round(seconds * 100))
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{cs:02}"


def _escape(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def chunk_words(words):
    """Kelimeleri ekranda birlikte görünecek 1-3 kelimelik öbeklere ayırır."""
    chunks, current = [], []
    for word in words:
        too_long = len(" ".join(w.text for w in current + [word])) > MAX_CHARS
        paused = current and word.start - current[-1].end > CHUNK_GAP
        if current and (len(current) == MAX_WORDS or too_long or paused):
            chunks.append(current)
            current = []
        current.append(word)
        if word.text[-1:] in ".!?…,":
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def build(words, offset, margin_bottom=MARGIN_BOTTOM):
    """offset: klibin kaynak videodaki başlangıcı; zamanlar klibe göre kaydırılır."""
    lines = [HEADER.format(font=FONT, size=FONT_SIZE, margin=margin_bottom)]
    chunks = chunk_words(words)
    for index, chunk in enumerate(chunks):
        next_start = chunks[index + 1][0].start if index + 1 < len(chunks) else None
        chunk_end = chunk[-1].end + LINGER
        if next_start is not None:
            chunk_end = min(chunk_end, next_start)
        for position, word in enumerate(chunk):
            start = word.start
            end = chunk[position + 1].start if position + 1 < len(chunk) else chunk_end
            parts = []
            for other_position, other in enumerate(chunk):
                text = _escape(other.text)
                if other_position == position:
                    text = f"{{\\c{ACTIVE_COLOR}\\fscx108\\fscy108}}{text}{{\\r}}"
                parts.append(text)
            lines.append(
                f"Dialogue: 0,{_clock(start - offset)},{_clock(end - offset)},Klip,,0,0,0,,{' '.join(parts)}\n"
            )
    return "".join(lines)
