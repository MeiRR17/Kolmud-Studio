"""Offline text slides rendered from measured narration, without stock/media services."""

from __future__ import annotations

import math
import subprocess
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

from core import audio_utils
from core.artifacts import private_text

RENDERER_VERSION = 1
WIDTH, HEIGHT, FPS = 1920, 1080, 30


def find_font(configured: Path | None = None) -> Path:
    candidates = [configured] if configured else [
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for path in candidates:
        if path and path.expanduser().is_file():
            return path.expanduser().resolve()
    raise ValueError("No local font found. Set VIDEO_FONT_PATH to an existing Hebrew/English TrueType font.")


def _rtl(text):
    return any(unicodedata.bidirectional(char) in {"R", "AL"} for char in text)


def _lines(draw, text, font, width):
    direction = "rtl" if _rtl(text) else "ltr"
    if direction == "rtl" and not features.check_feature("raqm"):
        raise ValueError("Hebrew video text requires a Pillow build with libraqm support.")
    lines, line = [], ""
    for word in text.split():
        proposed = (line + " " + word).strip()
        if draw.textlength(proposed, font=font, direction=direction) <= width:
            line = proposed
        else:
            if not line or draw.textlength(word, font=font, direction=direction) > width:
                raise ValueError("An unbroken word is too wide for the video slide; edit the paragraph.")
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines, direction


def _fit(draw, text, font_path, box, initial, minimum):
    width, height = box[2] - box[0], box[3] - box[1]
    for size in range(initial, minimum - 1, -2):
        font = ImageFont.truetype(str(font_path), size)
        try:
            lines, direction = _lines(draw, text, font, width)
        except ValueError as exc:
            if "libraqm" in str(exc):
                raise
            continue
        line_height = math.ceil(size * 1.5)
        if lines and len(lines) * line_height <= height:
            return font, lines, line_height, direction
    raise ValueError("Paragraph/title does not fit a readable slide; split or edit it before rendering. No text was truncated.")


def slide(title: str, text: str, position: int, count: int, font_path: Path, destination: Path):
    """Typeset all text with bidi shaping; refuse overflow instead of hiding words."""
    canvas = Image.new("RGB", (WIDTH, HEIGHT), "#101c2c")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((48, 48, WIDTH - 48, HEIGHT - 48), radius=30, outline="#2c4056", width=2)
    draw.rounded_rectangle((88, 94, 102, 132), radius=7, fill="#5bd2b8")
    small = ImageFont.truetype(str(font_path), 24)
    draw.text((122, 98), "KOLMUD STUDIO  /  ENGLISH LESSON", font=small, fill="#9eb2c8")
    title_box = (100, 168, WIDTH - 100, 290)
    font, lines, height, direction = _fit(draw, title, font_path, title_box, 52, 30)
    for i, line in enumerate(lines):
        draw.text((WIDTH - 100 if direction == "rtl" else 100, title_box[1] + i * height), line,
                  anchor="rt" if direction == "rtl" else "lt", direction=direction, font=font, fill="#f1f5fa")
    draw.rounded_rectangle((86, 318, WIDTH - 86, 900), radius=28, fill="#1c2d42")
    body_box = (132, 350, WIDTH - 132, 864)
    font, lines, line_height, direction = _fit(draw, text, font_path, body_box, 60, 32)
    y = body_box[1] + (body_box[3] - body_box[1] - len(lines) * line_height) / 2
    for line in lines:
        draw.text((body_box[2] if direction == "rtl" else body_box[0], y), line, font=font,
                  anchor="rt" if direction == "rtl" else "lt", direction=direction, fill="#edf3fb")
        y += line_height
    draw.text((100, 953), "English narration · text slides", font=small, fill="#9eb2c8")
    draw.text((WIDTH - 100, 953), f"{position:02d} / {count:02d}", anchor="rt", font=small, fill="#9eb2c8")
    draw.rounded_rectangle((100, 1006, WIDTH - 100, 1012), radius=3, fill="#2c4056")
    draw.rounded_rectangle((100, 1006, 100 + (WIDTH - 200) * position / count, 1012), radius=3, fill="#5bd2b8")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    canvas.save(destination, format="PNG")
    destination.chmod(0o600)
    return {"bodyFontSize": font.size, "lineCount": len(lines), "direction": direction}


def render(title: str, scenes: list[dict], audio: Path, directory: Path, font_path: Path) -> dict:
    duration = audio_utils.duration_seconds(audio)
    if not math.isfinite(duration) or duration <= 0 or not scenes:
        raise ValueError("Nonempty narration and scenes are required for video generation.")
    total_frames = math.ceil(duration * FPS)
    schedule = []
    previous = 0.0
    for scene in scenes:
        start, end = float(scene["start"]), float(scene["end"])
        if not all(math.isfinite(v) for v in (start, end)) or start < previous - 1e-6 or end <= start or end > duration + 1e-6 or not scene["text"].strip():
            raise ValueError("Invalid/overlapping narration scene timing.")
        a, b = math.ceil(start * FPS - 1e-8), min(total_frames, math.ceil(end * FPS - 1e-8))
        if b <= a or end - start < 1 / FPS - 1e-8:
            raise ValueError("A narration scene is shorter than one video frame.")
        if a > (schedule[-1]["endFrame"] if schedule else 0):
            schedule.append({"startFrame": schedule[-1]["endFrame"] if schedule else 0, "endFrame": a,
                             "text": "English lesson", "paragraph": None})
        schedule.append({**scene, "startFrame": a, "endFrame": b})
        previous = end
    if schedule[-1]["endFrame"] < total_frames:
        schedule.append({"startFrame": schedule[-1]["endFrame"], "endFrame": total_frames,
                         "text": "English lesson", "paragraph": None})
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    instructions = ["ffconcat version 1.0"]
    for i, scene in enumerate(schedule):
        path = directory / f"slide_{i:05d}.png"
        scene["layout"] = slide(title, scene["text"], i + 1, len(schedule), font_path, path)
        scene["image"] = path.name
        instructions += [f"file {path.name}", f"option framerate {FPS}",
                         f"duration {(scene['endFrame'] - scene['startFrame']) / FPS:.9f}"]
    # Concat needs a final image to honour the preceding duration.
    instructions += [f"file {schedule[-1]['image']}", f"option framerate {FPS}"]
    concat = directory / "slides.ffconcat"
    private_text(concat, "\n".join(instructions) + "\n")
    video = directory / "lesson.en.mp4"
    # Own generated relative filenames only; no user text, paths or URLs enter ffconcat syntax.
    command = [audio_utils._ffmpeg(), "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
               "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "0", "-i", str(concat.resolve()),
               "-i", str(audio.resolve()), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
               "-threads", "2", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-vf", f"fps={FPS}", "-r", str(FPS), "-fps_mode", "cfr", "-t", f"{total_frames / FPS:.9f}",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(video.resolve())]
    try:
        result = subprocess.run(command, capture_output=True, timeout=max(120, duration * 12))
    except subprocess.TimeoutExpired:
        raise ValueError("Local video rendering timed out; no complete generation was recorded.") from None
    if result.returncode or not video.is_file() or video.stat().st_size == 0:
        raise ValueError("Local video rendering failed; check ffmpeg PNG/libx264/AAC support.")
    video.chmod(0o600)
    return {"scenes": schedule, "durationSeconds": duration, "videoDurationSeconds": total_frames / FPS,
            "width": WIDTH, "height": HEIGHT, "fps": FPS, "totalFrames": total_frames}
