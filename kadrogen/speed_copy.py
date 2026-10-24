"""Create an accelerated copy of a finished video, preserving audio pitch."""
import math
import os
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path


class Cancelled(Exception):
    pass


def validate_speed(value):
    try:
        speed = float(value)
    except (TypeError, ValueError):
        raise ValueError("Скорость должна быть числом от 1,01 до 2.") from None
    if not math.isfinite(speed) or not 1.01 <= speed <= 2:
        raise ValueError("Скорость должна быть от 1,01× до 2×.")
    return speed


def create(source, speed, ffmpeg, encoder_args, duration, progress=None, cancel=None, no_window=0):
    speed = validate_speed(speed)
    source = Path(source)
    if not source.is_file():
        raise ValueError("Готовое видео не найдено.")
    cancel = cancel or threading.Event()
    progress = progress or (lambda value: None)
    label = f"{speed:.2f}".rstrip("0").rstrip(".")
    stem = source.with_name(source.stem + f"_speed_{label}x")
    output = Path(str(stem) + ".mp4")
    # Exclusive reservation: repeated clicks never replace an earlier copy.
    index = 2
    while True:
        try:
            with output.open("xb"):
                pass
            break
        except FileExistsError:
            output = Path(str(stem) + f"_{index}.mp4")
            index += 1
    tmp = str(output) + "." + uuid.uuid4().hex[:8] + ".part.mp4"
    process = None
    done = threading.Event()
    watcher = None
    success = False
    try:
        if cancel.is_set():
            raise Cancelled()
        with tempfile.TemporaryFile() as log:
            args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
                    "-filter_complex", f"[0:v:0]setpts=(PTS-STARTPTS)/{speed:.8f}[v]",
                    "-map", "[v]", "-map", "0:a:0?", "-af", f"atempo={speed:.8f},asetpts=PTS-STARTPTS",
                    *encoder_args, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    "-progress", "pipe:1", "-nostats", tmp]
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=log, text=True,
                                       creationflags=no_window)
            def watch():
                while not done.wait(.1):
                    if cancel.is_set():
                        if process.poll() is None:
                            process.kill()
                        return
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            for line in process.stdout:
                if line.startswith("out_time_us="):
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000
                        progress(min(99, max(0, round(seconds / max(.01, duration / speed) * 100))))
                    except ValueError:
                        pass
            code = process.wait()
            if cancel.is_set():
                raise Cancelled()
            if code:
                log.seek(0)
                raise RuntimeError("Не удалось создать ускоренную копию: " + log.read().decode(errors="replace")[-900:])
        os.replace(tmp, output)
        success = True
        progress(100)
        return str(output)
    finally:
        done.set()
        if process:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
        if watcher:
            watcher.join(timeout=2)
        if os.path.exists(tmp):
            os.remove(tmp)
        if not success:
            output.unlink(missing_ok=True)
