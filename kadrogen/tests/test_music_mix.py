"""Real FFmpeg checks: order, loop, gain, narration, duration and video copy."""
import array
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import music_mix
FF = str(ROOT/'ffmpeg'/'ffmpeg.exe') if (ROOT/'ffmpeg'/'ffmpeg.exe').exists() else 'ffmpeg'
PROBE = FF.replace('ffmpeg.exe','ffprobe.exe') if FF.endswith('ffmpeg.exe') else 'ffprobe'
fake = ModuleType('pipeline')
fake.CANCEL = threading.Event()
class Cancelled(Exception): pass
def check_cancel():
    if fake.CANCEL.is_set(): raise Cancelled()
fake.check_cancel = check_cancel
fake.ffmpeg_bin = lambda name='ffmpeg': PROBE if name == 'ffprobe' else FF
fake.NO_WINDOW = 0
fake.audio_duration = lambda p: float(subprocess.check_output([PROBE,'-v','error','-show_entries','format=duration','-of','csv=p=0',str(p)]))
sys.modules['pipeline'] = fake

def ff(args):
    return subprocess.check_output([FF,'-y','-v','error',*map(str,args)])
def video_hash(p):
    return ff(['-i',p,'-map','0:v:0','-c:v','copy','-f','hash','pipe:1'])
def audio(p, time):
    return array.array('h',ff(['-i',p,'-ss',time,'-t','0.20','-vn','-ac','1','-ar','16000','-f','s16le','pipe:1']))
def amplitude(samples, hz):
    n=len(samples)
    return 2*abs(sum(x*complex(math.cos(2*math.pi*hz*i/16000), math.sin(2*math.pi*hz*i/16000)) for i,x in enumerate(samples)))/n/32768

with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)
    source=root/'result.mp4'
    ff(['-f','lavfi','-i','testsrc2=s=320x180:r=25:d=10','-f','lavfi','-i','sine=frequency=440:duration=10',
        '-c:v','libx264','-preset','veryfast','-c:a','aac','-shortest',source])
    original=source.read_bytes()
    vhash=video_hash(source)
    tracks=[]
    for i,hz in enumerate((600,800,1000,1200)):
        # Different sample rates, formats and channel layouts exercise normalization.
        p=root/f"track '{i}.{'wav' if i%2 else 'mp3'}"
        ff(['-f','lavfi','-i',f'sine=frequency={hz}:duration=1:sample_rate={44100 if i%2 else 32000}',
            '-ac',str(1 if i%2 else 2),p])
        tracks.append(str(p))
    settings={'tracks':tracks,'volume':20}
    music_mix.apply(str(source),settings)
    assert video_hash(source)==vhash, 'Video must not be re-encoded'
    assert abs(fake.audio_duration(source)-10)<.1
    for t,hz in ((.7,600),(1.5,800),(2.5,1000),(3.5,1200),(4.5,600),(5.5,800),(6.5,1000),(7.5,1200)):
        values=audio(source,str(t))
        measured={f:amplitude(values,f) for f in (600,800,1000,1200)}
        assert max(measured,key=measured.get)==hz,(t,measured)
        assert measured[hz]>.012,(t,measured)
        assert .10<amplitude(values,440)<.15, 'Narration was unexpectedly reduced'
    low=root/'low.mp4';low.write_bytes(original)
    music_mix.apply(str(low),{'tracks':tracks,'volume':10})
    high=root/'high.mp4';high.write_bytes(original)
    music_mix.apply(str(high),{'tracks':tracks,'volume':30})
    ratio=amplitude(audio(high,'4.5'),600)/amplitude(audio(low,'4.5'),600)
    assert 2.5<ratio<3.5,ratio
    silent=root/'zero.mp4';silent.write_bytes(original)
    music_mix.apply(str(silent),{'tracks':tracks,'volume':0})
    assert silent.read_bytes()==original
    protected=root/'protected.mp4';protected.write_bytes(original)
    def cancel(stage,done,total,message):
        if done==1: fake.CANCEL.set()
    try:
        music_mix.apply(str(protected),settings,cancel)
        raise AssertionError('Cancellation ignored')
    except Cancelled: pass
    finally: fake.CANCEL.clear()
    assert protected.read_bytes()==original
    assert not list(root.glob('music_mix_*'))
    for bad in ({'tracks':['missing.mp3'],'volume':10},{'tracks':tracks,'volume':float('nan')},
                {'tracks':tracks,'volume':101},{'tracks':tracks,'volume':None}, {'tracks':'bad'}):
        try:
            music_mix.validate(bad)
            raise AssertionError('Invalid music options accepted')
        except ValueError: pass
    print('Music mix passed: four tracks in order, looping, adjustable gain, narration, unchanged video and cancellation')
