"""Regression: FFprobe CSV can repeat a stream under programs/side metadata."""
import sys, subprocess, tempfile, math
from pathlib import Path
from types import ModuleType
ROOT=Path(__file__).resolve().parents[1]
ffmpeg=str(ROOT/'ffmpeg/ffmpeg.exe') if (ROOT/'ffmpeg/ffmpeg.exe').exists() else 'ffmpeg'
ffprobe=ffmpeg.replace('ffmpeg.exe','ffprobe.exe') if ffmpeg.endswith('.exe') else 'ffprobe'
fake=ModuleType('pipeline');fake.subprocess=subprocess;fake.NO_WINDOW=0;fake.FPS=25
fake.ffmpeg_bin=lambda name='ffmpeg':ffprobe if name=='ffprobe' else ffmpeg
fake.audio_duration=lambda p:float(subprocess.check_output([ffprobe,'-v','error','-show_entries','format=duration','-of','csv=p=0',str(p)]))
sys.modules['pipeline']=fake;sys.path.insert(0,str(ROOT))
import manual_images, narration_tracks
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp)/'Папка с видео';root.mkdir()
 for format in ('mp4','mpegts'):
  path=root/f'вступление_{format}.mp4'
  subprocess.run([ffmpeg,'-y','-v','error','-f','lavfi','-i','color=s=160x90:d=1',
    '-f','lavfi','-i','sine=frequency=500:duration=1','-c:v','libx264','-c:a','aac','-shortest','-f',format,str(path)],check=True)
  assert manual_images.intro_duration(str(path))>.9
  assert narration_tracks.plan({'tracks':[str(path)]})['duration']>.9
 # An audio-only file must still be rejected as intro.
 audio=root/'только_звук.mp4'
 subprocess.run([ffmpeg,'-y','-v','error','-f','lavfi','-i','sine=duration=1','-c:a','aac',str(audio)],check=True)
 try:manual_images.intro_duration(str(audio));raise AssertionError('audio-only accepted')
 except ValueError as e:assert 'не найдена видеодорожка' in str(e)
if len(sys.argv)>1:
 assert manual_images.intro_duration(sys.argv[1])>0
print('MP4, program metadata, Cyrillic names, audio tracks and missing-video checks passed')
