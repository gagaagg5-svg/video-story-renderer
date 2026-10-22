"""Assemble a voice-over with a user-selected folder of still images.

This path never calls an image generator. It deliberately keeps its job cache
separate from Kadro.Gen's scene-generation jobs.
"""
import hashlib
import csv
import io
import os
import re
import shutil
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

from PIL import Image, ImageOps, UnidentifiedImageError

import pipeline


EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


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


def _read_xml_timing(file_path):
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
            if not name or os.path.splitext(name)[1].lower() not in EXTENSIONS:
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


def resolve_timing(paths, timing_file, audio_duration):
    """Return images in timeline order with durations; enforce one continuous image per clip."""
    if not timing_file:
        duration = audio_duration / len(paths)
        if duration < 0.3:
            raise ValueError("Изображений слишком много для длины озвучки: на кадр нужно хотя бы 0,3 секунды.")
        return paths, [duration] * len(paths)
    if not os.path.isfile(timing_file):
        raise ValueError("Файл тайминга не найден. Выберите CSV или XML снова.")
    ext = os.path.splitext(timing_file)[1].lower()
    if ext == ".csv":
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
            if i == 0 and start != 0:
                raise ValueError("XML: первый кадр должен начинаться в начале таймлайна (00:00:00:00).")
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


def _import_images(paths, work_dir, progress):
    image_dir = os.path.join(work_dir, "images")
    os.makedirs(image_dir, exist_ok=True)
    stamp = [{"path": os.path.normcase(os.path.abspath(p)), "size": os.stat(p).st_size,
              "mtime_ns": os.stat(p).st_mtime_ns} for p in paths]
    manifest = os.path.join(work_dir, "manual_images.json")
    ready = all(os.path.isfile(os.path.join(image_dir, f"{i:04d}.png")) for i in range(len(paths)))
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
        dest = os.path.join(image_dir, f"{i:04d}.png")
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


def make_video(audio, folder, motion=False, subtitles=False, progress_cb=None, timing_file=None):
    if not os.path.isfile(audio):
        raise ValueError("Файл озвучки не найден. Выберите MP3 заново.")
    paths = list_images(folder)
    progress = pipeline.Progress(progress_cb)
    pipeline.CANCEL.clear()
    work_dir = job_dir(audio)
    os.makedirs(work_dir, exist_ok=True)
    duration = pipeline.audio_duration(audio)
    if duration <= 0:
        raise ValueError("Не удалось определить длину озвучки.")
    paths, durations = resolve_timing(paths, timing_file, duration)
    n = len(paths)
    scenes, position = [], 0.0
    for i, length in enumerate(durations):
        scenes.append({"index": i, "start": position, "duration": length})
        position += length
    timeline_manifest = os.path.join(work_dir, "manual_timing.json")
    if pipeline.read_json(timeline_manifest) != durations:
        for name in ("clips", "clips_m", "groups"):
            shutil.rmtree(os.path.join(work_dir, name), ignore_errors=True)
        pipeline.write_json(timeline_manifest, durations)
    detail = "по таймингу" if timing_file else f"примерно {duration / n:.1f} с на кадр"
    progress("scenes", n, n, f"{n} своих кадров · {detail}")
    image_dir = _import_images(paths, work_dir, progress)
    output_dir = os.path.join(pipeline.data_dir(), "output")
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.basename(work_dir)
    result = os.path.join(output_dir, base + ".mp4")
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
        pipeline.assemble_video(scenes, image_dir, {}, audio, work_dir, raw, progress, motion=motion)
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
        pipeline.assemble_video(scenes, image_dir, {}, audio, work_dir, result, progress, motion=motion)
    pipeline.cleanup_job(work_dir)
    progress("done", 1, 1, f"готово: {result}")
    return result
