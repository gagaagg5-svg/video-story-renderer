"""Assemble a voice-over with a user-selected folder of still images.

This path never calls an image generator. It deliberately keeps its job cache
separate from Kadro.Gen's scene-generation jobs.
"""
import hashlib
import base64
import csv
import io
import math
import os
import random
import re
import shutil
import time
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

import pipeline


EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}


def _read_csv_timing(file_path):
    with open(file_path, encoding="utf-8-sig", newline="") as f:
        sample = f.read()
    try:
        dialect = csv.Sniffer().sniff(sample[:4096], delimiters=",;\t")
        rows = list(csv.reader(io.StringIO(sample), dialect))
    except csv.Error as exc:
        raise ValueError(f"Не удалось прочитать CSV: {exc}") from exc
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("CSV с таймингом пуст.")
    header = [c.strip().casefold() for c in rows[0]]
    duration_col = next((header.index(x) for x in ("duration_seconds", "duration", "seconds", "длительность") if x in header), None)
    name_col = next((header.index(x) for x in ("filename", "image", "file", "кадр") if x in header), None)
    if duration_col is None:
        if all(len(r) == 1 for r in rows):
            return [(None, r[0].strip()) for r in rows]
        raise ValueError("CSV: нужен столбец duration_seconds (секунды), необязательно filename.")
    result = []
    for line, row in enumerate(rows[1:], 2):
        if duration_col >= len(row) or not row[duration_col].strip():
            raise ValueError(f"CSV, строка {line}: пустая длительность.")
        name = row[name_col].strip() if name_col is not None and name_col < len(row) else None
        if name_col is not None and not name:
            raise ValueError(f"CSV, строка {line}: пустое имя кадра.")
        result.append((name, row[duration_col].strip()))
    return result


def _read_xml_timing(file_path, include_video=False):
    try:
        root = ET.parse(file_path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"XML не читается: {exc}") from exc
    if root.tag != "xmeml":
        raise ValueError("Поддерживается DaVinci XML формата Final Cut Pro 7 (Export Timeline → FCP 7 XML).")
    sequence = root.find(".//sequence")
    if sequence is None:
        raise ValueError("В XML не найден таймлайн sequence.")
    rate = sequence.find("rate")
    if rate is None or not rate.findtext("timebase"):
        raise ValueError("В XML не указана частота кадров таймлайна.")
    try:
        fps = int(rate.findtext("timebase"))
        if fps <= 0:
            raise ValueError()
    except ValueError:
        raise ValueError("Некорректная частота кадров в XML.") from None
    if (rate.findtext("ntsc") or "").upper() == "TRUE":
        fps *= 1000 / 1001
    files = {}
    for node in root.iter("file"):
        if node.get("id") and (node.findtext("name") or node.findtext("pathurl")):
            files[node.get("id")] = node.findtext("name") or node.findtext("pathurl")
    tracks = []
    for track in sequence.findall("./media/video/track"):
        clips = []
        for clip in track.findall("clipitem"):
            name = clip.findtext("file/name") or clip.findtext("file/pathurl")
            file_ref = clip.find("file")
            if not name and file_ref is not None:
                name = files.get(file_ref.get("id"))
            name = name or clip.findtext("name")
            if name and name.startswith("file:"):
                name = unquote(urlparse(name).path)
            accepted = EXTENSIONS | VIDEO_EXTENSIONS if include_video else EXTENSIONS
            if not name or os.path.splitext(name)[1].lower() not in accepted:
                continue
            try:
                start = int(clip.findtext("start"))
                end = int(clip.findtext("end"))
            except (TypeError, ValueError):
                raise ValueError("В XML кадру не заданы start и end на таймлайне.") from None
            clips.append((name, start, end))
        if clips:
            tracks.append(clips)
    if not tracks:
        raise ValueError("В видеодорожках XML нет кадров PNG/JPG/WEBP.")
    return tracks, fps


