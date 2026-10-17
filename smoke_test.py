"""Exercise actual rendering in both source and frozen Windows applications."""
import json
import subprocess
import tempfile
from pathlib import Path


def run():
    from video_story_renderer import Renderer, ffmpeg_path, ffprobe_path
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError('Bundled FFmpeg is missing')
    probe = ffprobe_path(ffmpeg)
    if not probe:
        raise RuntimeError('Bundled ffprobe is missing')
    with tempfile.TemporaryDirectory(prefix='video_story_selftest_') as tmp:
        tmp = Path(tmp)
        image, audio = tmp / 'image.png', tmp / 'voice.wav'
        def execute(args):
            subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', *args], check=True, capture_output=True)
        execute(['-f', 'lavfi', '-i', 'color=c=blue:s=320x180', '-frames:v', '1', '-threads', '1', str(image)])
        execute(['-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.6', str(audio)])
        class Status:
            def set(self, value):
                pass
        class Harness:
            status = Status()
            error = None
            finished = False
            def after(self, delay, callback):
                callback()
            def finish_render(self, path, error):
                self.error = error
                self.finished = True
        for music in ('', str(audio)):
            output = tmp / ('with_music.mp4' if music else 'voice_only.mp4')
            harness = Harness()
            Renderer.render_worker(harness, ffmpeg, {
                'frames': [{'path': str(image), 'duration': 0.3}] * 2,
                'voice': str(audio), 'music': music, 'volume': 0.12,
                'output': str(output), 'zoom': True,
            })
            if not harness.finished or harness.error:
                raise RuntimeError(harness.error or 'Renderer did not complete')
            data = json.loads(subprocess.check_output([
                probe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(output)
            ], text=True))
            video = next(s for s in data['streams'] if s['codec_type'] == 'video')
            assert (video['width'], video['height']) == (1920, 1080)
            assert video['codec_name'] == 'h264'
            assert any(s['codec_type'] == 'audio' for s in data['streams'])
            assert abs(float(data['format']['duration']) - 0.6) < 0.1

if __name__ == '__main__':
    run()
