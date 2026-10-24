"""Exercise real GLSL composition and encoded output (Mesa EGL in Linux CI).

No NVIDIA performance claim: software OpenGL is allowed only by this test.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import ModuleType

from PIL import Image, ImageDraw, ImageOps, ImageChops

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = str(ROOT / "ffmpeg" / "ffmpeg.exe") if (ROOT / "ffmpeg" / "ffmpeg.exe").exists() else "ffmpeg"
FFPROBE = FFMPEG.replace("ffmpeg.exe", "ffprobe.exe") if FFMPEG.endswith("ffmpeg.exe") else "ffprobe"
fake = ModuleType("pipeline")
fake.W, fake.H, fake.FPS = 320, 180, 25
fake.NO_WINDOW = 0
fake.CANCEL = threading.Event()
class Cancelled(Exception):
    pass
def check_cancel():
    if fake.CANCEL.is_set():
        raise Cancelled()
fake.check_cancel = check_cancel
fake.ffmpeg_bin = lambda: FFMPEG
fake.video_encoder_args = lambda: ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
fake._fit_16_9 = lambda image: ImageOps.fit(image, (640, 360))
fake.write_json = lambda path, value: Path(path).write_text(json.dumps(value))
sys.modules["pipeline"] = fake
spec = importlib.util.spec_from_file_location("gpu_render", ROOT / "gpu_render.py")
gpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu)


def ff(args):
    return subprocess.check_output([FFMPEG, "-y", "-v", "error", *map(str, args)])


def run():
    # Fractional CSV durations must not accumulate rounding drift.
    scenes = [{"duration": 1.013} for _ in range(100)]
    boundaries, overlaps = gpu.frame_plan(scenes, 25, True)
    assert boundaries[-1] == round(101.3 * 25)
    assert all(a < b for a, b in zip(boundaries, boundaries[1:]))
    assert gpu.crop_for({"zoom": False}, 2, 30, 25) == (0, 0, 1)
    crop = gpu.crop_for({"zoom": True, "focus": (1, 0)}, 49, 50, 25)
    assert crop[0] > 0 and crop[1] == 0 and abs(crop[2] - 1 / 1.12) < 1e-6
    try:
        composer = gpu.Composer(320, 180, allow_software=True)
    except Exception as exc:
        if "--require-gl" in sys.argv:
            raise
        print(f"OpenGL integration skipped (no graphics driver): {exc}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        images = root / "images"
        images.mkdir()
        picture = Image.new("RGB", (640, 360), "blue")
        ImageDraw.Draw(picture).rectangle((0, 0, 319, 179), fill="red")
        picture.save(images / "0000.png")
        Image.new("RGB", (640, 360), "green").save(images / "0001.png")
        a = composer.image(0, str(images / "0000.png"))
        b = composer.image(1, str(images / "0001.png"))
        raw = composer.draw(a, b, (0, 0, 1), (0, 0, 1), 0, 0, 0)
        output = Image.frombytes("RGB", (320, 180), bytes(raw))
        assert output.getpixel((20, 20))[0] > 240, "Top-left orientation was flipped"
        assert output.getpixel((20, 160))[2] > 240
        for effect in range(5):
            for fraction in (0, 0.5, 1):
                raw = composer.draw(a, b, (0, 0, 1), (0, 0, 1), fraction, effect, 0)
                output = Image.frombytes("RGB", (320, 180), bytes(raw))
                assert min(sum(pixel) for pixel in output.getdata()) >= 120, "Black transition edge"
                if fraction == 1:
                    assert output.getpixel((160, 90))[1] >= 120
        # Verify slide direction, not only that all shaders compile.
        output = Image.frombytes("RGB", (320, 180), bytes(composer.draw(a, b, (0, 0, 1), (0, 0, 1), .5, 1, 0)))
        assert output.getpixel((300, 90))[1] >= 120
        assert output.getpixel((20, 90))[2] > 240
        renderer = composer.renderer
        composer.close()
        # Dust must change pixels gently and repeat without a jump at 8 seconds.
        film = gpu.Composer(320, 180, film="light", allow_software=True)
        texture = film.image(0, str(images / "0000.png"))
        first = bytes(film.draw(texture, texture, (0, 0, 1), (0, 0, 1), 0, 0, 0))
        later = bytes(film.draw(texture, texture, (0, 0, 1), (0, 0, 1), 0, 0, 8))
        assert first == later
        film.film_gain = 0
        plain = bytes(film.draw(texture, texture, (0, 0, 1), (0, 0, 1), 0, 0, 0))
        assert first != plain
        film.close()
        audio, intro = root / "voice.wav", root / "intro.mp4"
        ff(["-f", "lavfi", "-i", "sine=frequency=440:duration=10", audio])
        ff(["-f", "lavfi", "-i", "color=c=yellow:s=320x180:r=25:d=0.4", "-c:v", "libx264", intro])
        scenes = []
        for i, color in enumerate(("yellow", "red", "blue", "green", "white", "purple")):
            Image.new("RGB", (640, 360), color).save(images / f"{i:04d}.png")
            scenes.append({"index": i, "duration": 0.8, "zoom": i != 0,
                           "focus": (0.8, 0.2), "zoom_seconds": 6})
        result = root / "result.mp4"
        metrics = gpu.render(scenes, str(images), {0: str(intro)}, str(audio), tmp, str(result),
                             lambda *args: None, motion=True, film="retro", allow_software=True)
        probe = json.loads(subprocess.check_output([FFPROBE, "-v", "error", "-show_streams", "-show_format",
                                                   "-of", "json", str(result)]))
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        assert int(video["nb_frames"]) == 120, video
        assert (video["width"], video["height"]) == (320, 180)
        assert abs(float(probe["format"]["duration"]) - 4.8) < .08
        assert any(s["codec_type"] == "audio" for s in probe["streams"])
        decoded = ff(["-i", result, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
        assert len(decoded) == 120 * 320 * 180 * 3
        # Last frame remains the final image, including all five transitions.
        final = Image.frombytes("RGB", (320, 180), decoded[-320 * 180 * 3:])
        r, g, b = final.getpixel((160, 90))
        assert r > 100 and b > 100 and g < 30
        # Short single scene and no effects; no transition or next image required.
        single = root / "single.mp4"
        gpu.render([{"index": 1, "duration": .12}], str(images), {}, str(audio), tmp, str(single),
                   lambda *args: None, allow_software=True)
        # Cancellation preserves an existing completed video and removes partial output.
        protected = root / "protected.mp4"
        protected.write_bytes(b"previous completed export")
        def cancel(stage, done, total, message):
            if done:
                fake.CANCEL.set()
        try:
            gpu.render(scenes, str(images), {}, str(audio), tmp, str(protected), cancel,
                       allow_software=True)
            raise AssertionError("Cancellation ignored")
        except Cancelled:
            pass
        finally:
            fake.CANCEL.clear()
        assert protected.read_bytes() == b"previous completed export"
        assert not Path(str(protected) + ".gpu.tmp.mp4").exists()
        # Black is neutral; screen overlay stays in screen coordinates and loops
        # at half speed across a scene boundary (source clip is only 0.4 sec).
        effect = root / "overlay.mp4"
        ff(["-f", "lavfi", "-i", "color=black:s=320x180:r=25:d=0.4",
            "-vf", "drawbox=x=160:y=0:w=160:h=90:color=white:t=fill:enable='lt(t,0.2)',"
                   "drawbox=x=160:y=90:w=160:h=90:color=red:t=fill:enable='gte(t,0.2)'",
            "-c:v", "libx264", "-crf", "10", effect])
        for index in (0, 1):
            Image.new("RGB", (640, 360), (40, 80, 120)).save(images / f"{index:04d}.png")
        overlay_result = root / "with_overlay.mp4"
        settings = {"path": str(effect), "opacity": .5, "speed": .5}
        gpu.render([{"index": 0, "duration": .6}, {"index": 1, "duration": .6}],
                   str(images), {}, str(audio), tmp, str(overlay_result), lambda *args: None,
                   overlay=settings, allow_software=True)
        frames = ff(["-i", overlay_result, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
        assert len(frames) == 30 * 320 * 180 * 3
        def sample_at(index):
            start = index * 320 * 180 * 3
            return Image.frombytes("RGB", (320, 180), frames[start:start + 320 * 180 * 3])
        for index in (3, 23):  # 0.12 and 0.92 sec must both show the first phase.
            picture = sample_at(index)
            assert all(abs(a-b) < 9 for a,b in zip(picture.getpixel((30,30)), (40,80,120)))
            assert picture.getpixel((250,30))[0] > 130
            assert picture.getpixel((250,140))[0] < 55
        picture = sample_at(13)
        assert picture.getpixel((250,30))[0] < 55
        assert picture.getpixel((250,140))[0] > 130
        assert picture.getpixel((250,140))[2] < 135
        # Overlay pipe must be terminated on cancellation as well.
        try:
            gpu.render(scenes, str(images), {}, str(audio), tmp, str(protected), cancel,
                       overlay=settings, allow_software=True)
            raise AssertionError("Overlay cancellation ignored")
        except Cancelled:
            pass
        finally:
            fake.CANCEL.clear()
        assert protected.read_bytes() == b"previous completed export"
        # Regression: tiny low-alpha dots used to vanish after H.264 compression.
        # Test the actual encoded Full HD output, not just a non-empty GL buffer.
        fake.W, fake.H = 1920, 1080
        Image.new("RGB", (640, 360), (110, 100, 90)).save(images / "0000.png")
        still_scene = [{"index": 0, "duration": .8, "zoom": False}]
        samples = []
        for intensity in (0, 50, 100):
            export = root / f"visibility_{intensity}.mp4"
            gpu.render(still_scene, str(images), {}, str(audio), tmp, str(export), lambda *args: None,
                       film="light", film_intensity=intensity, allow_software=True)
            frame = ff(["-i", export, "-ss", "0.4", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
            samples.append(Image.frombytes("RGB", (1920, 1080), frame))
        def visible_pixels(image):
            hist = ImageChops.difference(samples[0], image).convert("L").histogram()
            return sum(hist[12:])
        mild, strong = visible_pixels(samples[1]), visible_pixels(samples[2])
        assert 300 < mild < 20000, (mild, strong)  # visible, still sparse; no full-frame noise
        assert strong > mild, (mild, strong)
        print(json.dumps({"film_visible_pixels_50": mild, "film_visible_pixels_100": strong}))
        print(json.dumps({"ok": True, "graphics": renderer, "metrics": metrics}))


if __name__ == "__main__":
    run()