def resolve_timing(paths, timing_file, audio_duration, intro_duration=0.0, items_override=None):
    """Return images in timeline order with durations; enforce one continuous image per clip."""
    if not timing_file and items_override is None:
        duration = audio_duration / len(paths)
        if duration < 0.3:
            raise ValueError("Изображений слишком много для длины озвучки: на кадр нужно хотя бы 0,3 секунды.")
        return paths, [duration] * len(paths)
    if not os.path.isfile(timing_file):
        raise ValueError("Файл тайминга не найден. Выберите CSV или XML снова.")
    ext = os.path.splitext(timing_file)[1].lower()
    if items_override is not None:
        items = items_override
    elif ext == ".csv":
        raw = _read_csv_timing(timing_file)
        try:
            items = [(name, float(value.replace(",", "."))) for name, value in raw]
        except ValueError:
            raise ValueError("CSV: длительности должны быть числами в секундах, например 3.5.") from None
    elif ext == ".xml":
        tracks, fps = _read_xml_timing(timing_file)
        names = {os.path.basename(p).casefold() for p in paths}
        candidates = [t for t in tracks if len(t) == len(paths) and
                      {os.path.basename(x[0]).casefold() for x in t} == names]
        if len(candidates) != 1:
            raise ValueError("XML: не удалось однозначно найти дорожку со всеми выбранными изображениями. Проверьте имена и количество кадров.")
        clips = sorted(candidates[0], key=lambda x: x[1])
        for i, (_, start, end) in enumerate(clips):
            if start < 0 or (i and start != clips[i - 1][2]):
                raise ValueError("XML: на дорожке есть разрыв или наложение кадров. Нужна непрерывная дорожка изображений.")
            if i == 0 and start != 0 and abs(start / fps - intro_duration) > 1 / fps + 0.01:
                raise ValueError("XML: первый кадр должен начинаться в 00:00 или сразу после вступительного видео.")
        items = [(name, (end - start) / fps) for name, start, end in clips]
    else:
        raise ValueError("Тайминг: поддерживаются только CSV и DaVinci FCP 7 XML.")
    if len(items) != len(paths):
        raise ValueError(f"В тайминге {len(items)} кадров, а в папке {len(paths)}. Количество должно совпадать.")
    by_name = {os.path.basename(p).casefold(): p for p in paths}
    if len(by_name) != len(paths):
        raise ValueError("В папке есть кадры с одинаковым именем без учёта регистра.")
    if any(name is not None for name, _ in items):
        if any(name is None for name, _ in items):
            raise ValueError("Укажите filename для всех строк тайминга или ни для одной.")
        try:
            ordered = [by_name[os.path.basename(name).casefold()] for name, _ in items]
        except KeyError as exc:
            raise ValueError(f"В папке нет кадра из тайминга: {exc.args[0]}.") from None
        if len(set(ordered)) != len(paths):
            raise ValueError("Один кадр в тайминге повторяется; нужен один клип на каждый файл.")
    else:
        ordered = paths
    durations = [duration for _, duration in items]
    if any(not (0.3 <= duration < float("inf")) for duration in durations):
        raise ValueError("Длительность каждого кадра должна быть не меньше 0,3 секунды.")
    total = sum(durations)
    if abs(total - audio_duration) > 0.15:
        raise ValueError(f"Тайминг занимает {total:.2f} с, озвучка — {audio_duration:.2f} с. Подгоните таймлайн под MP3.")
    durations[-1] += audio_duration - total
    return ordered, durations


