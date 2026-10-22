"""End-to-end probe of FFmpeg zoom/pan and the three selected xfade effects."""
import ast
from pathlib import Path
import os
import subprocess
import tempfile
import threading

from PIL import Image

root = Path(__file__).resolve().parents[1]
source = (root / "pipeline.py").read_text(encoding="utf-8")
ffmpeg = root / "ffmpeg" / "ffmpeg.exe"
ffmpeg = str(ffmpeg) if ffmpeg.is_file() else "ffmpeg"
tree = ast.parse(source)
functions = {"ffmpeg_bin", "video_encoder_args", "_fit_16_9", "render_scene_clip", "xfade_chain"}
nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in functions]
ns = {"os": os, "subprocess": subprocess, "threading": threading, "NO_WINDOW": 0,
      "W": 640, "H": 360, "FPS": 25, "EDGE_CROP": 0.035, "TRANSITION": 0.5,
      "TRANSITIONS": ["zoomin", "slideleft", "slideright"],
      "_NVENC_LOCK": threading.Lock(), "_NVENC_AVAILABLE": None,
      "app_dir": lambda: str(root), "log": lambda message: print(message)}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "pipeline.py", "exec"), ns)

def run_ffmpeg(args):
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", *args], check=True)

ns["run_ffmpeg"] = run_ffmpeg
with tempfile.TemporaryDirectory() as scratch:
    clips = []
    for i in range(4):
        image = Path(scratch) / f"image{i}.png"
        Image.new("RGB", (800, 450), (60 * i, 100, 150)).save(image)
        clip = Path(scratch) / f"clip{i}.mp4"
        ns["render_scene_clip"](str(image), i, 1.3, str(clip))
        clips.append(str(clip))
    args, filt = ns["xfade_chain"](clips, [0.8] * 4)
    assert all(effect in filt for effect in ("zoomin", "slideleft", "slideright")), filt
    output = Path(scratch) / "xfade.mp4"
    run_ffmpeg(args + ["-filter_complex", filt, "-map", "[vout]", "-an", "-c:v", "libx264", str(output)])
    assert output.stat().st_size > 1000
    print("Native render and requested transitions passed")
