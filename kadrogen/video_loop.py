"""Repeat one video with straight cuts; preserve compatible compressed video."""
import json
import math
import os
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path


def validate(source, seconds):
    source = os.path.abspath(str(source or ''))
    if not os.path.isfile(source):
        raise ValueError('Выберите видео для повтора.')
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        raise ValueError('Укажите длительность видео.') from None
    if not math.isfinite(seconds) or not 1 <= seconds <= 86400:
        raise ValueError('Длительность: от 1 секунды до 24 часов.')
    return source, seconds


def probe(source):
    import pipeline
    r = subprocess.run([pipeline.ffmpeg_bin('ffprobe'), '-v', 'error', '-show_streams',
                        '-show_format', '-of', 'json', source], capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=30,
                       creationflags=pipeline.NO_WINDOW)
    if r.returncode:
        raise ValueError('Не удалось прочитать видео: ' + r.stderr[-500:])
    data = json.loads(r.stdout)
    video = next((s for s in data['streams'] if s['codec_type'] == 'video'
                  and not s.get('disposition', {}).get('attached_pic')), None)
    if not video:
        raise ValueError('В файле нет видеодорожки.')
    duration = float(video.get('duration') or data.get('format', {}).get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Не удалось определить длительность исходного видео.')
    return video, any(s['codec_type'] == 'audio' for s in data['streams']), duration


def _run(args, duration, progress, label):
    import pipeline
    pipeline.check_cancel()
    done = threading.Event()
    process = None
    watcher = None
    with tempfile.TemporaryFile() as log:
        try:
            process = subprocess.Popen([pipeline.ffmpeg_bin(), '-y', '-hide_banner', '-loglevel', 'error',
                                        *args[:-1], '-progress', 'pipe:1', '-nostats', args[-1]],
                                       stdout=subprocess.PIPE, stderr=log, text=True,
                                       creationflags=pipeline.NO_WINDOW)
            def watch():
                while not done.wait(.1):
                    if pipeline.CANCEL.is_set():
                        if process.poll() is None:
                            process.kill()
                        return
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            last = -1
            for line in process.stdout:
                if line.startswith('out_time_us='):
                    try:
                        value = min(99, max(0, int(float(line.split('=', 1)[1]) / 10000 / duration)))
                    except ValueError:
                        continue
                    if value != last:
                        progress('render', value, 100, label)
                        last = value
            code = process.wait()
            pipeline.check_cancel()
            if code:
                log.seek(0)
                raise RuntimeError('Не удалось повторить видео: ' + log.read().decode(errors='replace')[-1000:])
        finally:
            done.set()
            if process:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
            if watcher:
                watcher.join(timeout=2)


def create(source, seconds, out_dir, keep_audio=False, music=None, progress=None):
    import pipeline
    import music_mix
    source, seconds = validate(source, seconds)
    settings = music_mix.validate(music)
    progress = progress or (lambda *args: None)
    pipeline.check_cancel()
    video, has_audio, duration = probe(source)
    os.makedirs(out_dir, exist_ok=True)
    output = os.path.join(out_dir, 'loop_' + uuid.uuid4().hex[:12] + '.mp4')
    # Publish only a fully completed result; cancellation leaves the source intact.
    with tempfile.TemporaryDirectory(prefix='video_loop_', dir=out_dir) as work:
        prepared = source
        index = video['index']
        if video['codec_name'] not in ('h264', 'hevc'):
            prepared = os.path.join(work, 'prepared.mp4')
            progress('render', 0, 100, 'однократно подготавливаю исходный клип для MP4')
            args = ['-i', source, '-map', f'0:{index}', '-map', '0:a:0?',
                    '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
                    *pipeline.video_encoder_args(), '-c:a', 'aac', '-b:a', '192k', prepared]
            _run(args, duration, progress, 'подготавливаю короткий исходный клип')
            index = 0
        tmp = os.path.join(work, 'loop.mp4')
        progress('render', 0, 100, 'повторяю видео без переходов · видеодорожка без перекодирования')
        args = ['-stream_loop', '-1', '-i', prepared, '-map', f'0:{index}', '-c:v', 'copy']
        if keep_audio and has_audio:
            args += ['-map', '0:a:0', '-c:a', 'aac', '-b:a', '192k']
        else:
            args += ['-an']
        args += ['-t', f'{seconds:.6f}', '-movflags', '+faststart', tmp]
        _run(args, seconds, progress, 'повторяю видео без переходов')
        if settings:
            music_mix.apply(tmp, settings, progress)
        pipeline.check_cancel()
        os.replace(tmp, output)
    progress('done', 1, 1, f'готово: {output}')
    return output
