"""End-to-end probe of FFmpeg zoom/pan and the three selected xfade effects."""
import ast
import concurrent.futures as cf
from pathlib import Path
import os
import random
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace

from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
source = (root / "pipeline.py").read_text(encoding="utf-8")
ffmpeg = root / "ffmpeg" / "ffmpeg.exe"
ffmpeg = str(ffmpeg) if ffmpeg.is_file() else "ffmpeg"
tree = ast.parse(source)
functions = {"ffmpeg_bin", "video_encoder_args", "_fit_16_9", "render_scene_clip",
             "render_video_clip", "xfade_chain", "assemble_video", "concat_with_transitions"}
nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in functions]
ns = {"os": os, "subprocess": subprocess, "threading": threading, "cf": cf, "NO_WINDOW": 0,
      "W": 640, "H": 360, "FPS": 25, "EDGE_CROP": 0.035, "TRANSITION": 0.5,
      "TRANSITIONS": ["zoomin", "slideleft", "slideright", "slideup", "slidedown"], "XFADE_GROUP": 30,
      "_NVENC_LOCK": threading.Lock(), "_NVENC_AVAILABLE": None,
      "app_dir": lambda: str(root), "log": lambda message: None, "check_cancel": lambda: None}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "pipeline.py", "exec"), ns)

def run_ffmpeg(args):
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", *args], check=True)

ns["run_ffmpeg"] = run_ffmpeg
with tempfile.TemporaryDirectory() as scratch:
    clips = []
    for i in range(6):
        image = Path(scratch) / f"image{i}.png"
        Image.new("RGB", (800, 450), (60 * i, 100, 150)).save(image)
        clip = Path(scratch) / f"clip{i}.mp4"
        ns["render_scene_clip"](str(image), i, 1.3, str(clip))
        clips.append(str(clip))
    args, filt = ns["xfade_chain"](clips, [0.8] * 6)
    assert all(effect in filt for effect in ("zoomin", "slideleft", "slideright", "slideup", "slidedown")), filt
    output = Path(scratch) / "xfade.mp4"
    run_ffmpeg(args + ["-filter_complex", filt, "-map", "[vout]", "-an", "-c:v", "libx264", str(output)])
    assert output.stat().st_size > 1000
    # Opening video and images share one MP3 timeline. A noncentral focus
    # should also exercise zoompan's x/y expressions and long-scene cycling.
    intro = Path(scratch) / "opening.mp4"
    audio = Path(scratch) / "narration.wav"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=640x360:r=25:d=1",
                "-c:v", "libx264", str(intro)])
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                "-c:a", "pcm_s16le", str(audio)])
    image_dir = Path(scratch) / "images"
    image_dir.mkdir()
    scenes = [{"index": 0, "duration": 1.0}]
    for i in (1, 2):
        Image.new("RGB", (800, 450), (i * 70, 70, 120)).save(image_dir / f"{i:04d}.png")
        scenes.append({"index": i, "duration": 1.5, "focus": (0.85, 0.15),
                       "zoom_seconds": 6, "zoom": True})
    result = Path(scratch) / "intro_and_images.mp4"
    ns["assemble_video"](scenes, str(image_dir), {0: str(intro)}, str(audio),
                         scratch, str(result), lambda *args: None, motion=True)
    duration = float(subprocess.check_output(
        [ffmpeg.replace("ffmpeg.exe", "ffprobe.exe") if ffmpeg.endswith("ffmpeg.exe") else "ffprobe",
         "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(result)],
        text=True).strip())
    assert 3.9 <= duration <= 4.1, duration
    source = (root / "manual_images.py").read_text(encoding="utf-8")
    manual_tree = ast.parse(source)
    effect = next(node for node in manual_tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "apply_film_effect")
    mn = {"os": os, "random": random, "shutil": shutil, "Image": Image,
          "ImageDraw": ImageDraw,
          "pipeline": SimpleNamespace(W=640, H=360, FPS=25, run_ffmpeg=run_ffmpeg,
                                      check_cancel=lambda: None,
                                      video_encoder_args=ns["video_encoder_args"])}
    exec(compile(ast.Module(body=[effect], type_ignores=[]), "manual_images.py", "exec"), mn)
    mn["apply_film_effect"](str(result), "retro", scratch)
    assert result.stat().st_size > 1000
    film_probe = subprocess.check_output(
        [ffmpeg.replace("ffmpeg.exe", "ffprobe.exe") if ffmpeg.endswith("ffmpeg.exe") else "ffprobe",
         "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(result)], text=True)
    import json
    film_data = json.loads(film_probe)
    assert abs(float(film_data["format"]["duration"]) - duration) < 0.15
    assert any(stream["codec_type"] == "audio" for stream in film_data["streams"])
    print("Native render and requested transitions passed")

