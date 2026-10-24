"""Loop a user playlist under narration without re-encoding video frames."""
import math
import os
import subprocess
import tempfile
import threading
from pathlib import Path

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.wma', '.aiff', '.aif'}


def validate(options):
    if not options:
        return None
    if not isinstance(options, dict):
        raise ValueError('Некорректные настройки музыки.')
    tracks = options.get('tracks', [])
    if not isinstance(tracks, list) or len(tracks) > 50:
        raise ValueError('Выберите не более 50 музыкальных треков.')
    for path in tracks:
        if not isinstance(path, str) or not os.path.isfile(path) or Path(path).suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError('Музыкальный файл не найден или его формат не поддерживается.')
    try:
        volume = float(options.get('volume', 15))
    except (TypeError, ValueError):
        raise ValueError('Громкость музыки: число от 0 до 100%.') from None
    if not math.isfinite(volume) or not 0 <= volume <= 100:
        raise ValueError('Громкость музыки: от 0 до 100%.')
    return {'tracks': [os.path.abspath(p) for p in tracks], 'volume': volume} if tracks else None


def _run(args):
    import pipeline
    pipeline.check_cancel()
    done = threading.Event()
    process = None
    watcher = None
    with tempfile.TemporaryFile() as log:
        try:
            process = subprocess.Popen([pipeline.ffmpeg_bin(), '-y', '-hide_banner', '-loglevel', 'error', *args],
                                       stdout=subprocess.DEVNULL, stderr=log, creationflags=pipeline.NO_WINDOW)
            def watch():
                while not done.wait(.1):
                    if pipeline.CANCEL.is_set():
                        if process.poll() is None:
                            process.kill()
                        return
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            code = process.wait()
            pipeline.check_cancel()
            if code:
                log.seek(0)
                raise RuntimeError('Не удалось наложить музыку: ' + log.read().decode(errors='replace')[-1000:])
        finally:
            done.set()
            if process and process.poll() is None:
                process.kill()
                process.wait()
            if watcher:
                watcher.join(timeout=2)


def apply(video_path, options, progress=None):
    import pipeline
    settings = validate(options)
    if not settings or settings['volume'] == 0:
        return video_path
    progress = progress or (lambda *args: None)
    duration = pipeline.audio_duration(video_path)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Не удалось определить длину видео для музыки.')
    tracks = settings['tracks']
    channels = int(subprocess.check_output(
        [pipeline.ffmpeg_bin('ffprobe'), '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=channels', '-of', 'csv=p=0', video_path],
        creationflags=pipeline.NO_WINDOW, timeout=15).strip())
    voice_layout = 'pan=stereo|c0=c0|c1=c0' if channels == 1 else 'aformat=channel_layouts=stereo'
    # Normalize just the short playlist once. The final mix loops it as a stream,
    # so memory and temporary audio size do not grow with the full video length.
    with tempfile.TemporaryDirectory(prefix='music_mix_', dir=os.path.dirname(os.path.abspath(video_path))) as work:
        playlist = os.path.join(work, 'playlist.flac')
        output = os.path.join(work, 'mixed.mp4')
        progress('music', 0, 2, f'подготавливаю музыку · {len(tracks)} треков по порядку')
        inputs, filters = [], []
        for i, track in enumerate(tracks):
            inputs += ['-i', track]
            filters.append(f'[{i}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]')
        filters.append(''.join(f'[a{i}]' for i in range(len(tracks))) + f'concat=n={len(tracks)}:v=0:a=1[out]')
        _run([*inputs, '-filter_complex', ';'.join(filters), '-map', '[out]', '-c:a', 'flac', playlist])
        progress('music', 1, 2, f'музыка {settings["volume"]:g}% · повторяю плейлист до конца ролика')
        fade = min(2, duration / 2)
        filters = (f'[0:a:0]aresample=48000,{voice_layout},asetpts=PTS-STARTPTS,apad[voice];'
                   f'[1:a:0]volume={settings["volume"] / 100:.8f},afade=t=in:d={min(.5, duration / 2):.8f},'
                   f'afade=t=out:st={duration-fade:.8f}:d={fade:.8f}[music];'
                   '[voice][music]amix=inputs=2:duration=first:normalize=0,'
                   f'alimiter=limit=0.95:level=0:latency=1,atrim=duration={duration:.8f}[a]')
        _run(['-i', video_path, '-stream_loop', '-1', '-i', playlist, '-filter_complex', filters,
              '-map', '0:v:0', '-map', '[a]', '-map_metadata', '0', '-c:v', 'copy',
              '-c:a', 'aac', '-b:a', '192k', '-t', f'{duration:.8f}', '-movflags', '+faststart', output])
        pipeline.check_cancel()
        os.replace(output, video_path)
    progress('music', 2, 2, 'музыка добавлена')
    return video_path
