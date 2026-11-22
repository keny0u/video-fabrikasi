import subprocess

import imageio_ffmpeg

RENDER_TIMEOUT = 1800
GPU_ENCODER = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
CPU_ENCODER = ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
AUDIO = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
OUT_WIDTH, OUT_HEIGHT = 1080, 1920
CAMERA_SIZES = {"kucuk": 320, "orta": 450, "buyuk": 640}  # üstteki kamera bölümünün yüksekliği
SEAM_SUBTITLE_OFFSET = 110
FIT_SUBTITLE_GAP = 130
MIN_SUBTITLE_MARGIN = 120
HIDE_PADDING = 0.015
EDGE_TOLERANCE = 0.04  # kamera kenara bu kadar yakınsa kenara yaslı sayılır
MIN_GAME_SHARE = 0.4  # kesit oyunun bundan küçük bir parçası olmasın


def _even(value):
    return max(2, int(value) // 2 * 2)


def split_layout(plan):
    """Kamera kutusunun kaynaktaki piksel alanı ve çıktıdaki kamera bölümünün yüksekliği."""
    x, y, w, h = plan.box
    camera = (
        _even(w * plan.width), _even(h * plan.height),
        int(x * plan.width) // 2 * 2, int(y * plan.height) // 2 * 2,
    )
    return camera, _even(plan.camera_height)


def game_area(plan):
    """Oyun görüntüsünden kamera kutusunu dışarıda bırakan en büyük kesit: (x, y, genişlik, yükseklik).

    Kamera bir kenara yaslıysa o kenardaki şerit atılır; ekranda ikinci kamera ya da siyah kutu kalmaz.
    Kamera hiçbir kenara değmiyorsa kesit bütün görüntüdür ve kamera üstü kapatılır (hide_needed).
    """
    (cam_w, cam_h, cam_x, cam_y), _ = split_layout(plan)
    pad_x, pad_y = int(plan.width * HIDE_PADDING), int(plan.height * HIDE_PADDING)
    left, top = max(cam_x - pad_x, 0), max(cam_y - pad_y, 0)
    right, bottom = min(cam_x + cam_w + pad_x, plan.width), min(cam_y + cam_h + pad_y, plan.height)
    edge_x, edge_y = plan.width * EDGE_TOLERANCE, plan.height * EDGE_TOLERANCE
    options = []
    if left <= edge_x:
        options.append((right, 0, plan.width - right, plan.height))
    if right >= plan.width - edge_x:
        options.append((0, 0, left, plan.height))
    if top <= edge_y:
        options.append((0, bottom, plan.width, plan.height - bottom))
    if bottom >= plan.height - edge_y:
        options.append((0, 0, plan.width, top))
    options = [o for o in options if o[2] >= plan.width * MIN_GAME_SHARE and o[3] >= plan.height * MIN_GAME_SHARE]
    if not options:
        return (0, 0, plan.width, plan.height), True
    gx, gy, gw, gh = max(options, key=lambda o: o[2] * o[3])
    return (gx // 2 * 2, gy // 2 * 2, _even(gw), _even(gh)), False


def fit_layout(plan):
    """Oyunun tamamı görünen düzen: oyun boyutu, kameranın ve oyunun dikey konumu.

    Kamera ile oyun birlikte ekranın ortasına yerleşir; altta altyazıya yer kalır.
    """
    _, top = split_layout(plan)
    (_, _, area_w, area_h), _ = game_area(plan)
    game_w, game_h = OUT_WIDTH, round(OUT_WIDTH * area_h / area_w / 2) * 2
    if top + game_h > OUT_HEIGHT - MIN_SUBTITLE_MARGIN:
        # Dar kesit çok uzun olursa yüksekliğe sığdırılır, iki yanı bulanık kalır.
        game_h = _even(OUT_HEIGHT - MIN_SUBTITLE_MARGIN - top)
        game_w = _even(game_h * area_w / area_h)
    camera_y = max(0, OUT_HEIGHT - top - game_h) // 2 // 2 * 2
    return game_w, game_h, camera_y, camera_y + top


def subtitle_margin(plan):
    """Oyun düzeninde altyazının alttan uzaklığı."""
    _, top = split_layout(plan)
    if plan.game_fit:
        _, game_h, _, game_y = fit_layout(plan)
        return max(MIN_SUBTITLE_MARGIN, OUT_HEIGHT - (game_y + game_h) - FIT_SUBTITLE_GAP)
    return OUT_HEIGHT - top - SEAM_SUBTITLE_OFFSET


def video_filter(plan, subtitles_file):
    """Kadraj planına göre 1080x1920 çıkış üreten ffmpeg filtresini kurar."""
    if plan.mode == "track":
        x0 = plan.positions[0][1] if plan.positions else 0
        chain = (
            f"[0:v]sendcmd=f=kadraj.cmd,crop=w={plan.crop_width}:h=ih:x={x0}:y=0,"
            "scale=1080:1920,setsar=1"
        )
    elif plan.mode == "split":
        (cam_w, cam_h, cam_x, cam_y), top = split_layout(plan)
        (gx, gy, gw, gh), hide_needed = game_area(plan)
        game_source = f"crop={gw}:{gh}:{gx}:{gy}"
        if hide_needed:
            # Kamera ortada yüzüyorsa kesip atılamaz; üstü kapatılır ki ekranda iki kamera olmasın.
            pad_x, pad_y = int(plan.width * HIDE_PADDING), int(plan.height * HIDE_PADDING)
            game_source = (f"drawbox=x={max(cam_x - pad_x, 0)}:y={max(cam_y - pad_y, 0)}:"
                           f"w={cam_w + 2 * pad_x}:h={cam_h + 2 * pad_y}:color=black:t=fill")
        if plan.game_fit:
            game_w, game_h, camera_y, game_y = fit_layout(plan)
            game = f"[oyun]{game_source},scale={game_w}:{game_h}[alt];"
            game_x = (OUT_WIDTH - game_w) // 2
        else:
            camera_y, game_y, game_x = 0, top, 0
            bottom = OUT_HEIGHT - top
            game = (f"[oyun]{game_source},scale={OUT_WIDTH}:{bottom}:force_original_aspect_ratio=increase,"
                    f"crop={OUT_WIDTH}:{bottom}[alt];")
        chain = (
            "[0:v]split=3[arka][kam][oyun];"
            f"[arka]{game_source},scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,"
            f"scale={OUT_WIDTH}:{OUT_HEIGHT}[bg];"
            # Kamera kırpılmadan küçültülür; iki yanı bulanık arka plan olarak kalır.
            f"[kam]crop={cam_w}:{cam_h}:{cam_x}:{cam_y},"
            f"scale={OUT_WIDTH}:{top}:force_original_aspect_ratio=decrease:force_divisible_by=2[ust];"
            + game +
            f"[bg][alt]overlay={game_x}:{game_y}[ara];[ara][ust]overlay=(W-w)/2:{camera_y},setsar=1"
        )
    elif plan.mode == "blur":
        chain = (
            "[0:v]split[arka][on];"
            # Bulanıklık küçük boyutta hesaplanıp büyütülür; tam boyutta yapmaktan katlarca hızlı.
            "[arka]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,"
            "scale=1080:1920[bg];"
            "[on]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    else:
        chain = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"
        )
    if subtitles_file:
        chain += f",ass={subtitles_file}"
    return chain + ",format=yuv420p[v]"


def crop_commands(plan):
    return "".join(f"{moment:.3f} crop x {x};\n" for moment, x in plan.positions)


def render(source, output, start, end, plan, subtitles_text, workdir):
    """Klibi keser, dikeye çevirir, altyazıyı gömer. Ekran kartı yoksa işlemciyle kodlar."""
    # ffmpeg filtrelerinde Windows yolları kaçış ister; yardımcı dosyalar göreli adla verilir.
    if plan.mode == "track":
        (workdir / "kadraj.cmd").write_text(crop_commands(plan), encoding="utf-8")
    subtitles_file = None
    if subtitles_text:
        subtitles_file = "altyazi.ass"
        (workdir / subtitles_file).write_text(subtitles_text, encoding="utf-8")

    base = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
        "-filter_complex", video_filter(plan, subtitles_file), "-map", "[v]", "-map", "0:a?",
    ]
    tail = [*AUDIO, "-r", "30", "-movflags", "+faststart", str(output)]
    error = ""
    for encoder in (GPU_ENCODER, CPU_ENCODER):
        try:
            result = subprocess.run(
                base + encoder + tail, cwd=workdir, stdin=subprocess.DEVNULL,
                capture_output=True, timeout=RENDER_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("Klip işleme zaman aşımına uğradı.")
        if result.returncode == 0:
            return
        error = result.stderr.decode("utf-8", "replace").strip()[-400:]
    raise ValueError(f"Klip işlenemedi: {error}")
