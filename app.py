"""GoPro Auto Slow - converte vídeos de alto FPS (ex.: 240 fps) em slow motion.

Nenhum frame é removido: todos os quadros são mantidos e apenas reposicionados
no tempo para uma taxa fixa de FPS (padrão 60). Um vídeo de 240 fps exportado
a 60 fps fica com 4x a duração original.
"""
from __future__ import annotations

import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "GoPro Auto Slow"
DEFAULT_FPS = "60"
FPS_OPTIONS = ["24", "25", "30", "50", "60", "120"]
TOLERANCE = 1.01  # vídeos com fps <= alvo * TOLERANCE são ignorados
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# chave -> (rótulo, argumentos do encoder, pix_fmt, é GPU)
ENCODERS: dict[str, tuple[str, list[str], str, bool]] = {
    "h264_nvenc": ("GPU NVIDIA (NVENC)",
                   ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "21", "-b:v", "0"],
                   "yuv420p", True),
    "h264_qsv": ("GPU Intel (QuickSync)",
                 ["-c:v", "h264_qsv", "-preset", "faster", "-global_quality", "21"],
                 "nv12", True),
    "h264_amf": ("GPU AMD (AMF)",
                 ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "21", "-qp_p", "21"],
                 "yuv420p", True),
    "h264_videotoolbox": ("GPU Apple (VideoToolbox)",
                          ["-c:v", "h264_videotoolbox", "-q:v", "65"],
                          "yuv420p", True),
    "x264_fast": ("CPU rápido (x264)",
                  ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"], "yuv420p", False),
    "x264_quality": ("CPU alta qualidade (x264, lento)",
                     ["-c:v", "libx264", "-preset", "medium", "-crf", "18"], "yuv420p", False),
}
GPU_ORDER = {
    "win32": ["h264_nvenc", "h264_qsv", "h264_amf"],
    "darwin": ["h264_videotoolbox"],
}.get(sys.platform, ["h264_nvenc"])


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #
def find_ffmpeg() -> str | None:
    """Usa o ffmpeg embutido (imageio-ffmpeg); se não houver, o do PATH."""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


@dataclass
class VideoInfo:
    path: Path
    fps: float | None = None
    duration: float | None = None
    has_audio: bool = False


def probe(ffmpeg: str, path: Path) -> VideoInfo:
    """Lê fps/duração/áudio a partir da saída de `ffmpeg -i` (não precisa de ffprobe)."""
    info = VideoInfo(path)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=NO_WINDOW,
    )
    text = proc.stderr
    m = re.search(r"Video:.*?,\s*(\d+(?:\.\d+)?) fps", text)
    if m:
        info.fps = float(m.group(1))
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        h, mi, s = m.groups()
        info.duration = int(h) * 3600 + int(mi) * 60 + float(s)
    info.has_audio = "Audio:" in text
    return info


