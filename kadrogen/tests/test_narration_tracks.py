import array, json, os, subprocess, sys, tempfile, threading
from pathlib import Path
from types import ModuleType
ROOT=Path(__file__).resolve().parents[1]
ffmpeg=str(ROOT/'ffmpeg/ffmpeg.exe') if (ROOT/'ffmpeg/ffmpeg.exe').exists() else 'ffmpeg'
ffprobe=ffmpeg.replace('ffmpeg.exe','ffprobe.exe') if ffmpeg.endswith('.exe') else 'ffprobe'
fake=ModuleType('pipeline');fake.NO_WINDOW=0;fake.CANCEL=threading.Event()
class Cancelled(Exception):pass
def check():
 if fake.CANCEL.is_set():raise Cancelled()
fake.check_cancel=check;fake.ffmpeg_bin=lambda name='ffmpeg':ffprobe if name=='ffprobe' else ffmpeg
fake.audio_duration=lambda p:float(subprocess.check_output([ffprobe,'-v','error','-show_entries','format=duration','-of','csv=p=0',str(p)]))
sys.modules['pipeline']=fake;sys.path.insert(0,str(ROOT))
import narration_tracks as n
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp);fake.data_dir=lambda:tmp
 paths=[]
 for i,freq in enumerate((400,900)):
  p=root/f'act {i}.wav';paths.append(str(p))
  subprocess.run([ffmpeg,'-y','-v','error','-f','lavfi','-i',f'sine=frequency={freq}:duration=1','-ar','44100' if i else '48000','-ac','2' if i else '1',str(p)],check=True)
 original=[Path(p).read_bytes() for p in paths]
 sequence=n.plan({'tracks':paths});assert sequence['duration']==2
 result=n.prepare(sequence,lambda *a:None)
 assert abs(fake.audio_duration(result)-2)<.001
 assert n.prepare(sequence,lambda *a:None)==result
 csv=root/'voice.csv';csv.write_text('filename,start\nact 1.wav,00:00:03\nact 0.wav,00:00:01\n')
 planned=n.plan({'tracks':paths,'mode':'timing','timing':str(csv)})
 assert planned['duration']==4
 result=n.prepare(planned,lambda *a:None)
 pcm=array.array('h',subprocess.check_output([ffmpeg,'-v','error','-i',result,'-f','s16le','-ac','1','-ar','48000','pipe:1']))
 assert len(pcm)==4*48000,len(pcm)
 for start,active in [(0,False),(1,True),(2,False),(3,True)]:
  samples=pcm[int((start+.1)*48000):int((start+.9)*48000)]
  assert (max(map(abs,samples))>500)==active
  if active:
   crossings=sum(a<=0<b for a,b in zip(samples,samples[1:]));assert abs(crossings/.8-(400 if start==1 else 900))<4
 assert [Path(p).read_bytes() for p in paths]==original
 csv.write_text('filename,start\nact 0.wav,0\nact 1.wav,0.5\n')
 try:n.plan({'tracks':paths,'mode':'timing','timing':str(csv)});raise AssertionError('overlap accepted')
 except ValueError:pass
 for value in ('NaN','-1','01:70','inf'):
  try:n.seconds(value);raise AssertionError(value)
  except ValueError:pass
 fake.CANCEL.set()
 try:n.prepare(sequence,lambda *a:None);raise AssertionError('cancellation ignored')
 except Cancelled:pass
 fake.CANCEL.clear()
print('Narration sequence, timed gaps, order, sample positions, source preservation and cancellation passed')