def resolve_full_timing(paths, timing_file, audio_duration, intro_video=None):
    """Interpret the first video as part of the same 00:00-based timeline."""
    original_intro = intro_duration(intro_video)
    if not timing_file:
        if audio_duration - original_intro < len(paths) * 0.3:
            raise ValueError("После вступительного видео не остаётся времени для кадров.")
        images, durations = resolve_timing(paths, None, audio_duration - original_intro)
        return images, durations, original_intro
    if not os.path.isfile(timing_file):
        raise ValueError("Файл тайминга не найден. Выберите CSV или XML снова.")
    ext = os.path.splitext(timing_file)[1].lower()
    items, timed_intro = None, None
    if ext == ".csv":
        raw = _read_csv_timing(timing_file)
        if len(raw) == len(paths) + 1:
            if not intro_video:
                raise ValueError("В CSV есть строка вступления, но видео не выбрано.")
            name, value = raw.pop(0)
            if name and os.path.basename(name).casefold() != os.path.basename(intro_video).casefold():
                raise ValueError("Первой строкой CSV должно быть выбранное вступительное видео.")
            try:
                timed_intro = float(value.replace(",", "."))
            except ValueError:
                raise ValueError("CSV: длительность вступления должна быть числом в секундах.") from None
            try:
                items = [(n, float(d.replace(",", "."))) for n, d in raw]
            except ValueError:
                raise ValueError("CSV: длительности кадров должны быть числами в секундах.") from None
    elif ext == ".xml" and intro_video:
        tracks, fps = _read_xml_timing(timing_file, include_video=True)
        names = {os.path.basename(p).casefold() for p in paths}
        video_name = os.path.basename(intro_video).casefold()
        candidates = [c for t in tracks
                      for c in [sorted(t, key=lambda x: x[1])]
                      if len(c) == len(paths) + 1
                      and {os.path.basename(x[0]).casefold() for x in c[1:]} == names
                      and os.path.basename(c[0][0]).casefold() == video_name]
        if len(candidates) > 1:
            raise ValueError("XML: найдено несколько дорожек с выбранным вступлением и кадрами.")
        if candidates:
            clips = candidates[0]
            if clips[0][1] != 0 or any(end <= start or
                    (i and start != clips[i - 1][2])
                    for i, (_, start, end) in enumerate(clips)):
                raise ValueError("XML: видео и изображения должны идти подряд, начиная с 00:00.")
            timed_intro = clips[0][2] / fps
            items = [(name, (end - start) / fps) for name, start, end in clips[1:]]
    if ext not in (".xml", ".csv"):
        raise ValueError("Тайминг: поддерживаются только CSV и DaVinci FCP 7 XML.")
    intro_length = timed_intro if timed_intro is not None else original_intro
    if intro_video and (not math.isfinite(intro_length) or intro_length < 0.3):
        raise ValueError("Длительность вступления должна быть не меньше 0,3 секунды.")
    if audio_duration - intro_length < len(paths) * 0.3:
        raise ValueError("Вступление оставляет слишком мало времени для кадров и озвучки.")
    images, durations = resolve_timing(paths, timing_file, audio_duration - intro_length,
                                       intro_length, items_override=items)
    return images, durations, intro_length