def detect_encoders(ffmpeg: str) -> list[str]:
    """Retorna as chaves de encoders de GPU que realmente funcionam nesta máquina."""
    working = []
    for key in GPU_ORDER:
        _, args, pix_fmt, _ = ENCODERS[key]
        cmd = [
            ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30",
            "-frames:v", "5", *args, "-pix_fmt", pix_fmt, "-f", "null", "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=20, creationflags=NO_WINDOW)
        except Exception:
            continue
        if proc.returncode == 0:
            working.append(key)
    return working


def atempo_chain(tempo: float) -> str:
    """atempo só aceita 0.5..2.0 por filtro; encadeia quando necessário."""
    parts = []
    while tempo < 0.5:
        parts.append("atempo=0.5")
        tempo /= 0.5
    parts.append(f"atempo={tempo:.6f}")
    return ",".join(parts)


def build_command(ffmpeg: str, info: VideoInfo, dst: Path, fps: float, keep_audio: bool,
                  encoder: str = "x264_fast") -> list[str]:
    factor = info.fps / fps
    _, enc_args, pix_fmt, is_gpu = ENCODERS[encoder]
    cmd = [ffmpeg, "-hide_banner", "-y", "-nostdin"]
    if is_gpu:
        cmd += ["-hwaccel", "auto"]  # decodificação por GPU quando possível
    cmd += ["-i", str(info.path), "-map", "0:v:0"]
    if keep_audio and info.has_audio:
        cmd += ["-map", "0:a:0", "-af", atempo_chain(1 / factor), "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += [
        # Cada quadro recebe o timestamp N/fps: nenhum quadro é descartado ou duplicado.
        "-vf", f"setpts=N/({fps:g}*TB)",
        "-r", f"{fps:g}",
        *enc_args, "-pix_fmt", pix_fmt,
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        str(dst),
    ]
    return cmd


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
def fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    h, rest = divmod(int(round(seconds)), 3600)
    m, s = divmod(rest, 60)
    return f"{h:d}h{m:02d}min{s:02d}s" if h else f"{m:d}min{s:02d}s"

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("900x640")
        self.minsize(760, 520)

        self.ffmpeg = find_ffmpeg()
        self.folder: Path | None = None
        self.infos: list[VideoInfo] = []
        self.status: dict[int, str] = {}
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.busy = False

        self.folder_var = tk.StringVar()
        self.fps_var = tk.StringVar(value=DEFAULT_FPS)
        self.audio_var = tk.BooleanVar(value=False)
        self.enc_var = tk.StringVar(value="Detectando encoders...")
        self.enc_map: dict[str, str] = {}
        self.encoders_ready = False

        self._build_ui()
        self.fps_var.trace_add("write", lambda *_: self.refresh_tree())
        self.after(100, self.poll_events)

        if not self.ffmpeg:
            messagebox.showerror(
                APP_NAME,
                "ffmpeg não encontrado.\nInstale com 'pip install imageio-ffmpeg' "
                "ou adicione o ffmpeg ao PATH.",
            )
        else:
            threading.Thread(target=self.detect_worker, daemon=True).start()

    # ---- layout ---------------------------------------------------------- #
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 5}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Pasta com vídeos MP4:").pack(side="left")
        ttk.Entry(top, textvariable=self.folder_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=8
        )
        self.btn_browse = ttk.Button(top, text="Selecionar...", command=self.choose_folder)
        self.btn_browse.pack(side="left")

        opts = ttk.Frame(self)
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="FPS de saída:").pack(side="left")
        self.fps_combo = ttk.Combobox(opts, textvariable=self.fps_var, values=FPS_OPTIONS, width=6)
        self.fps_combo.pack(side="left", padx=8)
        self.chk_audio = ttk.Checkbutton(
            opts, text="Manter áudio (desacelerado)", variable=self.audio_var
        )
        self.chk_audio.pack(side="left", padx=16)
        ttk.Label(opts, text="Encoder:").pack(side="left", padx=(16, 0))
        self.enc_combo = ttk.Combobox(
            opts, textvariable=self.enc_var, state="disabled", width=34
        )
        self.enc_combo.pack(side="left", padx=8)

        cols = ("fps", "dur", "factor", "newdur", "status")
        heads = ("FPS original", "Duração", "Fator", "Nova duração", "Status")
        widths = (90, 80, 70, 100, 200)
        self.tree = ttk.Treeview(self, columns=cols, show="tree headings", height=10)
        self.tree.heading("#0", text="Arquivo")
        self.tree.column("#0", width=260)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, **pad)

        prog = ttk.Frame(self)
        prog.pack(fill="x", **pad)
        self.lbl_current = ttk.Label(prog, text="Arquivo atual")
        self.lbl_current.pack(anchor="w")
        self.bar_current = ttk.Progressbar(prog, maximum=100)
        self.bar_current.pack(fill="x", pady=(0, 6))
        self.lbl_total = ttk.Label(prog, text="Total")
        self.lbl_total.pack(anchor="w")
        self.bar_total = ttk.Progressbar(prog, maximum=100)
        self.bar_total.pack(fill="x")

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.btn_start = ttk.Button(btns, text="Converter", command=self.start, state="disabled")
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(btns, text="Cancelar", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=8)

        self.log = tk.Text(self, height=7, state="disabled", wrap="word")
        self.log.pack(fill="both", **pad)

    # ---- helpers --------------------------------------------------------- #
    def log_msg(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def target_fps(self) -> float | None:
        try:
            value = float(self.fps_var.get().replace(",", "."))
        except ValueError:
            return None
        return value if 1 <= value <= 1000 else None

    def is_convertible(self, info: VideoInfo, fps: float) -> bool:
        return bool(info.fps) and info.fps > fps * TOLERANCE

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.btn_browse.configure(state="disabled" if busy else "normal")
        self.fps_combo.configure(state="disabled" if busy else "normal")
        self.chk_audio.configure(state="disabled" if busy else "normal")
        self.enc_combo.configure(
            state="readonly" if (self.encoders_ready and not busy) else "disabled"
        )
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self.update_start_button()

    def update_start_button(self) -> None:
        fps = self.target_fps()
        ok = (
            not self.busy
            and self.encoders_ready
            and self.ffmpeg
            and fps
            and any(self.is_convertible(i, fps) for i in self.infos)
        )
        self.btn_start.configure(state="normal" if ok else "disabled")

    def refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        fps = self.target_fps()
        for idx, info in enumerate(self.infos):
            if info.fps is None:
                factor, newdur, status = "-", "-", "Erro ao ler"
            elif fps is None:
                factor, newdur, status = "-", "-", "FPS inválido"
            elif not self.is_convertible(info, fps):
                factor, newdur, status = "-", "-", "Ignorado (fps <= alvo)"
            else:
                f = info.fps / fps
                factor = f"{f:.2f}x"
                newdur = fmt_time(info.duration * f if info.duration else None)
                status = "Pendente"
            status = self.status.get(idx, status)
            self.tree.insert(
                "", "end", iid=str(idx), text=info.path.name,
                values=(f"{info.fps:g}" if info.fps else "-", fmt_time(info.duration),
                        factor, newdur, status),
            )
        self.update_start_button()

    # ---- ações ----------------------------------------------------------- #
    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Selecione a pasta com os vídeos MP4")
        if not chosen:
            return
        self.folder = Path(chosen)
        self.folder_var.set(chosen)
        self.infos, self.status = [], {}
        self.refresh_tree()
        self.set_busy(True)
        self.btn_cancel.configure(state="disabled")
        self.log_msg(f"Analisando {chosen} ...")
        threading.Thread(target=self.scan_worker, args=(self.folder,), daemon=True).start()

    def detect_worker(self) -> None:
        self.events.put(("encoders", detect_encoders(self.ffmpeg)))

    def scan_worker(self, folder: Path) -> None:
        files = sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".mp4" and not p.name.startswith("._")
        )
        infos = []
        for p in files:
            try:
                infos.append(probe(self.ffmpeg, p))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("log", f"Falha ao ler {p.name}: {exc}"))
                infos.append(VideoInfo(p))
        self.events.put(("scanned", infos))

    def start(self) -> None:
        fps = self.target_fps()
        if not fps or not self.folder:
            messagebox.showwarning(APP_NAME, "Informe um FPS válido (1 a 1000).")
            return
        self.cancel_event.clear()
        self.status = {}
        self.refresh_tree()
        self.set_busy(True)
        out_dir = self.folder / f"slowmo_{fps:g}fps"
        encoder = self.enc_map.get(self.enc_var.get(), "x264_fast")
        threading.Thread(
            target=self.convert_worker,
            args=(list(self.infos), out_dir, fps, self.audio_var.get(), encoder),
            daemon=True,
        ).start()

    def cancel(self) -> None:
        self.cancel_event.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.log_msg("Cancelando...")

    # ---- conversão (thread) --------------------------------------------- #
    def convert_worker(self, infos: list[VideoInfo], out_dir: Path, fps: float,
                       keep_audio: bool, encoder: str) -> None:
        todo = [(i, inf) for i, inf in enumerate(infos) if self.is_convertible(inf, fps)]
        out_dir.mkdir(exist_ok=True)
        done = 0
        batch_start = time.monotonic()
        for n, (idx, info) in enumerate(todo):
            if self.cancel_event.is_set():
                break
            dst = out_dir / f"{info.path.stem}_{fps:g}fps.mp4"
            factor = info.fps / fps
            file_start = time.monotonic()
            self.events.put(("current", n, len(todo), info.path.name))
            self.events.put(("status", idx, "Convertendo..."))
            self.events.put(("log", f"{info.path.name}: {info.fps:g} fps -> {fps:g} fps ({factor:.2f}x mais longo)"))
            ok, msg = self.convert_one(info, dst, fps, keep_audio, idx, n, len(todo), factor, encoder)
            if not ok and ENCODERS[encoder][3] and not self.cancel_event.is_set():
                self.events.put(("log", f"Encoder de GPU falhou em {info.path.name}; tentando na CPU."))
                ok, msg = self.convert_one(info, dst, fps, keep_audio, idx, n, len(todo), factor, "x264_fast")
            if self.cancel_event.is_set():
                dst.unlink(missing_ok=True)
                self.events.put(("status", idx, "Cancelado"))
                break
            if ok:
                done += 1
                self.events.put(("status", idx, "Concluído"))
                self.events.put(("log", f"{info.path.name}: concluído em {fmt_elapsed(time.monotonic() - file_start)}"))
            else:
                dst.unlink(missing_ok=True)
                self.events.put(("status", idx, "Erro"))
                self.events.put(("log", f"Erro em {info.path.name}:\n{msg}"))
        self.events.put(("finished", done, len(todo), str(out_dir), self.cancel_event.is_set(),
                         time.monotonic() - batch_start))

    def convert_one(self, info, dst, fps, keep_audio, idx, n, total, factor, encoder) -> tuple[bool, str]:
        cmd = build_command(self.ffmpeg, info, dst, fps, keep_audio, encoder)
        out_duration = (info.duration or 0) * factor
        tail: list[str] = []
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", creationflags=NO_WINDOW,
        )
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us=") and out_duration:
                try:
                    frac = min(int(line.split("=")[1]) / 1e6 / out_duration, 1.0)
                except ValueError:
                    continue
                self.events.put(("progress", idx, n, total, frac))
            elif "=" not in line or line.startswith("["):
                tail = (tail + [line])[-12:]
        code = self.proc.wait()
        return code == 0, "\n".join(tail)

    # ---- eventos da thread ---------------------------------------------- #
    def poll_events(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self.log_msg(ev[1])
                elif kind == "encoders":
                    gpu = ev[1]
                    auto_key = gpu[0] if gpu else "x264_fast"
                    self.enc_map = {f"Automático ({ENCODERS[auto_key][0]})": auto_key}
                    for key in [*gpu, "x264_fast", "x264_quality"]:
                        self.enc_map[ENCODERS[key][0]] = key
                    self.enc_combo.configure(values=list(self.enc_map))
                    self.enc_var.set(next(iter(self.enc_map)))
                    self.encoders_ready = True
                    self.set_busy(self.busy)
                    self.log_msg("Encoders disponíveis: " + ", ".join(
                        ENCODERS[k][0] for k in [*gpu, "x264_fast"]))
                elif kind == "scanned":
                    self.infos = ev[1]
                    self.refresh_tree()
                    self.set_busy(False)
                    self.log_msg(f"{len(self.infos)} arquivo(s) MP4 encontrado(s).")
                elif kind == "current":
                    _, n, total, name = ev
                    self.lbl_current.configure(text=f"Arquivo atual: {name}")
                    self.lbl_total.configure(text=f"Total: {n + 1}/{total}")
                    self.bar_current["value"] = 0
                elif kind == "status":
                    self.status[ev[1]] = ev[2]
                    self.refresh_tree()
                elif kind == "progress":
                    _, idx, n, total, frac = ev
                    self.bar_current["value"] = frac * 100
                    self.bar_total["value"] = (n + frac) / total * 100
                    if self.tree.exists(str(idx)):
                        self.tree.set(str(idx), "status", f"Convertendo {frac * 100:.0f}%")
                elif kind == "finished":
                    _, done, total, out_dir, cancelled, elapsed = ev
                    self.set_busy(False)
                    if not cancelled:
                        self.bar_current["value"] = 100 if total else 0
                        self.bar_total["value"] = 100 if total else 0
                    self.log_msg(
                        ("Cancelado. " if cancelled else "Finalizado. ")
                        + f"{done}/{total} convertido(s) em {fmt_elapsed(elapsed)}. Saída: {out_dir}"
                    )
        except queue.Empty:
            pass
        self.after(100, self.poll_events)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
