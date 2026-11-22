import importlib.util
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

MODEL_NAME = "large-v3-turbo"
CUDA_PACKAGES = ("cublas", "cudnn", "cuda_nvrtc")

_model = None
_model_lock = threading.Lock()


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list


def _add_cuda_dlls():
    """pip ile gelen CUDA kütüphanelerini Windows'un DLL aramasına ekler."""
    spec = importlib.util.find_spec("nvidia")
    if spec is None:
        return
    for root in spec.submodule_search_locations:
        for package in CUDA_PACKAGES:
            directory = Path(root) / package / "bin"
            if directory.is_dir():
                os.add_dll_directory(str(directory))
                os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ['PATH']}"


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
            _add_cuda_dlls()
            import ctranslate2
            from faster_whisper import WhisperModel

            if ctranslate2.get_cuda_device_count() > 0:
                _model = WhisperModel(MODEL_NAME, device="cuda", compute_type="float16")
            else:
                _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
        return _model


def release():
    """Modeli ekran kartı belleğinden çıkarır; görsel üretimi gibi işlere yer açılsın."""
    global _model
    with _model_lock:
        _model = None
    import gc

    gc.collect()


def save(segments, language, path):
    data = {
        "language": language,
        "segments": [
            {"start": s.start, "end": s.end, "text": s.text, "words": [[w.start, w.end, w.text] for w in s.words]}
            for s in segments
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = [
        Segment(s["start"], s["end"], s["text"], [Word(*w) for w in s["words"]])
        for s in data["segments"]
    ]
    return segments, data["language"]


def transcribe(samples, language=None, on_progress=None):
    """16 kHz sesi kelime zamanlarıyla yazıya döker; dil verilmezse kendisi bulur."""
    model = _load_model()
    segments, info = model.transcribe(
        samples,
        language=language or None,
        word_timestamps=True,
        vad_filter=True,
        # Önceki metne bakmamak, uzun videolarda aynı cümleyi tekrarlama döngüsünü önler.
        condition_on_previous_text=False,
    )
    result = []
    for segment in segments:
        words = [Word(w.start, w.end, w.word.strip()) for w in segment.words or [] if w.word.strip()]
        result.append(Segment(segment.start, segment.end, segment.text.strip(), words))
        if on_progress and info.duration:
            on_progress(min(segment.end / info.duration, 1.0))
    return result, info.language
