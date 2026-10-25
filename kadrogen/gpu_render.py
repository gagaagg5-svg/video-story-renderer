"""Single-pass image timeline: OpenGL composition -> one FFmpeg encode.

Only the current and next image live on the GPU. No intermediate scene videos,
full-length transition encode or separate film pass. Raw frames cross to FFmpeg
through RAM; this is deliberately not advertised as zero-copy NVENC.
"""
import math
import json
import os
import random
import struct
import subprocess
import sys
import tempfile
import threading
import time
from fractions import Fraction

from PIL import Image, ImageDraw
import pipeline


VERTEX = """
#version 330
out vec2 uv;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""
FRAGMENT = """
#version 330
uniform sampler2D current_image;
uniform sampler2D next_image;
uniform sampler2D overlay_image;
uniform float overlay_opacity;
uniform vec3 crop_a;
uniform vec3 crop_b;
uniform float progress;
uniform int transition;
in vec2 uv;
out vec4 color;
vec4 a(vec2 p) { return texture(current_image, crop_a.xy + p * crop_a.z); }
vec4 b(vec2 p) { return texture(next_image, crop_b.xy + p * crop_b.z); }
void scene() {
    if (progress <= 0.0) { color = a(uv); return; }
    float p = progress * progress * (3.0 - 2.0 * progress);
    if (transition == 0) {
        // Incoming image expands from the centre; no dissolve.
        vec2 q = (uv - 0.5) / max(p, 0.00001) + 0.5;
        if (all(greaterThanEqual(q, vec2(0.0))) && all(lessThanEqual(q, vec2(1.0))))
            color = b(q);
        else color = a((uv - 0.5) / (1.0 + 0.12 * p) + 0.5);
    } else {
        vec2 direction = transition == 1 ? vec2(1, 0) :
                         transition == 2 ? vec2(-1, 0) :
                         transition == 3 ? vec2(0, 1) : vec2(0, -1);
        vec2 q = uv + direction * p;
        if (all(greaterThanEqual(q, vec2(0.0))) && all(lessThanEqual(q, vec2(1.0))))
            color = a(q);
        else color = b(uv + direction * (p - 1.0));
    }
}
void main() {
    scene();
    if (overlay_opacity > 0.0) {
        vec3 effect = texture(overlay_image, uv).rgb * overlay_opacity;
        color.rgb = effect + color.rgb * (1.0 - effect);
    }
}
"""
DUST_VERTEX = """
#version 330
in vec4 box;
in vec3 style;
out vec2 uv;
out vec3 appearance;
void main() {
    vec2 p = vec2(gl_VertexID & 1, (gl_VertexID >> 1) & 1);
    uv = vec2((p.x + style.z) / 2.0, p.y);
    appearance = style;
    gl_Position = vec4((box.xy + p * box.zw) * 2.0 - 1.0, 0.0, 1.0);
}
"""
DUST_FRAGMENT = """
#version 330
uniform sampler2D atlas;
in vec2 uv;
in vec3 appearance;
out vec4 color;
void main() {
    color = vec4(vec3(appearance.y), texture(atlas, uv).a * appearance.x);
}
"""


def frame_plan(scenes, fps, motion):
    """Round cumulative boundaries, so timing error never accumulates per image."""
    from decimal import Decimal
    boundaries, seconds = [0], Decimal(0)
    for scene in scenes:
        seconds += Decimal(str(scene["duration"]))
        boundaries.append(round(seconds * fps))
    if any(b <= a for a, b in zip(boundaries, boundaries[1:])):
        raise ValueError("Длительность кадра меньше одного видеокадра.")
    lengths = [b - a for a, b in zip(boundaries, boundaries[1:])]
    overlaps = [0]
    for i in range(1, len(scenes)):
        overlaps.append(min(round(0.5 * fps), lengths[i - 1] // 2, lengths[i] // 2) if motion else 0)
    return boundaries, overlaps


def crop_for(scene, frame, total_frames, fps):
    if not scene.get("zoom"):
        return (0.0, 0.0, 1.0)
    seconds = float(scene.get("zoom_seconds", 18))
    denominator = max(1, total_frames - 1) if total_frames / fps <= seconds else fps * seconds
    zoom = 1.0 + 0.06 * (1.0 - math.cos(math.pi * frame / denominator))
    size = 1.0 / zoom
    fx, fy = scene.get("focus", (0.5, 0.5))
    return (max(0.0, min(1.0 - size, fx - size / 2)),
            max(0.0, min(1.0 - size, fy - size / 2)), size)


class Composer:
    def __init__(self, width, height, film="none", allow_software=False, film_intensity=50):
        import moderngl
        self.gl = moderngl
        kwargs = {"backend": "egl"} if sys.platform.startswith("linux") else {}
        self.ctx = moderngl.create_context(standalone=True, require=330, **kwargs)
        self.renderer = self.ctx.info.get("GL_RENDERER", "OpenGL")
        try:
            if not allow_software and any(s in self.renderer.lower() for s in
                                          ("llvmpipe", "softpipe", "software", "gdi generic", "basic render")):
                raise RuntimeError("OpenGL доступен только в программном режиме: " + self.renderer)
            self.width, self.height = width, height
            self.fbo = self.ctx.simple_framebuffer((width, height), components=3)
            self.fbo.use()
            self.program = self.ctx.program(vertex_shader=VERTEX, fragment_shader=FRAGMENT)
            self.vao = self.ctx.vertex_array(self.program, [])
            self.program["current_image"] = 0
            self.program["next_image"] = 1
            self.program["overlay_image"] = 3
            self.blank_overlay = self.ctx.texture((1, 1), 3, bytes(3))
            self.blank_overlay.use(3)
            self.textures = {}
            self.particles = []
            self.overlay_texture = None
            self.overlay_cache = []
            self.overlay_cache_bytes = 0
            self.film_gain = max(0.0, min(100.0, float(film_intensity))) / 50.0
            if film != "none" and self.film_gain > 0:
                self._init_dust(film)
            self.pixels = bytearray(width * height * 3)
        except BaseException:
            self.ctx.release()
            raise

    def _init_dust(self, film):
        rng = random.Random(42319)
        for i in range(48 if film == "light" else 76):
            self.particles.append((rng.uniform(15, 945), rng.uniform(25, 515), rng.random(),
                                   rng.uniform(-9, 9), rng.uniform(12, 28),
                                   rng.randint(60, 95) if film == "light" else rng.randint(85, 125),
                                   int(i % 6 == 0), rng.choice((15, 235, 250)),
                                   rng.uniform(5, 9), rng.uniform(12, 26)))
        atlas = Image.new("RGBA", (64, 32))
        draw = ImageDraw.Draw(atlas)
        draw.ellipse((8, 8, 24, 24), fill=(255, 255, 255, 255))
        draw.line([(40, 2), (48, 10), (44, 20), (52, 30)], fill=(255, 255, 255, 255), width=3)
        self.atlas = self.ctx.texture(atlas.size, 4, atlas.tobytes())
        self.atlas.filter = (self.gl.LINEAR, self.gl.LINEAR)
        self.atlas.repeat_x = self.atlas.repeat_y = False
        self.dust_program = self.ctx.program(vertex_shader=DUST_VERTEX, fragment_shader=DUST_FRAGMENT)
        self.dust_program["atlas"] = 2
        self.dust_buffer = self.ctx.buffer(reserve=len(self.particles) * 7 * 4)
        self.dust_vao = self.ctx.vertex_array(self.dust_program,
                                             [(self.dust_buffer, "4f 3f /i", "box", "style")])

    def image(self, key, path):
        if key not in self.textures:
            with Image.open(path) as source:
                picture = pipeline._fit_16_9(source.convert("RGB"))
                # Upload once, sample at fractional coordinates on the GPU each frame.
                picture = picture.resize((self.width * 2, self.height * 2), Image.Resampling.LANCZOS)
                texture = self.ctx.texture(picture.size, 3, picture.tobytes(), alignment=1)
            texture.build_mipmaps()
            texture.filter = (self.gl.LINEAR_MIPMAP_LINEAR, self.gl.LINEAR)
            texture.repeat_x = texture.repeat_y = False
            self.textures[key] = texture
        return self.textures[key]

    def video(self, key, pixels):
        if key not in self.textures:
            texture = self.ctx.texture((self.width, self.height), 3)
            texture.filter = (self.gl.LINEAR, self.gl.LINEAR)
            texture.repeat_x = texture.repeat_y = False
            self.textures[key] = texture
        self.textures[key].write(pixels, alignment=1)
        return self.textures[key]

    def keep(self, keys):
        for key in list(self.textures):
            if key not in keys:
                self.textures.pop(key).release()

    def cache_overlay(self, overlay, progress, budget=1024 * 1024 * 1024):
        """Decode a short CFR source once; reuse full-resolution GPU textures.

        Keep the native source cadence (including duplicate frames at slow
        playback). Long clips or failed allocations retain the streaming path.
        Count four GPU bytes per pixel conservatively even though input is RGB.
        """
        process = None
        done = threading.Event()
        watcher = None
        try:
            executable = pipeline.ffmpeg_bin()
            probe = os.path.join(os.path.dirname(executable), 'ffprobe.exe') if executable.lower().endswith('.exe') else 'ffprobe'
            data = json.loads(subprocess.check_output(
                [probe, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                 'stream=avg_frame_rate,r_frame_rate,duration:format=duration', '-of', 'json', overlay['path']],
                creationflags=pipeline.NO_WINDOW, timeout=15))
            stream = data['streams'][0]
            rate = Fraction(stream['avg_frame_rate'])
            if rate <= 0 or rate > 120 or rate != Fraction(stream['r_frame_rate']):
                return False
            duration = float(stream.get('duration') or data['format']['duration'])
            limit = budget // (self.width * self.height * 4)
            if not math.isfinite(duration) or duration <= 0 or math.ceil(duration * float(rate)) > limit:
                return False
            self.overlay_rate = float(rate)
            self.overlay_speed = overlay['speed']
            progress('render', 0, 0, 'готовлю короткий футаж в памяти видеокарты · без уменьшения разрешения')
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen(
                    [executable, '-v', 'error', '-i', overlay['path'], '-vf',
                     f'scale={self.width}:{self.height},setsar=1,fps={rate}',
                     '-an', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                    stdout=subprocess.PIPE, stderr=log, creationflags=pipeline.NO_WINDOW)
                def watch():
                    while not done.wait(.1):
                        if pipeline.CANCEL.is_set():
                            _stop(process)
                            return
                watcher = threading.Thread(target=watch, daemon=True)
                watcher.start()
                while True:
                    pipeline.check_cancel()
                    pixels = process.stdout.read(self.width * self.height * 3)
                    if not pixels:
                        break
                    if len(pixels) != self.width * self.height * 3 or len(self.overlay_cache) >= limit:
                        raise RuntimeError('Футаж не помещается в кэш')
                    texture = self.ctx.texture((self.width, self.height), 3, pixels, alignment=1)
                    texture.filter = (self.gl.LINEAR, self.gl.LINEAR)
                    texture.repeat_x = texture.repeat_y = False
                    self.overlay_cache.append(texture)
                if process.wait(timeout=30) != 0 or not self.overlay_cache:
                    raise RuntimeError('Не удалось подготовить футаж')
            self.overlay_cache_bytes = len(self.overlay_cache) * self.width * self.height * 4
            progress('render', 0, 0, f'футаж загружен в GPU · {len(self.overlay_cache)} кадров · повтор без чтения файла')
            return True
        except Exception:
            for texture in self.overlay_cache:
                texture.release()
            self.overlay_cache.clear()
            self.overlay_cache_bytes = 0
            pipeline.check_cancel()
            return False
        finally:
            done.set()
            _stop(process)
            if process and process.stdout:
                process.stdout.close()
            if watcher:
                watcher.join(timeout=2)

    def draw(self, current, following, crop_a, crop_b, progress, transition, seconds,
             overlay_pixels=None, overlay_opacity=0.3):
        current.use(0)
        following.use(1)
        self.program["crop_a"] = crop_a
        self.program["crop_b"] = crop_b
        self.program["progress"] = progress
        self.program["transition"] = transition
        self.program["overlay_opacity"] = 0.0
        if (overlay_pixels is not None or self.overlay_cache) and overlay_opacity > 0:
            if self.overlay_texture is None:
                self.overlay_texture = self.ctx.texture((self.width, self.height), 3) if not self.overlay_cache else self.overlay_cache[0]
            if self.overlay_cache:
                # Native frames are held at slower speeds and skipped at faster
                # speeds; the cache loops independently of image boundaries.
                # Match FFmpeg fps's nearest timestamp rounding (half ties go
                # forward), including non-integer speeds such as 1.2x.
                index = max(0, math.ceil((seconds + .5 / pipeline.FPS) * self.overlay_speed *
                                         self.overlay_rate - 1e-8) - 1) % len(self.overlay_cache)
                self.overlay_cache[index].use(3)
            else:
                self.overlay_texture.write(overlay_pixels, alignment=1)
                self.overlay_texture.use(3)
            self.program["overlay_opacity"] = overlay_opacity
        # One framebuffer write: image, motion, transition and footage are
        # composed together. Avoid a second fullscreen draw and blend barrier.
        self.vao.render(vertices=3)
        if self.particles:
            data = []
            for x0, y0, offset, dx, dy, opacity, fibre, value, size, length in self.particles:
                phase = (seconds / 8.0 + offset) % 1.0
                alpha = min(1.0, opacity / 255.0 * self.film_gain) * math.sin(math.pi * phase) ** 2
                data.extend(((x0 + dx * (phase - 0.5)) / 960,
                             (y0 + dy * (phase - 0.5)) / 540,
                             (10 if fibre else size) / 960, (length if fibre else size) / 540,
                             alpha, value / 255.0, float(fibre)))
            self.dust_buffer.write(struct.pack(f"{len(data)}f", *data))
            self.atlas.use(2)
            self.ctx.enable(self.gl.BLEND)
            self.ctx.blend_func = self.gl.SRC_ALPHA, self.gl.ONE_MINUS_SRC_ALPHA
            self.dust_vao.render(mode=self.gl.TRIANGLE_STRIP, vertices=4, instances=len(self.particles))
            self.ctx.disable(self.gl.BLEND)
        # Texture rows and framebuffer rows both use the same top-first convention
        # here; do not flip this readback or the exported images become upside down.
        self.fbo.read_into(self.pixels, components=3, alignment=1)
        return self.pixels

    def close(self):
        self.ctx.release()


def _stop(process):
    if process and process.poll() is None:
        process.kill()
        process.wait(timeout=10)


def render(scenes, img_dir, videos, audio, work_dir, output, progress,
           motion=False, film="none", allow_software=False, film_intensity=50, overlay=None,
           overlay_cache_budget=1024 * 1024 * 1024):
    """Returns render metrics. Raises on failure so the caller can use FFmpeg fallback."""
    started = time.perf_counter()
    width, height, fps = pipeline.W, pipeline.H, pipeline.FPS
    boundaries, overlaps = frame_plan(scenes, fps, motion)
    total = boundaries[-1]
    composer = Composer(width, height, film, allow_software=allow_software, film_intensity=film_intensity)
    encoder = decoder = overlay_decoder = None
    stopped = threading.Event()
    tmp = output + ".gpu.tmp.mp4"
    # Stderr must never fill a pipe while we write raw frames to stdin.
    encoder_log = tempfile.TemporaryFile()
    decoder_log = tempfile.TemporaryFile()
    overlay_log = tempfile.TemporaryFile()
    watcher = None
    try:
        cache_started = time.perf_counter()
        cached_overlay = bool(overlay and overlay.get('cache', True) and overlay['opacity'] > 0 and overlay_cache_budget > 0 and
                              composer.cache_overlay(overlay, progress, overlay_cache_budget))
        cache_seconds = time.perf_counter() - cache_started
        enc = pipeline.video_encoder_args()
        encoder_name = enc[enc.index("-c:v") + 1]
        film_label = f" · плёнка: {film}, {film_intensity:g}%" if film != "none" else ""
        progress("render", 0, total, f"эффекты: {composer.renderer} · кодирование: {encoder_name} · один проход{film_label}")
        args = [pipeline.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
                "-r", str(fps), "-i", "pipe:0", "-i", audio,
                "-map", "0:v:0", "-map", "1:a:0", *enc,
                "-vf", "scale=in_range=pc:out_range=tv:out_color_matrix=bt709,setsar=1",
                "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-af", "apad", "-t", f"{total / fps:.8f}",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", tmp]
        encoder = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=encoder_log, creationflags=pipeline.NO_WINDOW)
        if overlay and overlay["opacity"] > 0 and not cached_overlay:
            vf = (f"setpts=(PTS-STARTPTS)/{overlay['speed']:.8f},"
                  f"scale={width}:{height},setsar=1,fps={fps}")
            overlay_decoder = subprocess.Popen(
                [pipeline.ffmpeg_bin(), "-v", "error", "-stream_loop", "-1", "-i", overlay["path"],
                 "-vf", vf, "-frames:v", str(total), "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                stdout=subprocess.PIPE, stderr=overlay_log, creationflags=pipeline.NO_WINDOW)
            progress("render", 0, total, f"футаж: {os.path.basename(overlay['path'])} · {overlay['opacity']:.0%} · {overlay['speed']:g}× · один проход")
        if videos:
            if set(videos) != {scenes[0]["index"]}:
                raise ValueError("GPU-путь поддерживает видео только в начале.")
            vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                  f"setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={boundaries[1] / fps + 1}")
            decoder = subprocess.Popen(
                [pipeline.ffmpeg_bin(), "-v", "error", "-i", videos[scenes[0]["index"]],
                 "-vf", vf, "-frames:v", str(boundaries[1]), "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                stdout=subprocess.PIPE, stderr=decoder_log, creationflags=pipeline.NO_WINDOW)

        def cancel_pipes():
            while not stopped.wait(0.1):
                if pipeline.CANCEL.is_set():
                    for process in (encoder, decoder, overlay_decoder):
                        try:
                            _stop(process)
                        except OSError:
                            pass
                    return

        watcher = threading.Thread(target=cancel_pipes, daemon=True)
        watcher.start()
        reported = 0.0
        timings = {"image_upload_seconds": 0.0, "compose_readback_seconds": 0.0,
                   "encoder_pipe_seconds": 0.0, "overlay_stream_read_seconds": 0.0}
        for i, scene in enumerate(scenes):
            pipeline.check_cancel()
            key = scene["index"]
            next_key = scenes[i + 1]["index"] if i + 1 < len(scenes) else key
            prepared_at = time.perf_counter()
            composer.keep({key, next_key})
            next_texture = (composer.image(next_key, os.path.join(img_dir, f"{next_key:04d}.png"))
                            if i + 1 < len(scenes) else None)
            texture = None if key in videos else composer.image(key, os.path.join(img_dir, f"{key:04d}.png"))
            timings["image_upload_seconds"] += time.perf_counter() - prepared_at
            length = boundaries[i + 1] - boundaries[i]
            incoming = overlaps[i]
            outgoing = overlaps[i + 1] if i + 1 < len(scenes) else 0
            for local in range(length):
                pipeline.check_cancel()
                if key in videos:
                    pixels = decoder.stdout.read(width * height * 3)
                    if len(pixels) != width * height * 3:
                        raise RuntimeError("Не удалось прочитать вступительное видео.")
                    texture = composer.video(key, pixels)
                transition_frame = local - (length - outgoing)
                fraction = transition_frame / outgoing if outgoing and transition_frame >= 0 else 0.0
                crop_a = crop_for(scene, local + incoming, length + incoming, fps)
                crop_b = (crop_for(scenes[i + 1], max(0, transition_frame),
                                   boundaries[i + 2] - boundaries[i + 1] + outgoing, fps)
                          if outgoing else (0.0, 0.0, 1.0))
                frame = boundaries[i] + local
                overlay_pixels = None
                if overlay_decoder:
                    overlay_read_at = time.perf_counter()
                    overlay_pixels = overlay_decoder.stdout.read(width * height * 3)
                    if len(overlay_pixels) != width * height * 3:
                        raise RuntimeError("Не удалось прочитать футаж. Проверьте видеофайл эффекта.")
                    timings["overlay_stream_read_seconds"] += time.perf_counter() - overlay_read_at
                compose_at = time.perf_counter()
                pixels = composer.draw(texture, next_texture or texture, crop_a, crop_b,
                                       fraction, i % 5, frame / fps, overlay_pixels,
                                       overlay["opacity"] if overlay else 0)
                timings["compose_readback_seconds"] += time.perf_counter() - compose_at
                pipe_at = time.perf_counter()
                encoder.stdin.write(pixels)
                now = time.perf_counter()
                timings["encoder_pipe_seconds"] += now - pipe_at
                if now - reported >= 1.0:
                    elapsed = now - started
                    speed = (frame + 1) / fps / max(elapsed, 0.001)
                    remaining = (total - frame - 1) / fps / max(speed, 0.001)
                    progress("render", frame + 1, total,
                             f"один проход · {speed:.1f}× · осталось примерно {remaining / 60:.1f} мин")
                    reported = now
            if key in videos:
                decoder.stdout.close()
                if decoder.wait(timeout=30) != 0:
                    raise RuntimeError("Ошибка чтения вступительного видео.")
        finish_at = time.perf_counter()
        encoder.stdin.close()
        if overlay_decoder:
            overlay_decoder.stdout.close()
            if overlay_decoder.wait(timeout=30) != 0:
                raise RuntimeError("Ошибка чтения футажа.")
        if encoder.wait(timeout=120) != 0:
            raise RuntimeError("Ошибка кодирования GPU-рендера.")
        timings["encoder_finish_seconds"] = time.perf_counter() - finish_at
        pipeline.check_cancel()
        os.replace(tmp, output)
        metrics = {"backend": "opengl-single-pass", "graphics": composer.renderer,
                   "film_effect": film, "film_intensity": film_intensity,
                   "overlay": overlay,
                   "composition": "fused-overlay-shader",
                   "timings": {k: round(v, 3) for k, v in timings.items()},
                   "overlay_backend": 'gpu-cache' if cached_overlay else 'stream' if overlay_decoder else 'none',
                   "overlay_cache_frames": len(composer.overlay_cache),
                   "overlay_cache_bytes": composer.overlay_cache_bytes,
                   "overlay_prepare_seconds": round(cache_seconds, 3),
                   "encoder": encoder_name, "frames": total, "video_seconds": total / fps,
                   "elapsed_seconds": round(time.perf_counter() - started, 3)}
        progress("render", total, total,
                 f"замеры: картинки {timings['image_upload_seconds']:.1f} с · композиция и чтение GPU {timings['compose_readback_seconds']:.1f} с · "
                 f"передача кодировщику {timings['encoder_pipe_seconds']:.1f} с · завершение {timings['encoder_finish_seconds']:.1f} с")
        pipeline.write_json(os.path.join(work_dir, "render_metrics.json"), metrics)
        progress("render", total, total, f"готово за {metrics['elapsed_seconds']:.1f} с · один проход")
        return metrics
    except Exception as exc:
        pipeline.check_cancel()
        encoder_log.seek(0)
        decoder_log.seek(0)
        overlay_log.seek(0)
        details = (encoder_log.read() + decoder_log.read() + overlay_log.read()).decode(errors="replace")[-1200:]
        raise RuntimeError(f"{exc} {details}".strip()) from exc
    finally:
        stopped.set()
        for process in (encoder, decoder, overlay_decoder):
            _stop(process)
            if process:
                for stream in (process.stdin, process.stdout):
                    if stream and not stream.closed:
                        try:
                            stream.close()
                        except OSError:
                            pass
        if watcher:
            watcher.join(timeout=2)
        encoder_log.close()
        decoder_log.close()
        overlay_log.close()
        composer.close()
        if os.path.exists(tmp):
            os.remove(tmp)

