"""Exercise the compiled application locally without any paid API calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback


def run(report_path):
    import pipeline
    import manual_images
    import fastgen_api
    from PIL import Image

    report = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
    try:
        resources = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        for item in ("ui.html", "fonts/IntroDemo-BlackCAPS.otf", "docs/instruction.html"):
            if not (resources / item).is_file():
                raise RuntimeError(f"Missing bundled resource: {item}")
        with tempfile.TemporaryDirectory(prefix="kadrogen_exe_test_") as tmp:
            root = Path(tmp)
            old_data_dir = fastgen_api._DATA_DIR
            fastgen_api._DATA_DIR = str(root)
            try:
                images = root / "images"
                images.mkdir()
                for i, color in enumerate(("red", "blue"), 1):
                    Image.new("RGB", (320, 180), color).save(images / f"{i:03d}.png")
                audio = root / "voice.wav"
                subprocess.run([pipeline.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i",
                                "sine=frequency=440:duration=3", str(audio)], check=True,
                               creationflags=pipeline.NO_WINDOW, timeout=30)
                timing = root / "timing.csv"
                timing.write_text("filename,duration_seconds\n002.png,1\n001.png,2\n", encoding="utf-8")
                output = manual_images.make_video(str(audio), str(images), timing_file=str(timing))
                info = subprocess.run([pipeline.ffmpeg_bin("ffprobe"), "-v", "error", "-show_entries",
                                       "format=duration:stream=codec_type,width,height", "-of", "json", output],
                                      check=True, capture_output=True, text=True,
                                      creationflags=pipeline.NO_WINDOW, timeout=30)
                data = json.loads(info.stdout)
                duration = float(data["format"]["duration"])
                assert abs(duration - 3.0) < 0.2, data
                assert any(s.get("width") == 1920 and s.get("height") == 1080 for s in data["streams"]), data
                assert any(s.get("codec_type") == "audio" for s in data["streams"]), data
                report.update(ok=True, duration_seconds=duration, checks=["bundled resources", "CSV ordering", "FFmpeg", "Full HD video with audio"])
            finally:
                fastgen_api._DATA_DIR = old_data_dir
    except Exception:
        report["error"] = traceback.format_exc()
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
