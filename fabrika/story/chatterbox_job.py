"""Chatterbox seslendirme işçisi: .venv-ses ortamında ayrı süreç olarak çalışır (ana ortamın paketleriyle çakışıyor).

Kullanım: python chatterbox_job.py is.json
is.json: {"jobs": [{"text", "reference", "exaggeration", "cfg", "seed", "out"}]}
Model bir kez yüklenir, bütün cümleler sırayla okunur; her biri kendi wav dosyasına yazılır.
"""
import json
import sys

import torch
import torchaudio
from chatterbox.mtl_tts import ChatterboxMultilingualTTS


def main():
    jobs = json.loads(open(sys.argv[1], encoding="utf-8").read())["jobs"]
    model = ChatterboxMultilingualTTS.from_pretrained(device="cuda" if torch.cuda.is_available() else "cpu")
    for index, job in enumerate(jobs, start=1):
        torch.manual_seed(job.get("seed", 0))
        wav = model.generate(job["text"], language_id="tr", audio_prompt_path=job["reference"],
                             exaggeration=job["exaggeration"], cfg_weight=job["cfg"])
        torchaudio.save(job["out"], wav, model.sr)
        print(f"ilerleme {index}/{len(jobs)}", flush=True)


if __name__ == "__main__":
    main()
