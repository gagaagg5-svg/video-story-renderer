"""Real FFmpeg integration checks for straight-cut video repetition."""
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import video_loop
FF = str(ROOT/'ffmpeg'/'ffmpeg.exe') if (ROOT/'ffmpeg'/'ffmpeg.exe').exists() else 'ffmpeg'
PROBE = FF.replace('ffmpeg.exe', 'ffprobe.exe') if FF.endswith('.exe') else 'ffprobe'
fake = ModuleType('pipeline')
fake.CANCEL = threading.Event()
class Cancelled(Exception): pass
def check_cancel():
    if fake.CANCEL.is_set(): raise Cancelled()
fake.check_cancel = check_cancel
fake.ffmpeg_bin = lambda name='ffmpeg': PROBE if name == 'ffprobe' else FF
fake.NO_WINDOW = 0
fake.audio_duration = lambda p: float(subprocess.check_output([PROBE, '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(p)]))
fake.video_encoder_args = lambda: ['-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p']
sys.modules['pipeline'] = fake
def ff(args):
    return subprocess.check_output([FF, '-y', '-v', 'error', *map(str, args)])
def frames(p):
    return [line.split(',')[-1].strip() for line in ff(['-i', p, '-map', '0:v:0', '-f', 'framemd5', '-']).decode().splitlines() if not line.startswith('#')]
def info(p):
    return json.loads(subprocess.check_output([PROBE, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(p)]))

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    source = root / "исходное видео '1.mp4"
    ff(['-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=25:d=2', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-c:v', 'libx264', '-c:a', 'aac', source])
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    out = root/'out'
    # H.264 fast path must not request a video encoder.
    def no_encoder(): raise AssertionError('Compatible video was re-encoded')
    fake.video_encoder_args = no_encoder
    result = video_loop.create(str(source), 7.16, str(out))
    data = info(result)
    assert not any(s['codec_type'] == 'audio' for s in data['streams'])
    assert abs(float(data['format']['duration']) - 7.16) < .15, data['format']
    original_frames, repeated = frames(source), frames(result)
    assert repeated[:150] == original_frames * 3, 'Repeat lost/reordered or changed frames'
    kept = video_loop.create(str(source), 5, str(out), keep_audio=True)
    assert any(s['codec_type'] == 'audio' for s in info(kept)['streams'])
    short = video_loop.create(str(source), .8 + .2, str(out))
    assert abs(fake.audio_duration(short) - 1) < .15
    track = root/'music.wav'
    ff(['-f', 'lavfi', '-i', 'sine=frequency=800:duration=0.7', track])
    music = video_loop.create(str(source), 5, str(out), music={'tracks':[str(track)], 'volume':25})
    assert any(s['codec_type'] == 'audio' for s in info(music)['streams'])
    pcm = ff(['-i', music, '-ss', '4', '-t', '0.2', '-map', '0:a:0', '-f', 's16le', '-'])
    assert any(pcm), 'Looped music is silent near end'
    assert frames(music)[:100] == original_frames * 2
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    tiny = root/'tiny.mp4'
    ff(['-f', 'lavfi', '-i', 'color=s=32x32:r=1:d=1', '-c:v', 'libx264', tiny])
    long = video_loop.create(str(tiny), 7200, str(out))
    assert abs(fake.audio_duration(long) - 7200) < 1.1
    before = set(out.iterdir())
    def cancel(stage, done, total, message):
        fake.CANCEL.set()
    try:
        video_loop.create(str(source), 7200, str(out), progress=cancel)
        raise AssertionError('Cancellation ignored')
    except Cancelled: pass
    finally: fake.CANCEL.clear()
    assert set(out.iterdir()) == before, 'Partial output leaked'
    fallback = root/'source.webm'
    ff(['-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=25:d=1', '-c:v', 'libvpx-vp9', fallback])
    fake.video_encoder_args = lambda: ['-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p']
    converted = video_loop.create(str(fallback), 3, str(out))
    assert info(converted)['streams'][0]['codec_name'] == 'h264'
    for invalid in [0, -1, float('nan'), float('inf'), 86401, 'bad']:
        try:
            video_loop.validate(str(source), invalid)
            raise AssertionError(invalid)
        except ValueError: pass
print('Video loop passed: 2 hours, repeated identical frames, trim, audio, music, fallback, source safety, cancellation')
