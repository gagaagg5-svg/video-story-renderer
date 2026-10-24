"""Verify accelerated file duration, audio pitch, source preservation and cancel."""
import array
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import speed_copy
FF = str(ROOT/'ffmpeg'/'ffmpeg.exe') if (ROOT/'ffmpeg'/'ffmpeg.exe').exists() else 'ffmpeg'
PROBE = FF.replace('ffmpeg.exe','ffprobe.exe') if FF.endswith('ffmpeg.exe') else 'ffprobe'
ENC = ['-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p']
def ff(args):
    return subprocess.check_output([FF,'-y','-v','error',*map(str,args)])
with tempfile.TemporaryDirectory() as tmp:
    source=Path(tmp)/'original.test.mp4'
    ff(['-f','lavfi','-i','testsrc2=s=320x180:r=25:d=10','-f','lavfi','-i',
        'sine=frequency=440:duration=10','-c:v','libx264','-c:a','aac','-shortest',source])
    digest=hashlib.sha256(source.read_bytes()).digest()
    progress=[]
    output=speed_copy.create(str(source),1.2,FF,ENC,10,progress.append)
    data=json.loads(subprocess.check_output([PROBE,'-v','error','-show_streams','-show_format','-of','json',output]))
    assert abs(float(data['format']['duration'])-10/1.2)<.15, data['format']
    assert any(s['codec_type']=='audio' for s in data['streams'])
    assert next(s for s in data['streams'] if s['codec_type']=='video')['width']==320
    samples=array.array('h',ff(['-i',output,'-ss','1','-t','3','-vn','-ac','1','-ar','16000','-f','s16le','pipe:1']))
    crossings=sum(a<=0<b for a,b in zip(samples,samples[1:]))
    hz=crossings/(len(samples)/16000)
    assert abs(hz-440)<5, hz  # atempo must keep pitch, not shift it to 528 Hz.
    assert progress[-1]==100
    second=speed_copy.create(str(source),1.2,FF,ENC,10)
    assert output!=second and Path(output).is_file()
    event=threading.Event()
    def cancel(_): event.set()
    try:
        speed_copy.create(str(source),1.5,FF,ENC,10,cancel,event)
        raise AssertionError('Cancel ignored')
    except speed_copy.Cancelled: pass
    assert hashlib.sha256(source.read_bytes()).digest()==digest
    assert not list(Path(tmp).glob('*.part.mp4'))
    assert not list(Path(tmp).glob('*1.5x*.mp4'))
    silent=Path(tmp)/'silent.mp4'
    ff(['-f','lavfi','-i','color=blue:s=320x180:r=25:d=1','-c:v','libx264',silent])
    assert Path(speed_copy.create(str(silent),1.2,FF,ENC,1)).is_file()
    for invalid in (1,0,2.1,float('nan'),None,'bad'):
        try:
            speed_copy.validate_speed(invalid)
            raise AssertionError('Invalid speed accepted')
        except ValueError: pass
    print('Speed copy: duration, pitch, silent video, unique files and cancellation passed')
