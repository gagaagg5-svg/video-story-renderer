"""Real FFmpeg mixed timeline: order, silent animation, exact cuts and preview."""
import ast, concurrent.futures as cf, importlib.util, json, os, subprocess, sys, tempfile, threading
from pathlib import Path
from types import ModuleType
from PIL import Image, ImageOps
ROOT = Path(__file__).resolve().parents[1]
fake = ModuleType('pipeline')
ffmpeg = str(ROOT/'ffmpeg/ffmpeg.exe') if (ROOT/'ffmpeg/ffmpeg.exe').exists() else 'ffmpeg'
ffprobe = ffmpeg.replace('ffmpeg.exe','ffprobe.exe') if ffmpeg.endswith('.exe') else 'ffprobe'
fake.W, fake.H, fake.FPS = 320,180,25
fake.NO_WINDOW=0;fake.CANCEL=threading.Event()
fake.check_cancel=lambda:None
fake.ffmpeg_bin=lambda name='ffmpeg':ffprobe if name=='ffprobe' else ffmpeg
fake.subprocess=subprocess
fake.log=lambda *a:None
fake.Progress=lambda cb: cb or (lambda *a:None)
fake.short_base=lambda s,keep=25:s[:keep]
fake.read_json=lambda p:json.loads(Path(p).read_text()) if Path(p).exists() else None
fake.write_json=lambda p,v:Path(p).write_text(json.dumps(v))
fake.video_encoder_args=lambda:['-c:v','libx264','-preset','ultrafast','-crf','18','-pix_fmt','yuv420p']
fake.run_ffmpeg=lambda args:subprocess.run([ffmpeg,'-y','-v','error',*map(str,args)],check=True)
fake.audio_duration=lambda p:float(subprocess.check_output([ffprobe,'-v','error','-show_entries','format=duration','-of','csv=p=0',str(p)]))
fake._fit_16_9=lambda im:ImageOps.fit(im,(320,180))
fake.cleanup_job=lambda *a:None
sys.modules['pipeline']=fake
sys.path.insert(0,str(ROOT))
import manual_images as m
ns=dict(vars(fake),cf=cf,os=os,Image=Image,ImageOps=ImageOps,EDGE_CROP=0,TRANSITION=.5,XFADE_GROUP=30,_NVENC_AVAILABLE=False,_NVENC_ERROR='test')
tree=ast.parse((ROOT/'pipeline.py').read_text(encoding='utf-8'))
names={'assemble_video','render_scene_clip','render_video_clip'}
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),'pipeline.py','exec'),ns)
fake.assemble_video=ns['assemble_video']
# Force CPU fallback for this test; GPU mixed playback is verified separately.
sys.modules['gpu_render']=ModuleType('gpu_render')
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp);media=root/'media';media.mkdir();fake.data_dir=lambda:tmp
 Image.new('RGB',(320,180),'red').save(media/'01.png')
 Image.new('RGB',(320,180),'blue').save(media/'03.png')
 anim=media/'02.mp4'
 fake.run_ffmpeg(['-f','lavfi','-i','color=c=lime:s=320x180:r=25:d=1','-f','lavfi','-i','sine=frequency=1700:duration=1','-c:v','libx264','-c:a','aac','-shortest',anim])
 audio=root/'voice.wav';fake.run_ffmpeg(['-f','lavfi','-i','anullsrc=r=44100:cl=mono','-t','3',audio])
 timing=root/'timing.csv';timing.write_text('filename,duration_seconds\n01.png,1\n02.mp4,1\n03.png,1\n')
 paths=m.list_images(str(media));assert len(paths)==3
 ordered,durations,_=m.resolve_full_timing(paths,str(timing),3);assert durations==[1,1,1]
 try:m.resolve_full_timing(paths,None,3);raise AssertionError('missing timing accepted')
 except ValueError:pass
 assert m.image_preview(str(media),1)['animation']
 output=m.make_video(str(audio),str(media),motion=True,timing_file=str(timing))
 # make_video returns path (or result tuple depending on version).
 assert isinstance(output,str),output
 assert abs(fake.audio_duration(output)-3)<.1
 raw=subprocess.check_output([ffmpeg,'-v','error','-i',output,'-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
 size=320*180*3;assert len(raw)//size==75
 for frame,channel in [(0,0),(24,0),(25,1),(49,1),(50,2),(74,2)]:
  im=Image.frombytes('RGB',(320,180),raw[frame*size:(frame+1)*size]);pixel=im.getpixel((160,90));assert pixel[channel]>200,(frame,pixel)
 pcm=subprocess.check_output([ffmpeg,'-v','error','-i',output,'-f','s16le','-ac','1','pipe:1'])
 import array
 assert max(map(abs,array.array('h',pcm)))<10,'source animation sound leaked'
 # XML with the same mixed order, including filename references.
 xml=root/'timing.xml';xml.write_text('<xmeml><sequence><rate><timebase>25</timebase></rate><media><video><track>'+''.join(f'<clipitem><start>{i*25}</start><end>{(i+1)*25}</end><in>0</in><file><name>{name}</name></file></clipitem>' for i,name in enumerate(['01.png','02.mp4','03.png']))+'</track></video></media></sequence></xmeml>')
 assert m.resolve_full_timing(paths,str(xml),3)[1]==[1,1,1]
print('Timed animation import, CPU rendering, boundary and mute checks passed')
