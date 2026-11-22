"""Sahne görsellerini ekran kartında SDXL ile üretir (SDXL-Lightning: 4 adımda bir görsel)."""
import gc
import threading

from ..clips.transcribe import _add_cuda_dlls

BASE_MODEL = "stabilityai/stable-diffusion-xl-base-1.0"
LIGHTNING = ("ByteDance/SDXL-Lightning", "sdxl_lightning_4step_lora.safetensors")
# 9:16 dikey. 768x1344'te 8 GB ekran kartının belleği taşıp görsel başına 6 dakikaya çıkıyor; bu boyutta 8 saniye.
WIDTH, HEIGHT = 640, 1136
STEPS = 4

STYLES = {
    "cizgi": "flat 2d cartoon illustration, clean vector art, bold outlines, simple shapes, soft pastel colors, "
             "storybook animation style, consistent character design",
    "gercekci": "cinematic photograph, realistic, dramatic lighting, shallow depth of field, film still, 35mm",
    "korku": "dark horror illustration, eerie atmosphere, muted desaturated colors, deep shadows, cinematic",
    "anime": "anime style illustration, studio quality, vibrant colors, detailed background, cel shading",
}

_pipeline = None
_lock = threading.Lock()


def _load():
    global _pipeline
    if _pipeline is None:
        _add_cuda_dlls()
        import torch
        from diffusers import EulerDiscreteScheduler, StableDiffusionXLPipeline

        pipeline = StableDiffusionXLPipeline.from_pretrained(BASE_MODEL, torch_dtype=torch.float16, variant="fp16")
        pipeline.load_lora_weights(LIGHTNING[0], weight_name=LIGHTNING[1])
        pipeline.fuse_lora()
        pipeline.scheduler = EulerDiscreteScheduler.from_config(pipeline.scheduler.config, timestep_spacing="trailing")
        # 8 GB ekran kartında Whisper ile birlikte sığsın diye modelin kullanılmayan parçaları işlemcide bekler.
        pipeline.enable_model_cpu_offload()
        # Dikey görselin son çözme adımı belleği en çok zorlayan yer; parça parça yapılır.
        pipeline.vae.enable_tiling()
        _pipeline = pipeline
    return _pipeline


def draw(prompt, style, seed):
    """Tek görsel çizer (PIL); tarz yazısı tarifin sonuna eklenir."""
    import torch

    with _lock:
        return _load()(
            prompt=f"{prompt}, {STYLES.get(style, STYLES['cizgi'])}, no text",
            num_inference_steps=STEPS,
            guidance_scale=0.0,
            width=WIDTH,
            height=HEIGHT,
            generator=torch.Generator("cpu").manual_seed(seed),
        ).images[0]


def generate(prompts, style, seed, outdir, on_progress=None, name="sahne"):
    """Her tarif için bir görsel; aynı tohum ve tarz sahneler arasında görünümü yakın tutar."""
    paths = []
    for index, prompt in enumerate(prompts):
        path = outdir / f"{name}_{index + 1:02}.png"
        draw(prompt, style, seed).save(path)
        paths.append(path)
        if on_progress:
            on_progress((index + 1) / len(prompts))
    return paths


def release():
    """Görsel üretimi bitince ekran kartı belleğini Whisper ve diğer işler için boşaltır."""
    global _pipeline
    with _lock:
        _pipeline = None
        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except ImportError:
            pass