def intro_duration(path):
    """A silent introductory video runs under the MP3 from the start."""
    if not path:
        return 0.0
    if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in VIDEO_EXTENSIONS:
        raise ValueError("Для вступления выберите существующий MP4, MOV, MKV или WEBM.")
    try:
        result = pipeline.audio_duration(path)
        video = pipeline.subprocess.run(
            [pipeline.ffmpeg_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True, creationflags=pipeline.NO_WINDOW,
            timeout=15).stdout.strip()
    except (OSError, ValueError, RuntimeError, pipeline.subprocess.CalledProcessError) as exc:
        raise ValueError(f"Не удалось прочитать вступительное видео: {exc}") from exc
    if not math.isfinite(result) or result < 0.3:
        raise ValueError("Вступительное видео должно длиться не менее 0,3 секунды.")
    if video != "video":
        raise ValueError("Во вступительном файле не найдена видеодорожка.")
    return result


def image_preview(folder, index):
    paths = list_images(folder)
    if not isinstance(index, int) or not 0 <= index < len(paths):
        raise ValueError("Кадр не найден.")
    with Image.open(paths[index]) as original:
        image = pipeline._fit_16_9(ImageOps.exif_transpose(original).convert("RGB"))
        image.thumbnail((640, 360))
        data = io.BytesIO()
        image.save(data, format="JPEG", quality=80)
    return {"name": os.path.basename(paths[index]),
            "src": "data:image/jpeg;base64," + base64.b64encode(data.getvalue()).decode("ascii")}


def apply_film_effect(video_path, strength, work_dir, film_intensity=50):
    """Slow translucent film dust and fine fibres, without full-frame digital noise."""
    import math

    if strength not in ("light", "retro"):
        raise ValueError("Неизвестный эффект плёнки.")
    gain = max(0.0, min(100.0, float(film_intensity))) / 50.0
    if gain == 0:
        return
    dust_dir = os.path.join(work_dir, "film_dust")
    shutil.rmtree(dust_dir, ignore_errors=True)
    os.makedirs(dust_dir)
    output = video_path + ".film.tmp.mp4"
    try:
        # Stable particles drift slowly and fade through an eight-second seamless
        # cycle. Regenerating random positions every frame caused harsh flicker.
        rng = random.Random(42319)
        width, height = 960, 540
        cycle_frames = pipeline.FPS * 8
        particles = []
        for index in range(48 if strength == "light" else 76):
            particles.append((rng.uniform(15, width - 15), rng.uniform(25, height - 25),
                              rng.random(), rng.uniform(-9, 9), rng.uniform(12, 28),
                              rng.randint(60, 95) if strength == "light" else rng.randint(85, 125),
                              index % 6 == 0, rng.choice((15, 235, 250)),
                              rng.uniform(2.5, 4.5), rng.uniform(12, 26)))
        for frame in range(cycle_frames):
            pipeline.check_cancel()
            image = Image.new("RGBA", (width, height))
            draw = ImageDraw.Draw(image)
            for x0, y0, offset, dx, dy, opacity, fibre, value, size, length in particles:
                phase = (frame / cycle_frames + offset) % 1.0
                alpha = round(min(255, opacity * gain) * math.sin(math.pi * phase) ** 2)
                x = x0 + dx * (phase - 0.5)
                y = y0 + dy * (phase - 0.5)
                color = (value, value, value, alpha)
                if fibre:
                    draw.line([(x, y), (x + 2, y + length * .3), (x + 1, y + length * .7),
                               (x + 3, y + length)], fill=color, width=1)
                else:
                    draw.ellipse((x, y, x + size, y + size), fill=color)
            image.save(os.path.join(dust_dir, f"dust_{frame:03d}.png"))
        filters = (f"[1:v]format=rgba,scale={pipeline.W}:{pipeline.H}:flags=bilinear[dust];"
                   "[0:v][dust]overlay=shortest=1:format=auto,format=yuv420p[v]")
        pipeline.run_ffmpeg(["-i", video_path, "-stream_loop", "-1", "-framerate", str(pipeline.FPS),
                             "-i", os.path.join(dust_dir, "dust_%03d.png"),
                             "-filter_complex", filters, "-map", "[v]", "-map", "0:a:0",
                             *pipeline.video_encoder_args(), "-c:a", "copy",
                             "-movflags", "+faststart", output])
        pipeline.check_cancel()
        os.replace(output, video_path)
    finally:
        shutil.rmtree(dust_dir, ignore_errors=True)
        if os.path.exists(output):
            os.remove(output)


def validate_overlay(options):
    if not options:
        return None
    if not isinstance(options, dict):
        raise ValueError("Некорректные настройки футажа.")
    path = options.get("path")
    if not isinstance(path, str) or not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in VIDEO_EXTENSIONS:
        raise ValueError("Для футажа выберите существующий MP4, MOV, MKV или WEBM.")
    try:
        opacity, speed = float(options.get("opacity", .3)), float(options.get("speed", .5))
    except (TypeError, ValueError):
        raise ValueError("Прозрачность и скорость футажа должны быть числами.") from None
    if not math.isfinite(opacity) or not 0 <= opacity <= 1:
        raise ValueError("Заметность футажа: от 0 до 100%.")
    if not math.isfinite(speed) or not .25 <= speed <= 2:
        raise ValueError("Скорость футажа: от 0,25× до 2×.")
    try:
        intro_duration(path)  # Checks that a readable video track actually exists.
    except ValueError as exc:
        raise ValueError("Не удалось прочитать видеофайл футажа.") from exc
    result = {"path": os.path.abspath(path), "opacity": opacity, "speed": speed}
    if 'cache' in options:
        result['cache'] = bool(options['cache'])
    return result


def apply_video_overlay(video_path, overlay):
    """Fallback for systems without OpenGL. Screen blend keeps black neutral."""
    if not overlay or overlay["opacity"] == 0:
        return
    output = video_path + ".overlay.tmp.mp4"
    filters = (f"[1:v]setpts=(PTS-STARTPTS)/{overlay['speed']:.8f},"
               f"scale={pipeline.W}:{pipeline.H},setsar=1,fps={pipeline.FPS},format=gbrp[fx];"
               "[0:v]setpts=PTS-STARTPTS,format=gbrp[base];"
               f"[base][fx]blend=all_mode=screen:all_opacity={overlay['opacity']:.8f}:shortest=1,"
               "format=yuv420p[v]")
    try:
        pipeline.run_ffmpeg(["-i", video_path, "-stream_loop", "-1", "-i", overlay["path"],
                             "-filter_complex", filters, "-map", "[v]", "-map", "0:a:0",
                             *pipeline.video_encoder_args(), "-c:a", "copy", "-movflags", "+faststart", output])
        pipeline.check_cancel()
        os.replace(output, video_path)
    finally:
        if os.path.exists(output):
            os.remove(output)


def _natural_key(path):
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", os.path.basename(path))]


def list_images(folder):
    if not os.path.isdir(folder):
        raise ValueError("Папка с изображениями не найдена. Выберите её снова.")
    paths = [os.path.join(folder, name) for name in os.listdir(folder)
             if os.path.isfile(os.path.join(folder, name))
             and os.path.splitext(name)[1].lower() in EXTENSIONS]
    paths.sort(key=_natural_key)
    if not paths:
        raise ValueError("В папке нет изображений PNG, JPG или WEBP.")
    return paths


def job_dir(audio):
    absolute = os.path.abspath(audio)
    stem = pipeline.short_base(os.path.splitext(os.path.basename(absolute))[0], keep=25)
    suffix = hashlib.sha1(os.path.normcase(absolute).encode("utf-8")).hexdigest()[:8]
    return os.path.join(pipeline.data_dir(), "jobs", f"manual_{stem}_{suffix}")


def _import_images(paths, work_dir, progress, offset=0):
    image_dir = os.path.join(work_dir, "images")
    os.makedirs(image_dir, exist_ok=True)
    stamp = [{"offset": offset, "path": os.path.normcase(os.path.abspath(p)), "size": os.stat(p).st_size,
              "mtime_ns": os.stat(p).st_mtime_ns} for p in paths]
    manifest = os.path.join(work_dir, "manual_images.json")
    ready = all(os.path.isfile(os.path.join(image_dir, f"{i + offset:04d}.png")) for i in range(len(paths)))
    if ready and pipeline.read_json(manifest) == stamp:
        progress("images", len(paths), len(paths), "свои изображения уже подготовлены")
        return image_dir

    # A changed source invalidates prerendered clips, including an interrupted run.
    for name in ("clips", "clips_m", "groups"):
        shutil.rmtree(os.path.join(work_dir, name), ignore_errors=True)
    for name in os.listdir(image_dir):
        if re.fullmatch(r"\d{4,}\.png", name):
            os.remove(os.path.join(image_dir, name))
    for i, path in enumerate(paths):
        pipeline.check_cancel()
        dest = os.path.join(image_dir, f"{i + offset:04d}.png")
        tmp = dest + ".part"
        try:
            with Image.open(path) as original:
                corrected = ImageOps.exif_transpose(original)
                corrected.convert("RGB").save(tmp, format="PNG")
            os.replace(tmp, dest)
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            raise ValueError(f"Не удалось открыть изображение {os.path.basename(path)}: {exc}") from exc
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        progress("images", i + 1, len(paths), f"загружено изображений: {i + 1}/{len(paths)}")
    pipeline.write_json(manifest, stamp)
    return image_dir


def make_video(audio, folder, motion=False, subtitles=False, progress_cb=None, timing_file=None,
               intro_video=None, focus_points=None, zoom_seconds=18, film_effect="none",
               preview=False, film_intensity=50, overlay=None):
    if not os.path.isfile(audio):
        raise ValueError("Файл озвучки не найден. Выберите MP3 заново.")
    paths = list_images(folder)
    overlay = validate_overlay(overlay)
    progress = pipeline.Progress(progress_cb)
    pipeline.CANCEL.clear()
    work_dir = job_dir(audio) + ("_preview" if preview else "")
    os.makedirs(work_dir, exist_ok=True)
    duration = pipeline.audio_duration(audio)
    if duration <= 0:
        raise ValueError("Не удалось определить длину озвучки.")
    paths, durations, intro_length = resolve_full_timing(paths, timing_file, duration, intro_video)
    if preview:
        intro_length = min(3.0, intro_length)
        durations = [min(3.0, value) for value in durations]
        subtitles = False  # Full-length speech recognition is inappropriate for a short visual test.
    focus_points = focus_points or {}
    try:
        zoom_seconds = float(zoom_seconds)
    except (TypeError, ValueError):
        raise ValueError("Скорость приближения должна быть числом.") from None
    if not math.isfinite(zoom_seconds) or not 6 <= zoom_seconds <= 40:
        raise ValueError("Время приближения: от 6 до 40 секунд.")
    if film_effect not in ("none", "light", "retro"):
        raise ValueError("Неизвестный эффект плёнки.")
    try:
        film_intensity = float(film_intensity)
    except (ValueError, TypeError):
        raise ValueError("Заметность плёнки: число от 0 до 100%.") from None
    if not math.isfinite(film_intensity) or not 0 <= film_intensity <= 100:
        raise ValueError("Заметность плёнки: от 0 до 100%.")
    n = len(paths)
    scenes, position = [], 0.0
    videos = {}
    offset = int(bool(intro_video))
    if intro_video:
        scenes.append({"index": 0, "start": 0.0, "duration": intro_length})
        videos[0] = intro_video
        position = intro_length
    points = {}
    for i, length in enumerate(durations):
        name = os.path.basename(paths[i])
        try:
            point = focus_points.get(name, [0.5, 0.5])
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            raise ValueError(f"Неверная точка фокуса для {name}.") from None
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in (x, y)):
            raise ValueError(f"Точка фокуса для {name} должна быть внутри кадра.")
        points[name] = [x, y]
        scenes.append({"index": i + offset, "start": position, "duration": length,
                       "focus": (x, y), "zoom_seconds": zoom_seconds, "zoom": bool(motion)})
        position += length
    timeline_manifest = os.path.join(work_dir, "manual_timing.json")
    intro_stamp = ({"path": os.path.abspath(intro_video), "size": os.stat(intro_video).st_size,
                    "mtime_ns": os.stat(intro_video).st_mtime_ns} if intro_video else None)
    overlay_stamp = ({**overlay, "size": os.stat(overlay["path"]).st_size,
                      "mtime_ns": os.stat(overlay["path"]).st_mtime_ns} if overlay else None)
    render_state = {"durations": durations, "intro": intro_stamp, "intro_length": intro_length,
                    "preview": bool(preview), "focus": points,
                    "zoom_seconds": zoom_seconds, "motion": bool(motion),
                    "renderer": "opengl-single-pass-v1", "film_effect": film_effect,
                    "film_intensity": film_intensity,
                    "overlay": overlay_stamp,
                    "transitions": "zoom-slide-v1"}
    if pipeline.read_json(timeline_manifest) != render_state:
        for name in ("clips", "clips_m", "groups"):
            shutil.rmtree(os.path.join(work_dir, name), ignore_errors=True)
        pipeline.write_json(timeline_manifest, render_state)
    detail = (f"проба: до 3 с на кадр, всего {intro_length + sum(durations):.0f} с" if preview else
              "по таймингу от 00:00" if timing_file else
              f"примерно {(duration - intro_length) / n:.1f} с на кадр")
    progress("scenes", n, n, f"{n} своих кадров · {detail}")
    image_dir = _import_images(paths, work_dir, progress, offset=offset)
    output_dir = os.path.join(pipeline.data_dir(), "output")
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.basename(work_dir)
    result = os.path.join(output_dir, base + ".mp4")

    def render_target(target):
        try:
            import gpu_render
            gpu_render.render(scenes, image_dir, videos, audio, work_dir, target, progress,
                              motion=motion, film=film_effect, film_intensity=film_intensity, overlay=overlay)
            return
        except Exception as exc:
            pipeline.check_cancel()
            pipeline.log(f"GPU-эффекты недоступны, резервный рендер: {exc}")
            progress("render", 0, 0, "резервный рендер · GPU-эффекты недоступны: " + str(exc)[:160])
        started = time.perf_counter()
        pipeline.assemble_video(scenes, image_dir, videos, audio, work_dir, target, progress, motion=motion)
        if overlay and overlay["opacity"] > 0:
            progress("render", 0, 0, "накладываю видеофутаж · резервный рендер")
            apply_video_overlay(target, overlay)
        if film_effect != "none":
            progress("render", 0, 0, "накладываю частицы старой плёнки · резервный рендер")
            apply_film_effect(target, film_effect, work_dir, film_intensity=film_intensity)
        pipeline.write_json(os.path.join(work_dir, "render_metrics.json"),
                            {"backend": "ffmpeg-fallback", "elapsed_seconds": round(time.perf_counter() - started, 3)})

    if subtitles:
        cfg = pipeline.load_config()
        if cfg.get("fastgen_api_key"):
            engine, client = "fastgen", pipeline.FastGen(cfg["fastgen_api_key"])
        elif cfg.get("openai_api_key"):
            engine, client = "whisper", pipeline.OpenAIClient(cfg["openai_api_key"])
        else:
            raise ValueError("Для субтитров нужен ключ fast-gen или OpenAI для распознавания речи.")
        transcript = pipeline.transcribe(engine, client, audio, work_dir, progress, duration)
        raw = os.path.join(work_dir, "nosubs.mp4")
        render_target(raw)
        pipeline.check_cancel()
        progress("subtitles", 0, 1, "накладываю субтитры")
        sub_dir = os.path.join(output_dir, "subtitles")
        os.makedirs(sub_dir, exist_ok=True)
        srt = os.path.join(sub_dir, base + ".srt")
        pipeline.subs.write_srt(transcript["words"], srt)
        pipeline.subs.burn(pipeline.ffmpeg_bin(), raw, srt, result + ".tmp.mp4", pipeline.NO_WINDOW)
        os.replace(result + ".tmp.mp4", result)
        progress("subtitles", 1, 1, "субтитры готовы")
    else:
        render_target(result)
    pipeline.cleanup_job(work_dir)
    progress("done", 1, 1, f"готово: {result}")
    return result




