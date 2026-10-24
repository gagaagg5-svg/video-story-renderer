"""Smoke-test the manual path without Windows UI or paid API calls."""
import importlib.util
import json
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType

from PIL import Image


def main():
    root = Path(tempfile.mkdtemp(prefix="kadro_test_"))
    source = root / "source"
    source.mkdir()
    for name, color in (("10.jpg", "red"), ("2.png", "green"), ("1.webp", "blue")):
        Image.new("RGB", (20, 12), color).save(source / name)
    audio = root / "voice.mp3"
    audio.write_bytes(b"test voice")

    fake = ModuleType("pipeline")
    fake.CANCEL = threading.Event()
    fake.data_dir = lambda: str(root)
    fake.short_base = lambda s, keep=25: s[:keep]
    fake.audio_duration = lambda path: 9.0
    fake.Progress = lambda callback: lambda *args: callback(*args) if callback else None
    fake.check_cancel = lambda: None
    fake.read_json = lambda path: json.loads(Path(path).read_text()) if Path(path).exists() else None
    fake.write_json = lambda path, data: Path(path).write_text(json.dumps(data))
    fake.cleanup_job = lambda job: None
    fake.log = lambda message: None
    calls = []

    def assemble(scenes, image_dir, videos, audio_path, job_dir, output, progress, motion=False):
        assert len(scenes) == 3
        calls.append(([s["duration"] for s in scenes], [s["start"] for s in scenes]))
        colors = [Image.open(Path(image_dir) / f"{i:04d}.png").getpixel((1, 1)) for i in range(3)]
        calls.append((colors, motion))
        Path(output).write_bytes(b"video")

    fake.assemble_video = assemble
    sys.modules["pipeline"] = fake
    # Test the manual UI/timing contract independently of the graphics driver.
    gpu = ModuleType("gpu_render")
    film_options = []
    overlay_options = []
    def render(scenes, image_dir, videos, audio, work_dir, output, progress, motion=False, film="none", film_intensity=50, overlay=None):
        film_options.append((film, film_intensity))
        overlay_options.append(overlay)
        assemble(scenes, image_dir, videos, audio, work_dir, output, progress, motion)
    gpu.render = render
    sys.modules["gpu_render"] = gpu
    module_path = Path(__file__).resolve().parents[1] / "manual_images.py"
    spec = importlib.util.spec_from_file_location("manual_images", module_path)
    manual = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manual)

    assert [Path(p).name for p in manual.list_images(str(source))] == ["1.webp", "2.png", "10.jpg"]
    result = manual.make_video(str(audio), str(source), motion=True)
    assert Path(result).read_bytes() == b"video"
    assert calls[0] == ([3.0, 3.0, 3.0], [0.0, 3.0, 6.0])
    assert calls[1][1] is True
    blue, green, red = calls[1][0]
    assert blue[2] >= 245 and blue[0] <= 10
    assert green[1] >= 120 and green[0] <= 10
    assert red[0] >= 245 and red[1] <= 10  # JPEG/WebP may shift color values.
    imported = Path(manual.job_dir(str(audio))) / "images" / "0000.png"
    first_time = imported.stat().st_mtime_ns
    manual.make_video(str(audio), str(source))
    assert imported.stat().st_mtime_ns == first_time
    Image.new("RGB", (20, 12), "yellow").save(source / "1.webp")
    manual.make_video(str(audio), str(source))
    yellow = Image.open(imported).getpixel((1, 1))
    assert yellow[0] >= 245 and yellow[1] >= 245 and yellow[2] <= 10
    images = manual.list_images(str(source))
    csv_path = root / "timing.csv"
    csv_path.write_text("filename,duration_seconds\n10.jpg,2\n1.webp,4\n2.png,3\n", encoding="utf-8")
    ordered, durations = manual.resolve_timing(images, str(csv_path), 9.0)
    assert [Path(p).name for p in ordered] == ["10.jpg", "1.webp", "2.png"]
    assert durations == [2.0, 4.0, 3.0]
    manual.make_video(str(audio), str(source), timing_file=str(csv_path))
    assert calls[-1][0][0][0] >= 245  # timeline order begins with red JPEG
    assert calls[-2] == ([2.0, 4.0, 3.0], [0.0, 2.0, 6.0])
    xml = root / "davinci.xml"
    xml.write_text('''<?xml version="1.0"?><xmeml version="5"><sequence><rate><timebase>30</timebase><ntsc>FALSE</ntsc></rate><media><video><track>
<clipitem><start>0</start><end>60</end><file id="a"><name>10.jpg</name></file></clipitem>
<clipitem><start>60</start><end>180</end><file id="b"><name>1.webp</name></file></clipitem>
<clipitem><start>180</start><end>270</end><file id="c"><name>2.png</name></file></clipitem>
</track></video></media></sequence></xmeml>''', encoding="utf-8")
    assert manual.resolve_timing(images, str(xml), 9.0) == (ordered, durations)
    # Full timeline: opening video occupies 00:00–00:02 and images follow it.
    intro = root / "intro.mp4"
    intro.write_bytes(b"intro")
    manual.intro_duration = lambda path: 2.0 if path else 0.0
    full_csv = root / "full_timing.csv"
    full_csv.write_text("filename,duration_seconds\nintro.mp4,2\n10.jpg,2\n1.webp,2\n2.png,3\n",
                        encoding="utf-8")
    image_order, image_durations, opening = manual.resolve_full_timing(
        images, str(full_csv), 9.0, str(intro))
    assert image_order == ordered and image_durations == [2.0, 2.0, 3.0] and opening == 2.0
    full_xml = root / "full_timeline.xml"
    full_xml.write_text('''<?xml version="1.0"?><xmeml version="5"><sequence><rate><timebase>30</timebase><ntsc>FALSE</ntsc></rate><media><video><track>
<clipitem><start>0</start><end>60</end><file id="v"><name>intro.mp4</name></file></clipitem>
<clipitem><start>60</start><end>120</end><file id="a"><name>10.jpg</name></file></clipitem>
<clipitem><start>120</start><end>180</end><file id="b"><name>1.webp</name></file></clipitem>
<clipitem><start>180</start><end>270</end><file id="c"><name>2.png</name></file></clipitem>
</track></video></media></sequence></xmeml>''', encoding="utf-8")
    assert manual.resolve_full_timing(images, str(full_xml), 9.0, str(intro)) == (
        ordered, [2.0, 2.0, 3.0], 2.0)
    full_csv.write_text(full_csv.read_text(encoding="utf-8").replace("intro.mp4,2", "intro.mp4,3"),
                        encoding="utf-8")
    try:
        manual.resolve_full_timing(images, str(full_csv), 9.0, str(intro))
        raise AssertionError("Full-timeline mismatch was accepted")
    except ValueError as exc:
        assert "озвучка" in str(exc)
    # A stale partially rendered clip must be dropped if the timing changes.
    clip_dir = Path(manual.job_dir(str(audio))) / "clips"
    clip_dir.mkdir(exist_ok=True)
    (clip_dir / "0000.mp4").write_bytes(b"old timing")
    csv_path.write_text("filename;duration_seconds\n10.jpg;3\n1.webp;3\n2.png;3\n", encoding="utf-8")
    manual.make_video(str(audio), str(source), timing_file=str(csv_path))
    assert not (clip_dir / "0000.mp4").exists()
    csv_path.write_text("filename,duration_seconds\n10.jpg,2\n1.webp,3\n2.png,3\n", encoding="utf-8")
    try:
        manual.resolve_timing(images, str(csv_path), 9.0)
        raise AssertionError("Audio mismatch was accepted")
    except ValueError as exc:
        assert "озвучка" in str(exc)
    xml.write_text(xml.read_text(encoding="utf-8").replace("<start>60</start>", "<start>61</start>"), encoding="utf-8")
    try:
        manual.resolve_timing(images, str(xml), 9.0)
        raise AssertionError("Timeline gap was accepted")
    except ValueError as exc:
        assert "разрыв" in str(exc)
    # A long narration with only a few images must have a genuinely short
    # preview that still visits every image, without touching the full job.
    fake.audio_duration = lambda path: 3000.0
    quick = manual.make_video(str(audio), str(source), motion=True, preview=True)
    assert quick.endswith("_preview.mp4")
    assert calls[-2] == ([3.0, 3.0, 3.0], [0.0, 3.0, 6.0])
    assert Path(quick).is_file()
    assert Path(manual.job_dir(str(audio)) + "_preview").is_dir()
    manual.make_video(str(audio), str(source), preview=True, film_effect="light", film_intensity=75)
    assert film_options[-1] == ("light", 75)
    for bad in (-1, 101, float("nan"), None):
        try:
            manual.make_video(str(audio), str(source), preview=True, film_intensity=bad)
            raise AssertionError("Invalid film intensity accepted")
        except ValueError:
            pass
    effect = {"path": str(intro), "opacity": .35, "speed": .5}
    manual.make_video(str(audio), str(source), preview=True, overlay=effect)
    assert overlay_options[-1] == effect
    for bad in ({**effect, "speed": 0}, {**effect, "opacity": 2}, {**effect, "speed": None},
                {**effect, "opacity": float("nan")}, {**effect, "path": str(root / "missing.mp4")}, "bad"):
        try:
            manual.validate_overlay(bad)
            raise AssertionError("Invalid overlay accepted")
        except ValueError:
            pass
    # A missing graphics context must retain the existing FFmpeg path.
    def unavailable(*args, **kwargs):
        raise RuntimeError("No OpenGL driver")
    gpu.render = unavailable
    fallback = manual.make_video(str(audio), str(source), preview=True)
    assert Path(fallback).read_bytes() == b"video"
    metrics = json.loads((Path(manual.job_dir(str(audio)) + "_preview") / "render_metrics.json").read_text())
    assert metrics["backend"] == "ffmpeg-fallback"
    applied = []
    manual.apply_video_overlay = lambda target, settings: applied.append(settings)
    manual.make_video(str(audio), str(source), preview=True, overlay=effect)
    assert applied == [effect]
    print("Manual-image smoke test passed")


if __name__ == "__main__":
    main()



