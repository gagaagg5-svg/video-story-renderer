import csv
import os
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from duration_utils import parse_duration, format_duration

APP_NAME = "Video Story Renderer"
FPS = 30
WIDTH = 1920
HEIGHT = 1080
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def ffmpeg_path():
    base = Path(__file__).resolve().parent
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for candidate in (base / executable, base / "bin" / executable):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("ffmpeg")
    if found:
        return found
    return None


def ffprobe_path(ffmpeg):
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    return str(sibling) if sibling.exists() else shutil.which("ffprobe")


def probe_duration(path, ffmpeg):
    probe = ffprobe_path(ffmpeg)
    if not probe:
        return None
    try:
        out = subprocess.check_output([
            probe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path)
        ], text=True, stderr=subprocess.STDOUT)
        return float(out.strip())
    except Exception:
        return None


def quote_concat(path):
    return str(path).replace("\\", "/").replace("'", "'\\''")


class Renderer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("900x670")
        self.minsize(760, 560)
        self.frames = []  # {path, duration}
        self.voice_path = tk.StringVar()
        self.music_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.music_volume = tk.DoubleVar(value=0.12)
        self.zoom_enabled = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Добавь изображения или загрузи CSV с таймингом.")
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="Сборка видеоистории", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(root, text="Кадры + озвучка + фоновая музыка → MP4 1920×1080", foreground="#555").pack(anchor="w", pady=(2, 10))

        controls = ttk.Frame(root)
        controls.pack(fill="x", pady=(0, 6))
        for label, command in [
            ("Добавить кадры", self.add_images), ("Загрузить CSV", self.load_csv),
            ("Сохранить CSV", self.save_csv), ("Удалить", self.remove_frame),
            ("↑ Выше", lambda: self.move_frame(-1)), ("↓ Ниже", lambda: self.move_frame(1)),
            ("Длительность", self.set_duration),
        ]:
            ttk.Button(controls, text=label, command=command).pack(side="left", padx=(0, 5))

        table_frame = ttk.Frame(root)
        table_frame.pack(fill="both", expand=True)
        cols = ("number", "image", "duration")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse", height=12)
        self.tree.heading("number", text="#")
        self.tree.heading("image", text="Изображение")
        self.tree.heading("duration", text="Длительность")
        self.tree.column("number", width=50, anchor="center", stretch=False)
        self.tree.column("image", width=600)
        self.tree.column("duration", width=150, anchor="center", stretch=False)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.set_duration())

        media = ttk.LabelFrame(root, text="Аудио и экспорт", padding=8)
        media.pack(fill="x", pady=(10, 0))
        self._file_row(media, "Озвучка", self.voice_path, self.pick_voice, 0)
        self._file_row(media, "Фоновая музыка (необязательно)", self.music_path, self.pick_music, 1)
        self._file_row(media, "Сохранить MP4 как", self.output_path, self.pick_output, 2)
        opts = ttk.Frame(media)
        opts.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        ttk.Checkbutton(opts, text="Плавный медленный зум", variable=self.zoom_enabled).pack(side="left")
        ttk.Label(opts, text="Громкость музыки:").pack(side="left", padx=(20, 5))
        ttk.Scale(opts, from_=0.02, to=0.35, variable=self.music_volume, orient="horizontal", length=140).pack(side="left")
        ttk.Label(opts, text="(относительно озвучки)", foreground="#666").pack(side="left", padx=5)
        self.render_button = ttk.Button(media, text="РЕНДЕРИТЬ ВИДЕО", command=self.start_render)
        self.render_button.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(9, 0), ipady=5)
        media.columnconfigure(1, weight=1)

        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.pack(fill="x", pady=(10, 3))
        ttk.Label(root, textvariable=self.status, wraplength=850).pack(anchor="w")

    def _file_row(self, parent, label, var, command, row):
        ttk.Label(parent, text=label, width=29).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(parent, text="Выбрать…", command=command).grid(row=row, column=2, pady=3)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, item in enumerate(self.frames, 1):
            self.tree.insert("", "end", iid=str(i-1), values=(i, Path(item["path"]).name, format_duration(item["duration"])))

    def add_images(self):
        paths = filedialog.askopenfilenames(title="Выбери кадры", filetypes=[("Изображения", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"), ("Все файлы", "*.*")])
        for path in paths:
            if Path(path).suffix.lower() in IMAGE_TYPES and path not in [f["path"] for f in self.frames]:
                self.frames.append({"path": path, "duration": 480.0})
        self.refresh()

    def selected_index(self):
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def remove_frame(self):
        i = self.selected_index()
        if i is not None:
            self.frames.pop(i)
            self.refresh()

    def move_frame(self, step):
        i = self.selected_index()
        j = i + step if i is not None else -1
        if i is not None and 0 <= j < len(self.frames):
            self.frames[i], self.frames[j] = self.frames[j], self.frames[i]
            self.refresh()
            self.tree.selection_set(str(j))

    def set_duration(self):
        i = self.selected_index()
        if i is None:
            messagebox.showinfo(APP_NAME, "Сначала выбери кадр в списке.")
            return
        value = simpledialog.askstring("Длительность кадра", "Введи секунды, ММ:СС или ЧЧ:ММ:СС (например, 08:00):", initialvalue=format_duration(self.frames[i]["duration"]))
        if value is not None:
            try:
                self.frames[i]["duration"] = parse_duration(value)
            except ValueError as exc:
                messagebox.showerror("Неверное время", str(exc))
                return
            self.refresh()
            self.tree.selection_set(str(i))

    def load_csv(self):
        path = filedialog.askopenfilename(title="Загрузить таблицу тайминга", filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            rows = []
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                sample = f.read(4096)
                f.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                reader = csv.DictReader(f, dialect=dialect)
                for row in reader:
                    name = (row.get("image") or row.get("filename") or row.get("file") or "").strip()
                    dur = (row.get("duration_seconds") or row.get("duration") or row.get("seconds") or "").strip()
                    if not name and not dur:
                        continue
                    if not name or not dur:
                        raise ValueError("В каждой строке CSV нужны изображение и длительность.")
                    img = Path(name)
                    if not img.is_absolute():
                        img = Path(path).parent / img
                    if not img.exists():
                        raise FileNotFoundError(f"Не найден кадр: {img}")
                    rows.append({"path": str(img.resolve()), "duration": parse_duration(dur)})
            if not rows:
                raise ValueError("В CSV не найдены строки с колонками image и duration_seconds.")
            self.frames = rows
            self.refresh()
            self.status.set(f"Загружено кадров: {len(rows)}")
        except Exception as e:
            messagebox.showerror("Ошибка чтения CSV", str(e))

    def save_csv(self):
        if not self.frames:
            messagebox.showinfo(APP_NAME, "Список кадров пуст.")
            return
        path = filedialog.asksaveasfilename(title="Сохранить тайминг", defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["image", "duration_seconds"])
                for item in self.frames:
                    w.writerow([item["path"], format_duration(item["duration"])])
            self.status.set(f"Таблица сохранена: {path}")

    def pick_voice(self):
        p = filedialog.askopenfilename(title="Выбери файл озвучки", filetypes=[("Аудио", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"), ("Все файлы", "*.*")])
        if p: self.voice_path.set(p)

    def pick_music(self):
        p = filedialog.askopenfilename(title="Выбери фоновую музыку", filetypes=[("Аудио", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"), ("Все файлы", "*.*")])
        if p: self.music_path.set(p)

    def pick_output(self):
        p = filedialog.asksaveasfilename(title="Куда сохранить готовое видео", defaultextension=".mp4", filetypes=[("Видео MP4", "*.mp4")])
        if p: self.output_path.set(p)

    def start_render(self):
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            messagebox.showerror("FFmpeg не найден", "Установи FFmpeg и добавь его в PATH. Инструкция есть в README.")
            return
        if not self.frames:
            messagebox.showerror(APP_NAME, "Добавь кадры или загрузи CSV с таймингом.")
            return
        if not self.voice_path.get() or not Path(self.voice_path.get()).exists():
            messagebox.showerror(APP_NAME, "Выбери файл озвучки.")
            return
        if not self.output_path.get():
            messagebox.showerror(APP_NAME, "Укажи, куда сохранить MP4.")
            return
        if self.music_path.get() and not Path(self.music_path.get()).exists():
            messagebox.showerror(APP_NAME, "Файл фоновой музыки не найден.")
            return
        if Path(self.output_path.get()).resolve() in [Path(i["path"]).resolve() for i in self.frames]:
            messagebox.showerror(APP_NAME, "Файл MP4 не должен перезаписывать один из кадров.")
            return
        sources = [Path(f["path"]).resolve() for f in self.frames]
        sources.append(Path(self.voice_path.get()).resolve())
        if self.music_path.get():
            sources.append(Path(self.music_path.get()).resolve())
        output = Path(self.output_path.get()).resolve()
        if output in sources:
            messagebox.showerror(APP_NAME, "Нельзя перезаписать исходный аудиофайл или изображение.")
            return
        if output.exists() and not messagebox.askyesno("Файл существует", "Заменить существующее видео после успешного рендера?"):
            return
        config = {
            "frames": [dict(f) for f in self.frames],
            "voice": self.voice_path.get(), "music": self.music_path.get(),
            "output": str(output), "zoom": self.zoom_enabled.get(),
            "volume": float(self.music_volume.get()),
        }
        self.render_button.configure(state="disabled")
        self.progress.start(10)
        self.status.set("Рендер начат. Длительность зависит от числа и размера кадров.")
        threading.Thread(target=self.render_worker, args=(ffmpeg, config), daemon=True).start()

    def render_worker(self, ffmpeg, config):
        temp_dir = None
        pending_output = None
        try:
            requested_total = sum(float(f["duration"]) for f in config["frames"])
            if requested_total <= 0:
                raise ValueError("Суммарная длительность кадров должна быть больше нуля.")
            total_frames = sum(max(1, round(float(f["duration"]) * FPS)) for f in config["frames"])
            total = total_frames / FPS
            audio_duration = probe_duration(config["voice"], ffmpeg)
            if audio_duration and audio_duration > requested_total + 1:
                raise ValueError(f"Озвучка длится {audio_duration/60:.1f} мин, а кадры рассчитаны на {requested_total/60:.1f} мин. Увеличь тайминг кадров.")
            temp_dir = tempfile.mkdtemp(prefix="video_story_render_")
            segments = []
            for i, item in enumerate(config["frames"]):
                frames_count = max(1, round(float(item["duration"]) * FPS))
                seg = os.path.join(temp_dir, f"scene_{i:04d}.mp4")
                if config["zoom"]:
                    zoom = f"zoompan=z='1+0.08*on/{max(1, frames_count - 1)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30"
                else:
                    zoom = "fps=30,scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080"
                vf = f"scale=2200:1238:force_original_aspect_ratio=increase,crop=2200:1238,{zoom},scale=1920:1080,setsar=1,format=yuv420p"
                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-i", item["path"], "-vf", vf, "-frames:v", str(frames_count), "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart", seg]
                self.after(0, lambda n=i+1, count=len(config["frames"]): self.status.set(f"Рендер кадра {n} из {count}…"))
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError(result.stderr[-2000:] or "Ошибка FFmpeg во время обработки кадра.")
                segments.append(seg)
            concat_file = os.path.join(temp_dir, "segments.txt")
            with open(concat_file, "w", encoding="utf-8") as f:
                for seg in segments:
                    f.write("file '" + quote_concat(seg) + "'\n")
            out = str(Path(config["output"]).resolve())
            os.makedirs(os.path.dirname(out), exist_ok=True)
            fd, pending_output = tempfile.mkstemp(prefix=".render_", suffix=".mp4", dir=os.path.dirname(out))
            os.close(fd)
            if config["music"]:
                vol = max(0.01, min(0.35, float(config["volume"])))
                audio_filter = f"[1:a]aresample=48000,apad,atrim=0:{total:.3f}[voice];[2:a]volume={vol},aresample=48000,atrim=0:{total:.3f}[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-i", config["voice"], "-stream_loop", "-1", "-i", config["music"], "-filter_complex", audio_filter, "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart", pending_output]
            else:
                audio_filter = f"[1:a]aresample=48000,apad,atrim=0:{total:.3f}[aout]"
                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-i", config["voice"], "-filter_complex", audio_filter, "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart", pending_output]
            self.after(0, lambda: self.status.set("Собираю сцены и синхронизирую звук…"))
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr[-3000:] or "Ошибка FFmpeg при сборке итогового MP4.")
            os.replace(pending_output, out)
            pending_output = None
            self.after(0, lambda: self.finish_render(out, None))
        except Exception as e:
            msg = str(e)
            self.after(0, lambda: self.finish_render(None, msg))
        finally:
            if pending_output:
                Path(pending_output).unlink(missing_ok=True)
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def finish_render(self, path, error):
        self.progress.stop()
        self.render_button.configure(state="normal")
        if error:
            self.status.set("Рендер завершился с ошибкой.")
            messagebox.showerror("Не удалось отрендерить видео", error)
        else:
            self.status.set(f"Готово: {path}")
            messagebox.showinfo("Видео готово", f"Видео сохранено:\n{path}")


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        from smoke_test import run
        run()
    else:
        Renderer().mainloop()
