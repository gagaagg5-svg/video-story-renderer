"""Reimport with a locked old PNG; cancellation must not publish partial batches.

On Windows a real CreateFile handle denies delete/write sharing, reproducing
WinError 32 from Explorer/gallery readers. No paid APIs or graphics driver needed.
"""
import ast
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
fake = ModuleType("pipeline")
fake.read_json = lambda p: json.loads(Path(p).read_text()) if Path(p).exists() else None
fake.write_json = lambda p, value: Path(p).write_text(json.dumps(value))
fake.check_cancel = lambda: None
fake.REDO_LOCK = threading.Lock()
fake.REDO = set()
fake.LAST_JOB_DIR = None
sys.modules["pipeline"] = fake
import manual_images as m


class Cancelled(Exception):
    pass


def deny_writes(path):
    if os.name != "nt":
        return open(path, "rb"), lambda f: f.close()
    from ctypes import wintypes
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    dll.CreateFileW.restype = wintypes.HANDLE
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = dll.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    try:
        path.unlink()
    except PermissionError as exc:
        assert exc.winerror == 32, exc
    else:
        raise AssertionError("Windows lock was not established")
    return handle, dll.CloseHandle


# Exercise the actual UI methods without importing/starting the Windows window.
tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
thumb = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "make_thumb")
api = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Api")
gallery = next(n for n in api.body if isinstance(n, ast.FunctionDef) and n.name == "gallery")
namespace = dict(os=os, THUMBS={}, LOCK=threading.Lock(), STATE={}, pipeline=fake, manual_images=m)
exec(compile(ast.Module(body=[thumb, gallery], type_ignores=[]), "app.py", "exec"), namespace)

with tempfile.TemporaryDirectory(prefix="kadro_locks_") as tmp:
    root = Path(tmp)
    source = root / "Исходные кадры"
    source.mkdir()
    paths = []
    for i, color in enumerate(("red", "green", "blue")):
        path = source / f"{i}.png"
        Image.new("RGB", (32, 18), color).save(path)
        paths.append(str(path))
    work = root / "manual_test"
    no_progress = lambda *args: None
    old = Path(m._import_images(paths, str(work), no_progress))
    original_bytes = (old / "0000.png").read_bytes()
    handle, close = deny_writes(old / "0000.png")
    try:
        # An unchanged import reuses the batch without rewriting locked files.
        assert m._import_images(paths, str(work), no_progress) == str(old)
        Image.new("RGB", (32, 18), "yellow").save(paths[0])
        new = Path(m._import_images(paths, str(work), no_progress))
        assert new != old
        assert (old / "0000.png").read_bytes() == original_bytes
        with Image.open(new / "0000.png") as frame:
            assert frame.getpixel((0, 0)) == (255, 255, 0)
        assert m.prepared_image_dir(str(work)) == str(new)

        # Gallery never reads the previous batch during import, even on failure.
        namespace["STATE"].update(job_dir=str(work), scenes=3, running=True, stage="images")
        assert namespace["gallery"](None, []) == {"total": 3, "items": []}
        namespace["STATE"]["running"] = False
        assert namespace["gallery"](None, [])["items"] == []
        namespace["STATE"]["stage"] = "render"
        items = namespace["gallery"](None, [])["items"]
        assert len(items) == 3 and items[0]["img"].startswith("data:image/jpeg;base64,")
        assert namespace["gallery"](None, [0, 2, 4])["items"] == []
        # Thumbnail source handles close explicitly, even if image decode fails.
        opened = []
        real_open = open
        def tracking_open(*args, **kwargs):
            stream = real_open(*args, **kwargs)
            opened.append(stream)
            return stream
        namespace["THUMBS"].clear()
        with patch("builtins.open", tracking_open):
            assert namespace["make_thumb"](str(new / "0000.png"))
        assert opened and all(f.closed for f in opened)

        manifest = work / "manual_images.json"
        published = manifest.read_bytes()
        Image.new("RGB", (32, 18), "purple").save(paths[1])
        def cancel_after_first(stage, done, total, message):
            if done == 1:
                raise Cancelled()
        try:
            m._import_images(paths, str(work), cancel_after_first)
            raise AssertionError("cancel ignored")
        except Cancelled:
            pass
        assert manifest.read_bytes() == published
        recovered = m._import_images(paths, str(work), no_progress)
        assert recovered != str(new)
        assert len(list(Path(recovered).glob("*.png"))) == 3

        # Missing cache frames and source reordering must create fresh batches.
        (Path(recovered) / "0002.png").unlink()
        repaired = m._import_images(paths, str(work), no_progress)
        assert repaired != recovered
        reordered = m._import_images(list(reversed(paths)), str(work), no_progress)
        with Image.open(Path(reordered) / "0000.png") as frame:
            assert frame.getpixel((0, 0)) == (0, 0, 255)

        # Legacy 1.7.5 files may remain locked; migration must never touch them.
        legacy = root / "manual_legacy"
        (legacy / "images").mkdir(parents=True)
        legacy_frame = legacy / "images" / "0030.png"
        legacy_frame.write_bytes(original_bytes)
        legacy_handle, legacy_close = deny_writes(legacy_frame)
        try:
            fake.write_json(str(legacy / "manual_images.json"), m._source_stamp(paths))
            migrated = m._import_images(paths, str(legacy), no_progress)
            assert Path(migrated).name.startswith("images_")
            assert legacy_frame.read_bytes() == original_bytes
        finally:
            legacy_close(legacy_handle)

        # A source changed mid-import cannot be committed as a valid cache.
        published = manifest.read_bytes()
        Image.new("RGB", (32, 18), "orange").save(paths[0])
        def change_source(stage, done, total, message):
            if done == 1:
                Image.new("RGB", (33, 18), "black").save(paths[0])
        try:
            m._import_images(paths, str(work), change_source)
            raise AssertionError("changed source was published")
        except ValueError as exc:
            assert "изменились" in str(exc)
        assert manifest.read_bytes() == published
    finally:
        close(handle)

print("PASS: locked old/legacy PNG, cache reuse, new batch, gallery, handle release, cancellation, retry, reorder")
