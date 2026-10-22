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
    calls = []

    def assemble(scenes, image_dir, videos, audio_path, job_dir, output, progress, motion=False):
        assert len(scenes) == 3
        calls.append(([s["duration"] for s in scenes], [s["start"] for s in scenes]))
        colors = [Image.open(Path(image_dir) / f"{i:04d}.png").getpixel((1, 1)) for i in range(3)]
        calls.append((colors, motion))
        Path(output).write_bytes(b"video")

    fake.assemble_video = assemble
    sys.modules["pipeline"] = fake
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
    print("Manual-image smoke test passed")


if __name__ == "__main__":
    main()
