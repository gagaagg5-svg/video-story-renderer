"""Arrange narration files on one shared timeline, without changing their speed."""
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import wave
import pipeline


def seconds(value):
    try:
        parts = str(value).strip().replace(',', '.').split(':')
        if not 1 <= len(parts) <= 3:
            raise ValueError()
        nums = [float(p) for p in parts]
        if any(not math.isfinite(n) or n < 0 for n in nums):
            raise ValueError()
        if len(nums) > 1 and any(n >= 60 for n in nums[1:]):
            raise ValueError()
        result = 0.0
        for n in nums:
            result = result * 60 + n
        return result
    except (ValueError, TypeError):
        raise ValueError(f'Некорректное время озвучки: {value}. Пример: 00:05:30 или 330.') from None


def plan(options):
    if not options:
        return None
    paths = options.get('tracks')
    if not isinstance(paths, list) or not paths or len(paths) > 100:
        raise ValueError('Выберите от 1 до 100 файлов озвучки.')
    if any(not isinstance(p, str) or not os.path.isfile(p) for p in paths):
        raise ValueError('Файл озвучки не найден. Выберите файлы заново.')
    mode = options.get('mode', 'sequence')
    if mode not in ('sequence', 'timing'):
        raise ValueError('Неизвестный режим озвучки.')
    starts = None
    if mode == 'timing':
        file = options.get('timing') or ''
        if not os.path.isfile(file):
            raise ValueError('Выберите CSV с таймингом озвучки: filename,start.')
        names = {os.path.basename(p).casefold(): p for p in paths}
        if len(names) != len(paths):
            raise ValueError('Для тайминга имена файлов озвучки должны быть уникальными.')
        with open(file, encoding='utf-8-sig', newline='') as f:
            sample = f.read()
        try:
            dialect = csv.Sniffer().sniff(sample[:4096], delimiters=',;\t')
            rows = list(csv.DictReader(sample.splitlines(), dialect=dialect))
            items = [(names[row['filename'].strip().casefold()], seconds(row['start'])) for row in rows]
        except (csv.Error, KeyError, AttributeError):
            raise ValueError('CSV озвучки: нужны столбцы filename,start; имена должны совпадать с выбранными файлами.') from None
        if len(items) != len(paths) or len({p for p, _ in items}) != len(paths):
            raise ValueError('CSV озвучки должен содержать каждый выбранный файл ровно один раз.')
        items.sort(key=lambda item: item[1])
        paths, starts = zip(*items)
    clips, end = [], 0.0
    for i, path in enumerate(paths):
        try:
            duration = pipeline.audio_duration(path)
            probe = subprocess.run([pipeline.ffmpeg_bin('ffprobe'), '-v', 'error', '-select_streams', 'a:0',
                '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', path],
                capture_output=True, text=True, check=True, timeout=30, creationflags=pipeline.NO_WINDOW)
            if probe.stdout.strip() != 'audio' or not math.isfinite(duration) or duration <= 0:
                raise ValueError()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ValueError(f'Не удалось прочитать озвучку: {os.path.basename(path)}') from None
        start = starts[i] if starts is not None else end
        if start < end - 0.001:
            raise ValueError(f'Озвучка {os.path.basename(path)} перекрывает предыдущую. Её конец: {end:.3f} с.')
        stat = os.stat(path)
        clips.append(dict(path=os.path.abspath(path), start=max(start,end), duration=duration,
                          size=stat.st_size, mtime_ns=stat.st_mtime_ns))
        end = max(start,end) + duration
    if end > 24 * 3600:
        raise ValueError('Общая длительность озвучки не должна превышать 24 часа.')
    return {'clips': clips, 'duration': end}


def _run(args):
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen([pipeline.ffmpeg_bin(), '-y', '-v', 'error', *args],
                                   stdout=subprocess.DEVNULL, stderr=log, creationflags=pipeline.NO_WINDOW)
        try:
            while process.poll() is None:
                pipeline.check_cancel()
                time.sleep(.1)
            pipeline.check_cancel()
            if process.returncode:
                log.seek(0)
                raise RuntimeError('Подготовка озвучки: ' + log.read().decode(errors='replace')[-500:])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def prepare(arrangement, progress):
    pipeline.check_cancel()
    key = hashlib.sha256(json.dumps(arrangement, sort_keys=True).encode()).hexdigest()[:20]
    cache = Path(pipeline.data_dir()) / 'narration'
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / ('narration_' + key + '.wav')
    if target.is_file():
        return str(target)
    with tempfile.TemporaryDirectory(prefix='voice_', dir=cache) as folder:
        root = Path(folder); parts=[]; end=0.0
        for i, clip in enumerate(arrangement['clips']):
            pipeline.check_cancel()
            gap = clip['start'] - end
            if gap > .00001:
                silence=root / f'gap{i}.wav'
                _run(['-f','lavfi','-i','anullsrc=r=48000:cl=mono','-t',f'{gap:.9f}',
                      '-c:a','pcm_s16le','-rf64','auto',str(silence)])
                parts.append(silence)
            audio=root/f'clip{i}.wav'
            _run(['-i',clip['path'],'-map','0:a:0','-vn','-ar','48000','-ac','1',
                  '-af','apad','-t',f"{clip['duration']:.9f}",'-c:a','pcm_s16le','-rf64','auto',str(audio)])
            parts.append(audio);end=clip['start']+clip['duration']
            progress('images', i+1, len(arrangement['clips']), f"озвучка: {i+1}/{len(arrangement['clips'])}")
        listing=root/'concat.txt'
        listing.write_text(''.join("file '"+p.name+"'\n" for p in parts),encoding='utf-8')
        output=root/'combined.wav'
        _run(['-f','concat','-safe','0','-i',str(listing),'-c:a','copy','-rf64','auto',str(output)])
        pipeline.check_cancel()
        os.replace(output,target)
    return str(target)
