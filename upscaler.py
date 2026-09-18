#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  ВИДЕО-АПСЕЙЛЕР  ·  Video Upscaler  ·  100% бесплатно, без регистрации
================================================================================
Улучшает качество видео и поднимает разрешение вплоть до 4K / 8K.

Два движка:
  [1] БЫСТРЫЙ  — ffmpeg: lanczos/spline-апскейл + шумодав + резкость + цвет.
                 Работает на любом ПК, очень быстро.
  [2] AI       — Real-ESRGAN (нейросеть): реально дорисовывает детали.
                 Максимальное качество, но нужна видеокарта (Vulkan) для
                 вменяемой скорости. Модель скачивается один раз, бесплатно.

Запуск:      python upscaler.py
Без меню:    python upscaler.py "C:\video\film.mp4" --target 4k --engine ai
================================================================================
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------- #
#  Настройка консоли (Windows + UTF-8 + цвета)
# ----------------------------------------------------------------------------- #
IS_WIN = os.name == "nt"
BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = BASE_DIR / "tools"
ASSETS_DIR = BASE_DIR / "assets"
APPS_DIR = BASE_DIR / "apps"
TEMP_ROOT = BASE_DIR / "temp"
RESULT_DIR = BASE_DIR / "result"          # сюда складываются готовые видео


def purge_temp() -> None:
    """Полностью вычищает временную папку программы (включая саму папку).

    Вызывается после каждой AI-обработки, при завершении программы
    и при старте (убирает остатки от аварийно прерванных запусков).
    """
    if not TEMP_ROOT.is_dir():
        return
    for sub in list(TEMP_ROOT.iterdir()):
        try:
            if sub.is_dir():
                shutil.rmtree(sub, ignore_errors=True)
            else:
                sub.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        if TEMP_ROOT.is_dir() and not any(TEMP_ROOT.iterdir()):
            TEMP_ROOT.rmdir()
    except Exception:
        pass


atexit.register(purge_temp)


def _on_term(signum, frame):                      # noqa: ARG001
    purge_temp()
    sys.exit(143)


for _sig in ("SIGTERM", "SIGBREAK"):
    try:
        signal.signal(getattr(signal, _sig), _on_term)
    except Exception:
        pass

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if IS_WIN:
    try:
        os.system("")                      # включает VT-обработку в cmd
        subprocess.run("chcp 65001 > nul", shell=True, check=False)
    except Exception:
        pass

C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
    "blue": "\033[94m", "magenta": "\033[95m", "cyan": "\033[96m",
    "white": "\033[97m",
}
_NO_COLOR = os.environ.get("NO_COLOR") or not sys.stdout.isatty()
if _NO_COLOR:
    C = {k: "" for k in C}


def cl(text: str, *styles: str) -> str:
    return "".join(C.get(s, "") for s in styles) + str(text) + C["reset"]


def hr(ch: str = "─", n: int = 74, color: str = "dim") -> None:
    print(cl(ch * n, color))


def title(text: str) -> None:
    hr("═")
    print(cl(text, "bold", "cyan"))
    hr("═")


def ok(text: str) -> None:
    print(f"  {cl('✓', 'green')} {text}")


def warn(text: str) -> None:
    print(f"  {cl('!', 'yellow')} {cl(text, 'yellow')}")


def err(text: str) -> None:
    print(f"  {cl('✗', 'red')} {cl(text, 'red')}")


def info(text: str) -> None:
    print(f"  {cl('·', 'blue')} {text}")


VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".mpg", ".mpeg",
              ".ts", ".flv", ".wmv", ".3gp", ".vob", ".mts", ".m2ts", ".ogv"}

TARGETS: Dict[str, Tuple[int, int, str]] = {
    "720":  (1280, 720,  "HD 720p"),
    "1080": (1920, 1080, "Full HD 1080p"),
    "1440": (2560, 1440, "2K QHD 1440p"),
    "4k":   (3840, 2160, "4K UHD 2160p"),
    "8k":   (7680, 4320, "8K UHD 4320p"),
}

# Пресеты качества для БЫСТРОГО движка
PRESETS: Dict[str, dict] = {
    "soft": {
        "name": "Мягкий (естественно, без «перешарпа»)",
        "denoise": 0, "sharpen": 0.0, "unsharp": (0, 0, 0.0),
        "sat": 1.00, "cont": 1.00, "gamma": 1.00, "grain": 0.0,
        "crf": 18, "slow": False,
    },
    "balanced": {
        "name": "Сбалансированный  (рекомендуется)",
        "denoise": 0.6, "sharpen": 0.5, "unsharp": (5, 5, 0.55),
        "sat": 1.06, "cont": 1.03, "gamma": 1.00, "grain": 0.5,
        "crf": 17, "slow": False,
    },
    "max": {
        "name": "Максимальная резкость и чистота",
        "denoise": 1.6, "sharpen": 1.2, "unsharp": (7, 7, 0.9),
        "sat": 1.12, "cont": 1.06, "gamma": 1.00, "grain": 0.7,
        "crf": 16, "slow": True,
    },
    "restore": {
        "name": "Реставрация старого/шумного видео (VHS, DVD, камера)",
        "denoise": 2.8, "sharpen": 0.6, "unsharp": (5, 5, 0.5),
        "sat": 1.10, "cont": 1.05, "gamma": 1.02, "grain": 0.0,
        "crf": 16, "slow": True, "deblock": True, "deint": True,
    },
}

# Модели Real-ESRGAN
AI_MODELS = {
    "general": {
        "name": "Универсальная — фильм/сериал/видео с камеры (лучшее качество)",
        "model": "realesrgan-x4plus", "scales": [4], "speed": 1.0,
    },
    "anime": {
        "name": "Аниме / мультфильмы (чистые линии)",
        "model": "realesrgan-x4plus-anime", "scales": [4], "speed": 1.1,
    },
    "fast": {
        "name": "Быстрая — animevideov3 (в 3-5 раз быстрее, отлично для аниме)",
        "model": "realesr-animevideov3", "scales": [2, 3, 4], "speed": 3.5,
    },
}


# ----------------------------------------------------------------------------- #
#  Поиск / установка ffmpeg
# ----------------------------------------------------------------------------- #
def _which(name: str) -> Optional[str]:
    return shutil.which(name) or shutil.which(name + ".exe")


def _ffmpeg_search_dirs() -> List[Path]:
    """Где искать ffmpeg: папки программы + типичные места установки."""
    dirs: List[Path] = [TOOLS_DIR, APPS_DIR, BASE_DIR]
    if IS_WIN:
        env = os.environ
        cand = [
            Path(env.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin",
            Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "ffmpeg" / "bin",
            Path("C:/ffmpeg/bin"),
            Path(env.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
            Path(env.get("ProgramData", r"C:\ProgramData")) / "chocolatey" / "bin",
            Path(env.get("USERPROFILE", "")) / "scoop" / "shims",
            Path(env.get("USERPROFILE", "")) / "scoop" / "apps" / "ffmpeg" / "current" / "bin",
        ]
        wg = Path(env.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        if wg.is_dir():
            for sub in wg.glob("*ffmpeg*"):
                for exe in list(sub.rglob("ffmpeg.exe"))[:2]:
                    cand.append(exe.parent)
        dirs += [c for c in cand if str(c)]
    else:
        dirs += [Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin"),
                 Path("/opt/homebrew/bin"), Path("/snap/bin")]
    # рекурсивно по папкам программы: вдруг сборку распаковали куда угодно внутрь
    for root in (TOOLS_DIR, APPS_DIR, BASE_DIR):
        if root.is_dir():
            for exe in root.rglob("ffmpeg.exe" if IS_WIN else "ffmpeg"):
                dirs.append(exe.parent)
    return dirs


def find_ffmpeg() -> Tuple[Optional[str], Optional[str]]:
    """Ищет ffmpeg/ffprobe: env -> папки программы -> типичные места -> PATH."""
    names = ("ffmpeg.exe", "ffprobe.exe") if IS_WIN else ("ffmpeg", "ffprobe")
    dirs: List[Path] = []
    env_ff = os.environ.get("FFMPEG_PATH")
    if env_ff:
        p = Path(env_ff)
        dirs.append(p if p.is_dir() else p.parent)
    dirs += _ffmpeg_search_dirs()

    full: Optional[Tuple[str, str]] = None
    half: Optional[str] = None
    seen = set()
    for d in dirs:
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        f, pr = d / names[0], d / names[1]
        if f.is_file():
            try:
                os.chmod(f, 0o755)
            except Exception:
                pass
            if pr.is_file():
                full = full or (str(f), str(pr))
            else:
                half = half or str(f)
    if full:
        return full
    wf, wp = _which("ffmpeg"), _which("ffprobe")
    if wf and wp:
        return wf, wp
    if half and wp:
        return half, wp
    if wf:
        return wf, None
    if half:
        return half, None
    return None, None


FFMPEG, FFPROBE = find_ffmpeg()


# ----------------------------------------------------------------------------- #
#  Поиск Real-ESRGAN
# ----------------------------------------------------------------------------- #
RES_WIN_ZIP = "realesrgan-ncnn-vulkan-20220424-windows.zip"
RES_URLS = [
    "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/" + RES_WIN_ZIP,
    "https://ghproxy.net/https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/" + RES_WIN_ZIP,
]
RES_EXE_NAME = "realesrgan-ncnn-vulkan.exe" if IS_WIN else "realesrgan-ncnn-vulkan"


def find_realesrgan() -> Tuple[Optional[str], Optional[str]]:
    """Возвращает (путь_к_бинарнику, путь_к_папке_models). Учитывает ОС."""
    env = os.environ.get("RES_PATH")
    if env:
        e = Path(env)
        models = e.parent / "models" if e.is_file() else e / "models"
        if e.exists() and models.is_dir():
            return str(e), str(models)
    for root in (APPS_DIR, TOOLS_DIR, ASSETS_DIR, BASE_DIR):
        if not root.is_dir():
            continue
        for exe in list(root.rglob(RES_EXE_NAME)):
            if IS_WIN and not str(exe).lower().endswith(".exe"):
                continue
            if not IS_WIN and str(exe).lower().endswith(".exe"):
                continue
            models = exe.parent / "models"
            if not models.is_dir():
                # модели могут лежать рядом в assets
                alt = ASSETS_DIR / "realesrgan-models"
                models = alt if alt.is_dir() else models
            if exe.exists():
                try:
                    os.chmod(exe, 0o755)
                except Exception:
                    pass
                return str(exe), (str(models) if models.is_dir() else None)
    # системный
    w = _which("realesrgan-ncnn-vulkan")
    if w:
        return w, None
    return None, None


RES_EXE, RES_MODELS = find_realesrgan()


# ----------------------------------------------------------------------------- #
#  Сетевые хелперы
# ----------------------------------------------------------------------------- #
def download(url: str, dest: Path, desc: str = "") -> bool:
    """Скачивание файла: PowerShell (Windows) -> curl -> urllib."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    label = desc or dest.name
    print(f"  {cl('↓', 'blue')} Скачиваю: {label}")
    print(f"    {cl(url, 'dim')}")
    cmds = []
    if IS_WIN:
        ps = (
            "$ProgressPreference='SilentlyContinue';"
            "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
            f"Invoke-WebRequest -Uri '{url}' -OutFile '{tmp}' -UseBasicParsing"
        )
        cmds.append(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
    cmds.append(["curl", "-L", "--fail", "--retry", "2", "-o", str(tmp), url])

    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, text=True, timeout=1800)
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
                tmp.replace(dest)
                ok(f"Готово: {dest.name}  ({dest.stat().st_size/1e6:.1f} МБ)")
                return True
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            warn("Таймаут скачивания")
        except Exception as e:
            warn(f"Ошибка: {e}")
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
    # последний вариант — чистый python
    try:
        import urllib.request
        urllib.request.urlretrieve(url, tmp)
        if tmp.exists() and tmp.stat().st_size > 1000:
            tmp.replace(dest)
            ok(f"Готово: {dest.name}")
            return True
    except Exception as e:
        err(f"Не удалось скачать {url}\n    {e}")
    return False


FFMPEG_URLS_WIN = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
]


def install_ffmpeg() -> bool:
    print()
    title("Установка FFmpeg")
    if not IS_WIN:
        warn("Авто-установка доступна только в Windows.")
        info("Linux:  sudo apt install ffmpeg     macOS:  brew install ffmpeg")
        return False
    zip_path = TOOLS_DIR / "ffmpeg.zip"
    got = False
    for u in FFMPEG_URLS_WIN:
        if download(u, zip_path, "FFmpeg (сборка для Windows)"):
            got = True
            break
    if not got:
        err("Скачать не получилось. Скачай вручную: https://www.gyan.dev/ffmpeg/builds/")
        return False
    try:
        ok("Распаковываю...")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(TOOLS_DIR / "_ff")
        found = 0
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            for f in (TOOLS_DIR / "_ff").rglob(name):
                shutil.copy2(f, TOOLS_DIR / name)
                found += 1
                break
        shutil.rmtree(TOOLS_DIR / "_ff", ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        if found == 2:
            ok(f"FFmpeg установлен в {TOOLS_DIR}")
            return True
        err("В архиве не нашлись ffmpeg.exe / ffprobe.exe")
    except Exception as e:
        err(f"Ошибка распаковки: {e}")
    return False


def install_realesrgan() -> bool:
    print()
    title("Установка AI-движка Real-ESRGAN")
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = APPS_DIR / RES_WIN_ZIP
    got = False
    # 1) офлайн-вариант: архив уже лежит в assets/ (рядом с программой)
    names = [RES_WIN_ZIP] if IS_WIN else [
        "realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
        "realesrgan-ncnn-vulkan-20220424-macos.zip", RES_WIN_ZIP]
    for nm in names:
        bundled = ASSETS_DIR / nm
        if bundled.is_file() and bundled.stat().st_size > 1_000_000:
            ok(f"Найден локальный архив: {bundled.name} — интернет не нужен.")
            shutil.copy2(bundled, zip_path)
            got = True
            break
    # 2) скачивание
    if not got:
        for u in RES_URLS:
            if download(u, zip_path, "Real-ESRGAN + модели (~45 МБ, один раз)"):
                got = True
                break
    if not got:
        err("Не удалось ни найти локальный архив, ни скачать.")
        info(f"Скачай вручную и положи в {ASSETS_DIR}:")
        info("https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0")
        return False
    try:
        ok("Распаковываю...")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(APPS_DIR / "realesrgan")
        zip_path.unlink(missing_ok=True)
        if not IS_WIN:
            for f in (APPS_DIR / "realesrgan").rglob("realesrgan-ncnn-vulkan*"):
                try:
                    os.chmod(f, 0o755)
                except Exception:
                    pass
        global RES_EXE, RES_MODELS
        RES_EXE, RES_MODELS = find_realesrgan()
        if RES_EXE:
            ok(f"AI-движок установлен: {RES_EXE}")
            return True
    except Exception as e:
        err(f"Ошибка распаковки: {e}")
    return False


# ----------------------------------------------------------------------------- #
#  Анализ видео
# ----------------------------------------------------------------------------- #
def probe(path: Path) -> Optional[dict]:
    if not FFPROBE:
        return None
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        return json.loads(r.stdout)
    except Exception:
        return None


def parse_fps(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        if "/" in raw:
            a, b = raw.split("/")
            return round(float(a) / float(b), 3) if float(b) else 0.0
        return float(raw)
    except Exception:
        return 0.0


class MediaInfo:
    def __init__(self, path: Path):
        self.path = path
        self.width = self.height = 0
        self.fps = 0.0
        self.duration = 0.0
        self.vcodec = "?"
        self.pix_fmt = "?"
        self.color_space = ""
        self.bit_depth = 8
        self.has_audio = False
        self.acodec = ""
        self.size = path.stat().st_size if path.exists() else 0
        d = probe(path)
        if not d:
            return
        fmt = d.get("format", {}) or {}
        try:
            self.duration = float(fmt.get("duration", 0) or 0)
        except Exception:
            self.duration = 0.0
        for s in d.get("streams", []):
            if s.get("codec_type") == "video" and self.width == 0:
                self.width = int(s.get("width") or 0)
                self.height = int(s.get("height") or 0)
                self.fps = parse_fps(s.get("r_frame_rate") or s.get("avg_frame_rate") or "")
                self.vcodec = s.get("codec_name", "?")
                self.pix_fmt = s.get("pix_fmt", "?") or "?"
                self.color_space = s.get("color_space", "") or ""
                pf = self.pix_fmt
                m = re.search(r"(\d+)le|(\d+)be", pf)
                self.bit_depth = int(next((g for g in (m.groups() if m else []) if g), 8)) if m else 8
                if "10" in pf or "12" in pf:
                    self.bit_depth = 10 if "10" in pf else 12
                try:
                    self.duration = self.duration or float(s.get("duration", 0) or 0)
                except Exception:
                    pass
            elif s.get("codec_type") == "audio" and not self.has_audio:
                self.has_audio = True
                self.acodec = s.get("codec_name", "")

    @property
    def valid(self) -> bool:
        return self.width > 0 and self.height > 0

    @property
    def frames(self) -> int:
        return max(1, int(self.duration * (self.fps or 25)))

    def show(self) -> None:
        print()
        hr()
        print(f"  {cl('Файл:', 'bold')}       {self.path.name}")
        print(f"  {cl('Папка:', 'bold')}      {cl(str(self.path.parent), 'dim')}")
        res = f"{self.width}×{self.height}"
        label = "SD" if self.height < 720 else ("HD" if self.height < 1080 else
                ("Full HD" if self.height < 1440 else ("2K" if self.height < 2160 else "4K+")))
        print(f"  {cl('Разрешение:', 'bold')} {cl(res, 'magenta')} ({label})")
        print(f"  {cl('Длительность:', 'bold')} {fmt_time(self.duration)}   "
              f"{cl('FPS:', 'bold')} {self.fps or '?'}   "
              f"{cl('Размер:', 'bold')} {human(self.size)}")
        print(f"  {cl('Кодек:', 'bold')}      {self.vcodec} / {self.pix_fmt} "
              f"({self.bit_depth} бит)   {cl('Звук:', 'bold')} "
              f"{self.acodec if self.has_audio else cl('нет', 'yellow')}")
        hr()


def total_ram_gb() -> float:
    """Объём ОЗУ в ГБ (Windows / Linux / macOS)."""
    try:
        if IS_WIN:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
            return st.ullTotalPhys / (1024 ** 3)
        for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            if os.path.exists(p):
                v = Path(p).read_text().strip()
                if v.isdigit() and int(v) < (1 << 50):
                    return int(v) / (1024 ** 3)
        if os.path.exists("/proc/meminfo"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return float(line.split()[1]) / (1024 ** 2)
    except Exception:
        pass
    return 8.0


def free_disk_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(str(path.parent if path.is_file() else path)).free / (1024 ** 3)
    except Exception:
        return 999.0


def human(n: float) -> str:
    for u in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.1f} {u}" if u != "Б" else f"{int(n)} {u}"
        n /= 1024
    return f"{n:.1f} ТБ"


def fmt_time(s: float) -> str:
    if not s or s < 0:
        return "00:00"
    s = int(s)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


# ----------------------------------------------------------------------------- #
#  Ввод/вывод в консоли
# ----------------------------------------------------------------------------- #
def clean_path(s: str) -> Path:
    """Убирает кавычки и хвостовые точки/пробелы (Windows drag&drop)."""
    s = s.strip().strip('"').strip("'").strip()
    while s.endswith("."):
        s = s[:-1]
    return Path(s).expanduser()


def ask(prompt: str, default: str = "") -> str:
    d = f" {cl('[' + default + ']', 'dim')}" if default else ""
    try:
        v = input(f"  {cl(prompt, 'bold')}{d} > ").strip()
    except EOFError:
        return default
    return v or default


def ask_choice(prompt: str, options: List[Tuple[str, str]], default: str = "1",
               note: Optional[str] = None) -> str:
    print()
    print(f"  {cl(prompt, 'bold', 'cyan')}")
    for key, label in options:
        mark = cl("►", "green") if key == default else " "
        print(f"   {mark} {cl('[' + key + ']', 'yellow')} {label}")
    if note:
        print(f"     {cl(note, 'dim')}")
    while True:
        v = ask("Выбор", default).lower()
        keys = [k for k, _ in options]
        if v in keys:
            return v
        err(f"Введи одну из: {', '.join(keys)}")


def ask_yes(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        v = ask(f"{prompt} ({d})", "").lower()
        if not v:
            return default
        if v in ("y", "yes", "д", "да", "1", "+"):
            return True
        if v in ("n", "no", "н", "нет", "0", "-"):
            return False
        err("Ответь y (да) или n (нет)")


def pick_file() -> Optional[Path]:
    print()
    print(cl("  Перетащи видеофайл (или папку с видео) прямо в это окно и нажми Enter.", "dim"))
    print(cl("  Либо введи полный путь. Пустая строка — выход.", "dim"))
    while True:
        raw = ask("Файл / папка", "")
        if not raw:
            return None
        p = clean_path(raw)
        if p.is_file():
            return p
        if p.is_dir():
            vids = sorted([f for f in p.iterdir()
                           if f.is_file() and f.suffix.lower() in VIDEO_EXTS])
            if not vids:
                err(f"В папке {p} видео не найдено.")
                continue
            if len(vids) == 1:
                return vids[0]
            print()
            print(f"  {cl('В папке найдено видео:', 'bold')}")
            opts = []
            for i, f in enumerate(vids, 1):
                mi = MediaInfo(f)
                opts.append((str(i), f"{f.name}  {cl(f'{mi.width}×{mi.height}', 'magenta')}"))
                if i >= 9:
                    break
            opts.append(("a", cl("Обработать ВСЕ файлы папки (пакетно)", "green")))
            c = ask_choice("Что обработать?", opts, "1")
            if c == "a":
                return p          # вернём папку — режим batch
            return vids[int(c) - 1]
        err(f"Не найдено: {p}")


def ask_settings() -> dict:
    """Один раз спрашивает настройки — применяются ко всем файлам пакета."""
    auto: dict = {"yes": True}
    c = ask_choice("Движок для всех файлов?", [
        ("1", "БЫСТРЫЙ (ffmpeg) — быстро, всегда работает"),
        ("2", "AI (Real-ESRGAN) — лучшее качество, нужна видеокарта")], "1")
    auto["engine"] = "fast" if c == "1" else "ai"
    c = ask_choice("Целевое разрешение?", [
        ("720", "1280×720 HD"), ("1080", "1920×1080 Full HD"),
        ("1440", "2560×1440 2K"), ("4k", "3840×2160 4K"),
        ("x2", "в 2 раза от исходника")], "4k")
    if c == "x2":
        auto["fit_x2"] = True
    else:
        auto["size"] = TARGETS[c][0], TARGETS[c][1]
    if auto["engine"] == "fast":
        c = ask_choice("Пресет?", [(k, v["name"]) for k, v in PRESETS.items()], "balanced")
        auto["preset"] = c
    else:
        c = ask_choice("Модель нейросети?", [(k, v["name"]) for k, v in AI_MODELS.items()], "general")
        auto["model"] = c
    return auto


# ----------------------------------------------------------------------------- #
#  Прогресс ffmpeg
# ----------------------------------------------------------------------------- #
class Progress:
    """Парсит stderr ffmpeg (frame=/time=) и рисует полоску прогресса."""

    def __init__(self, total_sec: float, label: str = "Обработка"):
        self.total = max(total_sec, 0.001)
        self.label = label
        self._stop = False
        self._cur = 0.0
        self._frame = 0
        self._fps = 0.0
        self._t0 = time.time()

    def start(self) -> None:
        self._t0 = time.time()
        th = threading.Thread(target=self._loop, daemon=True)
        th.start()

    def feed(self, chunk: str) -> None:
        for m in re.finditer(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", chunk):
            h, mi, s = m.groups()
            self._cur = int(h) * 3600 + int(mi) * 60 + float(s)
        for m in re.finditer(r"frame=\s*(\d+)\s+fps=\s*([\d.]+)", chunk):
            self._frame = int(m.group(1))
            try:
                self._fps = float(m.group(2))
            except Exception:
                pass

    def _loop(self) -> None:
        last = -1
        while not self._stop:
            p = min(self._cur / self.total, 1.0)
            if int(p * 100) != last:
                last = int(p * 100)
                self._draw(p)
            time.sleep(0.2)

    def _draw(self, p: float) -> None:
        width = 28
        filled = int(width * p)
        bar = "█" * filled + "░" * (width - filled)
        el = time.time() - self._t0
        eta = (el / p - el) if p > 0.02 else 0
        spd = ""
        if self._fps:
            spd = f" {self._fps:.0f} fps"
        line = (f"\r  {self.label} {cl(bar, 'green')} {p*100:5.1f}%  "
                f"{fmt_time(self._cur)}/{fmt_time(self.total)}{spd}  "
                f"осталось ~{fmt_time(eta)}   ")
        sys.stdout.write(line[:118].ljust(118))
        sys.stdout.flush()

    def done(self) -> None:
        self._stop = True
        self._draw(1.0)
        sys.stdout.write("\n")
        sys.stdout.flush()


def run_ffmpeg(cmd: List[str], total_sec: float, label: str = "Обработка",
               quiet_tail: bool = False) -> int:
    """Запуск ffmpeg с живым прогрессом. Возвращает код возврата."""
    pr = Progress(total_sec, label)
    pr.start()
    try:
        p = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0),
        )
        buf = ""
        assert p.stderr is not None
        while True:
            ch = p.stderr.read(1)
            if not ch:
                break
            buf += ch
            if ch in ("\r", "\n"):
                pr.feed(buf)
                if not quiet_tail:
                    buf = ""
                else:
                    buf = buf[-2000:]
        p.wait()
        pr.done()
        return p.returncode
    except KeyboardInterrupt:
        pr.done()
        raise
    except Exception as e:
        pr.done()
        err(str(e))
        return 1


# ----------------------------------------------------------------------------- #
#  Формирование цепочки фильтров (БЫСТРЫЙ движок)
# ----------------------------------------------------------------------------- #
_FILTER_CACHE: Dict[str, bool] = {}


def filter_supported(chain: str) -> bool:
    """Проверяет, поддерживает ли установленный ffmpeg цепочку фильтров."""
    if chain in _FILTER_CACHE:
        return _FILTER_CACHE[chain]
    if not FFMPEG:
        return False
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "nullsrc=size=64x64:duration=0.04:rate=25",
             "-vf", chain, "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
            creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
        _FILTER_CACHE[chain] = (r.returncode == 0)
    except Exception:
        _FILTER_CACHE[chain] = False
    return _FILTER_CACHE[chain]


def target_size(src: MediaInfo, tw: int, th: int,
                mode: str = "fit") -> Tuple[int, int]:
    """Считает итоговый размер с сохранением пропорций (чётные числа)."""
    sw, sh = src.width or 16, src.height or 9
    if mode == "stretch":
        w, h = tw, th
    elif mode == "fill":
        r = max(tw / sw, th / sh)
        w, h = sw * r, sh * r
    else:  # fit
        r = min(tw / sw, th / sh)
        w, h = sw * r, sh * r
    w = int(round(w / 2) * 2)
    h = int(round(h / 2) * 2)
    return max(2, w), max(2, h)


def build_filterchain(src: MediaInfo, w: int, h: int, preset: dict,
                      fps_out: float, upscale: bool) -> str:
    f: List[str] = []

    # 1) Деинтерлейсинг для чересстрочного источника
    if preset.get("deint"):
        f.append("yadif=mode=0:parity=-1:deint=interlaced")

    # 2) Устранение «грязи» ПЕРЕД апскейлом (шум + блоки сжатия)
    if preset.get("deblock") and filter_supported("deblock=filter=weak"):
        f.append("deblock=filter=weak:block=3:alpha=0.10:beta=0.05")
    if preset.get("denoise", 0):
        d = preset["denoise"]
        f.append(f"hqdn3d={1.0*d:.2f}:{0.8*d:.2f}:{4.0*d:.2f}:{3.0*d:.2f}")

    # 3) Апскейл (или даунскейл) с сохранением пропорций + корректная матрица цвета
    if upscale:
        f.append(f"scale={w}:{h}:flags=spline+accurate_rnd+full_chroma_int+full_chroma_inp"
                 f":in_color_matrix=auto:out_color_matrix=bt709")
    else:
        f.append(f"scale={w}:{h}:flags=lanczos+accurate_rnd+full_chroma_int"
                 f":in_color_matrix=auto:out_color_matrix=bt709")

    # 4) SD (PAL/NTSC) → HD: доп. конвертация матрицы, если ffmpeg её понимает
    if (src.height or 0) <= 576:
        for cm in ("colormatrix=bt601-6-625:bt709", "colormatrix=bt470bg:bt709"):
            if filter_supported(cm):
                f.append(cm)
                break

    # 5) Резкость: unsharp (локальный контраст) + cas (детализация)
    lx, ly, la = preset.get("unsharp", (0, 0, 0.0))
    if la:
        f.append(f"unsharp=luma_msize_x={lx}:luma_msize_y={ly}:luma_amount={la:.2f}"
                 f":chroma_msize_x=3:chroma_msize_y=3:chroma_amount=0.15")
    if preset.get("sharpen", 0) and filter_supported("cas=0.5"):
        f.append(f"cas={min(1.0, preset['sharpen']):.2f}")

    # 6) Цвет / контраст
    eq = []
    if preset.get("sat", 1.0) != 1.0:
        eq.append(f"saturation={preset['sat']:.2f}")
    if preset.get("cont", 1.0) != 1.0:
        eq.append(f"contrast={preset['cont']:.2f}")
    if preset.get("gamma", 1.0) != 1.0:
        eq.append(f"gamma={preset['gamma']:.2f}")
    if eq:
        f.append("eq=" + ":".join(eq))

    # 7) Лёгкое «киношное» зерно — маскирует артефакты апскейла
    if preset.get("grain", 0):
        f.append(f"gblur=sigma={0.35 + 0.25*preset['grain']:.2f}:steps=1")
        f.append(f"noise=alls={int(3 + 6*preset['grain'])}:allf=t+u")

    # 8) Повышение частоты кадров
    if fps_out and fps_out > (src.fps or 0) + 0.5:
        mi = (f"minterpolate=fps={fps_out:g}:mi_mode=mci:mc_mode=aobmc:"
              f"me_mode=bidir:vsbmc=1:scd=fdiff:scd_threshold=8")
        if filter_supported(f"minterpolate=fps={fps_out:g}"):
            f.append(mi)
        else:
            f.append(f"fps={fps_out:g}")
            warn("minterpolate недоступен — использую простое fps (без дорисовки движения)")

    f.append("format=yuv420p")
    return ",".join(f)


def x264_mem_guard(w: int, h: int, ref: int, lookahead: int,
                   threads: int) -> Tuple[int, int]:
    """Уменьшает ref/rc-lookahead, если x264 не влезет в ОЗУ.

    x264 при 4K с дефолтным lookahead=40 и ref=3 может потребовать 1.5-2 ГБ
    и на слабых машинах молча падает. Считаем грубо и подрезаем параметры.
    """
    ram = total_ram_gb()
    mb_frame = w * h * 1.5 * 3 / (1024 ** 2)      # ~24 МБ на кадр для 4K
    est = mb_frame * (ref + lookahead + 6 + threads * 2.0) / 1024
    budget = max(ram * 0.55, 0.35)
    while est > budget and lookahead > 8:
        lookahead = max(8, int(lookahead * 0.6))
        est = mb_frame * (ref + lookahead + 6 + threads * 2.0) / 1024
    while est > budget and ref > 1:
        ref -= 1
        est = mb_frame * (ref + lookahead + 6 + threads * 2.0) / 1024
    return ref, lookahead


def encode_args(preset: dict, w: int = 1920, h: int = 1080,
                extra_slow: bool = False) -> List[str]:
    """Аргументы кодека: качество + защита от нехватки памяти."""
    crf = preset.get("crf", 17)
    x264_preset = "slow" if (preset.get("slow") or extra_slow) else "medium"
    ref = 5 if x264_preset == "slow" else 3
    lookahead = 40
    if w * h > 1920 * 1080:
        ref, lookahead = x264_mem_guard(w, h, ref, lookahead, max(1, (os.cpu_count() or 2)))
        if ref < 5 and x264_preset == "slow":
            x264_preset = "medium"          # slow на 4K ест слишком много ОЗУ/времени
    args = ["-c:v", "libx264", "-preset", x264_preset, "-crf", str(crf),
            "-profile:v", "high",
            "-x264-params", f"ref={ref}:rc-lookahead={lookahead}",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if w * h >= 3840 * 2160:
        args[args.index("-profile:v") + 1] = "high"
        args += ["-level", "5.1"]
    return args


# ----------------------------------------------------------------------------- #
#  ДВИЖОК 1: БЫСТРЫЙ (ffmpeg)
# ----------------------------------------------------------------------------- #
def run_fast_engine(src: MediaInfo, out: Path, w: int, h: int, preset: dict,
                    fps_out: float, threads: int = 0) -> bool:
    upscale = w >= (src.width or 0)
    chain = build_filterchain(src, w, h, preset, fps_out, upscale)

    ram = total_ram_gb()
    if w * h >= 3840 * 2160 and ram < 6:
        warn(f"ОЗУ всего {ram:.1f} ГБ — для {w}×{h} кодеку придётся урезать "
             f"буферы, качество чуть снизится, но работать будет.")

    def build_cmd(pres: dict) -> List[str]:
        cmd = [FFMPEG, "-hide_banner", "-y", "-threads", str(threads or 0),
               "-i", str(src.path), "-vf", chain,
               "-r", f"{fps_out:g}" if fps_out else f"{src.fps or 25:g}"]
        cmd += encode_args(pres, w, h)
        if src.has_audio:
            cmd += ["-map", "0:v:0", "-map", "0:a", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000", "-ac", "2"]
        else:
            cmd += ["-an"]
        cmd += ["-shortest", str(out)]
        return cmd

    print()
    print(f"  {cl('Цепочка фильтров:', 'dim')}")
    for part in chain.split(","):
        print(f"     {cl('•', 'blue')} {part}")
    print()

    rc = run_ffmpeg(build_cmd(preset), src.duration, "Быстрый апскейл")
    if rc == 0 and out.exists() and out.stat().st_size > 1000:
        return True

    # ---- запасной вариант: меньше нагрузка на память/процессор ---- #
    warn("Первый проход не удался — пробую облегчённый режим кодирования...")
    out.unlink(missing_ok=True)
    light = dict(preset)
    light["slow"] = False
    light["crf"] = max(preset.get("crf", 17), 19)
    cmd = build_cmd(light)
    # принудительно режем lookahead и ref до минимума
    if "-x264-params" in cmd:
        cmd[cmd.index("-x264-params") + 1] = "ref=1:rc-lookahead=8"
    if "-preset" in cmd:
        cmd[cmd.index("-preset") + 1] = "faster"
    rc = run_ffmpeg(cmd, src.duration, "Быстрый апскейл (лёгкий)")
    if rc == 0 and out.exists() and out.stat().st_size > 1000:
        return True
    err(f"ffmpeg завершился с кодом {rc}.")
    info("Что попробовать: пресет «Мягкий», разрешение 1080p/1440p вместо 4K, "
         "или движок AI.")
    return False


# ----------------------------------------------------------------------------- #
#  ДВИЖОК 2: AI (Real-ESRGAN ncnn-vulkan)
# ----------------------------------------------------------------------------- #
def gpu_is_available() -> Tuple[bool, str]:
    """Быстрая проверка: есть ли Vulkan-устройство (и не программный ли рендер)."""
    if not RES_EXE:
        return False, "AI-движок не установлен"
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "t.png"
            try:
                import cv2
                import numpy as np
                cv2.imwrite(str(inp), np.zeros((32, 32, 3), dtype=np.uint8))
            except Exception:
                inp.write_bytes(bytes.fromhex(
                    "89504e470d0a1a0a0000000d49484452000000010000000108060000"
                    "001f15c4890000000a49444154789c63000100000500010d0a2db4"
                    "0000000049454e44ae426082"))
            outp = Path(td) / "o.png"
            args = [RES_EXE, "-i", str(inp), "-o", str(outp), "-s", "2"]
            if RES_MODELS:
                args += ["-m", RES_MODELS]
            r = subprocess.run(args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300,
                               creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
            txt = (r.stdout or "") + (r.stderr or "")
            m = re.search(r"\[\d+\s+([^\]]+)\]", txt)
            dev = m.group(1).strip() if m else "неизвестно"
            if r.returncode != 0:
                return False, f"Vulkan недоступен (устройство: {dev})"
            soft = any(k in dev.lower()
                       for k in ("llvmpipe", "softpipe", "software", "basic render", "swrast"))
            return (not soft), dev
    except Exception as e:
        return False, f"ошибка проверки: {e}"


def pick_ai_scale(src: MediaInfo, w: int, h: int, model_key: str) -> int:
    """Минимальный масштаб нейросети, покрывающий целевое разрешение."""
    scales = AI_MODELS[model_key]["scales"]
    need = max(w / max(src.width, 1), h / max(src.height, 1))
    for s in scales:
        if s >= need - 0.01:
            return s
    return scales[-1]


def ai_chunk_frames(w: int, h: int) -> int:
    """Сколько кадров держать на диске одновременно, чтобы его не забить."""
    mb_in = max((w * 3) * (h * 3) * 3 / (1024 ** 2) * 0.55, 0.3)   # PNG после апскейла
    mb_pair = mb_in + max(w * h * 3 / (1024 ** 2) * 0.6, 0.1)
    free = free_disk_gb(TEMP_ROOT if TEMP_ROOT.exists() else BASE_DIR) * 1024
    budget = max(free * 0.4, 300)          # МБ, не больше 40% свободного места
    n = int(budget / max(mb_pair, 0.5))
    return max(24, min(n, 300))


def run_ai_engine(src: MediaInfo, out: Path, w: int, h: int,
                  model_key: str = "general", crf: int = 17,
                  threads: str = "1:2:2", preview_sec: float = 0) -> bool:
    """Покадровый AI-апскейл чанками: извлекли → улучшили → собрали."""
    global RES_EXE, RES_MODELS
    if not RES_EXE or not RES_MODELS:
        err("AI-движок Real-ESRGAN не найден.")
        if IS_WIN and ask_yes("Скачать и установить сейчас? (~45 МБ, один раз)", True):
            if not install_realesrgan():
                return False
        if not RES_EXE or not RES_MODELS:
            return False

    scale = pick_ai_scale(src, w, h, model_key)
    model = AI_MODELS[model_key]["model"]
    fps = src.fps or 25.0
    dur = preview_sec or src.duration
    total_frames = max(1, int(dur * fps))

    print()
    info(f"Модель: {cl(model, 'magenta')}   Масштаб нейросети: {cl('x' + str(scale), 'magenta')}"
         f"   → {src.width*scale}×{src.height*scale}, затем точно до {w}×{h}")

    work = TEMP_ROOT / f"ai_{int(time.time())}_{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    ups_root = work / "upscaled"
    ups_root.mkdir(exist_ok=True)

    chunk = ai_chunk_frames(src.width, src.height)
    info(f"Обработка порциями по {cl(str(chunk), 'cyan')} кадров "
         f"(всего ~{total_frames}) — диск не переполнится.")

    done_frames = 0
    t_start = time.time()
    try:
        while done_frames < total_frames:
            n = min(chunk, total_frames - done_frames)
            f_in = work / "in"
            f_out = work / "out"
            shutil.rmtree(f_in, ignore_errors=True)
            shutil.rmtree(f_out, ignore_errors=True)
            f_in.mkdir(parents=True, exist_ok=True)
            f_out.mkdir(parents=True, exist_ok=True)

            start_frame = done_frames + 1
            end_frame = done_frames + n
            print()
            print(cl(f"  Порция {start_frame}–{end_frame} из ~{total_frames}", "bold", "blue"))

            # --- извлечение кадров (глобальная нумерация) ---
            ss = done_frames / fps
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", f"{ss:.4f}", "-i", str(src.path),
                   "-frames:v", str(n), "-map", "0:v:0", "-fps_mode", "passthrough",
                   "-pix_fmt", "rgb24", "-compression_level", "3",
                   "-start_number", str(start_frame),
                   str(f_in / "f_%08d.png")]
            rc = run_ffmpeg(cmd, n / fps, "    извлечение")
            got = sorted(f_in.glob("f_*.png"))
            if not got:
                warn("Кадры закончились раньше времени.")
                break

            # --- нейросеть ---
            args = [RES_EXE, "-i", str(f_in), "-o", str(f_out),
                    "-n", model, "-s", str(scale), "-m", RES_MODELS,
                    "-j", threads, "-f", "png"]
            t0 = time.time()
            p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace",
                                 creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
            shown = -1
            buf = ""
            assert p.stdout is not None
            while True:
                ch = p.stdout.read(1)
                if not ch:
                    break
                buf += ch
                if ch == "\n":
                    m = re.search(r"([\d.]+)%", buf)
                    if m:
                        pct = float(m.group(1))
                        if int(pct) != shown:
                            shown = int(pct)
                            width = 26
                            filled = int(width * pct / 100)
                            bar = "█" * filled + "░" * (width - filled)
                            overall = (done_frames + len(got) * pct / 100) / total_frames
                            el = time.time() - t_start
                            eta = (el / max(overall, 0.01)) * (1 - overall)
                            sys.stdout.write(
                                (f"\r    AI {cl(bar, 'magenta')} {pct:5.1f}%  "
                                 f"| всего {overall*100:4.1f}%  осталось ~{fmt_time(eta)}  ")[:116].ljust(116))
                            sys.stdout.flush()
                    buf = ""
            p.wait()
            sys.stdout.write("\n")
            if p.returncode != 0:
                err(f"Real-ESRGAN вернул код {p.returncode}")
                return False

            ups = sorted(f_out.glob("*.png"))
            if not ups:
                err("Нейросеть не вернула кадры.")
                return False
            for f in ups:
                shutil.move(str(f), str(ups_root / f.name))
            info(f"Готово за {fmt_time(time.time()-t0)}: {len(ups)} кадр(ов)")

            done_frames += len(got)
            if len(got) < n:
                break       # видео короче, чем ожидалось
            shutil.rmtree(f_in, ignore_errors=True)
            shutil.rmtree(f_out, ignore_errors=True)

        all_frames = sorted(ups_root.glob("*.png"))
        if not all_frames:
            err("Нет улучшенных кадров.")
            return False
        ok(f"Улучшено всего кадров: {len(all_frames)}")

        # --- сборка через concat demuxer ---
        print()
        print(cl("  Сборка видео + звук", "bold"))
        list_file = work / "frames.txt"
        frame_dur = 1.0 / fps
        with open(list_file, "w", encoding="utf-8") as fh:
            for f in all_frames:
                fh.write(f"file '{f.as_posix()}'\n")
                fh.write(f"duration {frame_dur:.8f}\n")

        cmd = [FFMPEG, "-hide_banner", "-y",
               "-f", "concat", "-safe", "0", "-i", str(list_file)]
        if src.has_audio:
            cmd += ["-i", str(src.path),
                    "-map", "0:v:0", "-map", "1:a?",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        else:
            cmd += ["-an"]
        cmd += ["-fps_mode", "passthrough",
                "-vf", f"scale={w}:{h}:flags=lanczos,format=yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                "-x264-params", "ref=%d:rc-lookahead=%d" % x264_mem_guard(
                    w, h, 3, 40, max(1, os.cpu_count() or 2)),
                "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "-shortest", str(out)]

        rc = run_ffmpeg(cmd, dur, "  Кодирование H.264")
        if rc != 0:
            err("Не удалось собрать итоговое видео.")
            return False
        return out.exists() and out.stat().st_size > 1000

    except KeyboardInterrupt:
        print()
        warn("Прервано (Ctrl+C). Временные файлы удалены.")
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)
        purge_temp()          # не оставляем НИЧЕГО во временной папке


def make_comparison(src_path: Path, out_path: Path, when: float = -1.0) -> Optional[Path]:
    """Делает кадр «ДО | ПОСЛЕ» — удобно сравнить качество глазами."""
    try:
        if not FFMPEG:
            return None
        s_info, o_info = MediaInfo(src_path), MediaInfo(out_path)
        if not s_info.valid or not o_info.valid:
            return None
        base = min(d for d in (s_info.duration, o_info.duration) if d)
        t = when if when >= 0 else max(base * 0.35, 0.04)
        t = min(t, max(base - 0.05, 0.0))
        h = min(max(s_info.height, 360), 1080)
        if h % 2:
            h += 1
        dest = out_path.with_name(out_path.stem + "_сравнение.jpg")
        font = ""
        for fp in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                   "/usr/share/fonts/TTF/DejaVuSans.ttf",
                   "/System/Library/Fonts/Helvetica.ttc"):
            if os.path.exists(fp):
                font = ":fontfile=" + fp.replace(":", "\\:")
                break
        fs = max(24, h // 14)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{t:.3f}", "-i", str(src_path),
               "-ss", f"{t:.3f}", "-i", str(out_path),
               "-filter_complex",
               f"[0:v]scale=-2:{h}:flags=lanczos,drawtext=text='ДО':x=20:y=20:"
               f"fontsize={fs}:fontcolor=white:box=1:boxcolor=black@0.55{font}[l];"
               f"[1:v]scale=-2:{h}:flags=lanczos,drawtext=text='ПОСЛЕ':x=20:y=20:"
               f"fontsize={fs}:fontcolor=white:box=1:boxcolor=black@0.55{font}[r];"
               f"[l][r]hstack=inputs=2[v]",
               "-map", "[v]", "-frames:v", "1", "-q:v", "2", str(dest)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
        if r.returncode == 0 and dest.exists():
            return dest
        # drawtext может быть недоступен — пробуем без подписей
        cmd2 = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{t:.3f}", "-i", str(src_path),
                "-ss", f"{t:.3f}", "-i", str(out_path),
                "-filter_complex",
                f"[0:v]scale=-2:{h}[l];[1:v]scale=-2:{h}[r];[l][r]hstack=inputs=2[v]",
                "-map", "[v]", "-frames:v", "1", "-q:v", "2", str(dest)]
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=300,
                            creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
        if r2.returncode == 0 and dest.exists():
            return dest
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------- #
#  Имя выходного файла
# ----------------------------------------------------------------------------- #
def make_output_path(src: Path, w: int, h: int, engine: str) -> Path:
    """Готовые видео складываем в папку result/ внутри папки программы."""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    tag = "AI" if engine == "ai" else "HD"
    name = f"{src.stem}_{w}x{h}_{tag}.mp4"
    out = RESULT_DIR / name
    i = 2
    while out.exists():
        out = RESULT_DIR / f"{src.stem}_{w}x{h}_{tag}_{i}.mp4"
        i += 1
    return out


# ----------------------------------------------------------------------------- #
#  Обработка одного файла (интерактив)
# ----------------------------------------------------------------------------- #
def process_one(path: Path, auto: Optional[dict] = None) -> bool:
    global FFMPEG, FFPROBE
    if not FFMPEG:
        err("FFmpeg не найден — без него программа работать не может.")
        if IS_WIN and ask_yes("Скачать и установить FFmpeg автоматически?", True):
            if install_ffmpeg():
                FFMPEG, FFPROBE = find_ffmpeg()
        if not FFMPEG:
            return False

    src = MediaInfo(path)
    if not src.valid:
        err(f"Не удалось прочитать видео: {path}")
        return False
    src.show()

    auto = auto or {}

    # --- выбор движка ---
    engine = auto.get("engine")
    if not engine:
        ai_ready = bool(RES_EXE and RES_MODELS)
        opts = [
            ("1", f"БЫСТРЫЙ — ffmpeg (апскейл + резкость + шумодав + цвет). "
                  f"{cl('работает всегда', 'green')}"),
            ("2", f"AI — Real-ESRGAN (нейросеть дорисовывает детали). "
                  f"{cl('лучшее качество', 'magenta')} "
                  f"{'' if ai_ready else cl('[не установлен — предложу скачать]', 'yellow')}"),
        ]
        c = ask_choice("Какой движок используем?", opts, "1",
                       "Совет: для фильмов и фото-видео бери AI; если нет видеокарты — БЫСТРЫЙ.")
        engine = "fast" if c == "1" else "ai"

    # --- целевое разрешение ---
    tw, th = auto.get("size", (0, 0))
    if not tw and auto.get("fit_x2"):
        tw, th = src.width * 2, src.height * 2
    if not tw:
        opts = []
        for key, (w, h, label) in TARGETS.items():
            if w <= src.width and h <= src.height:
                continue
            cur = "  ← текущее" if (w == src.width and h == src.height) else ""
            opts.append((key, f"{w}×{h}  {label}{cur}"))
        opts.append(("x2", f"Увеличить в 2 раза  →  {src.width*2}×{src.height*2}"))
        opts.append(("x3", f"Увеличить в 3 раза  →  {src.width*3}×{src.height*3}"))
        opts.append(("custom", "Своё разрешение (ввести вручную)"))
        default = "4k" if any(k == "4k" for k, _ in opts) else (opts[0][0] if opts else "x2")
        c = ask_choice(f"Куда поднимаем? (сейчас {src.width}×{src.height})", opts, default)
        if c in TARGETS:
            tw, th = TARGETS[c][0], TARGETS[c][1]
        elif c == "x2":
            tw, th = src.width * 2, src.height * 2
        elif c == "x3":
            tw, th = src.width * 3, src.height * 3
        else:
            v = ask("Ширина x высота (например 2560x1440)", f"{src.width*2}x{src.height*2}")
            m = re.match(r"\s*(\d+)\s*[xх*]\s*(\d+)", v.lower())
            if not m:
                err("Не понял. Беру 4K.")
                tw, th = 3840, 2160
            else:
                tw, th = int(m.group(1)), int(m.group(2))

    w, h = target_size(src, tw, th, auto.get("fit", "fit"))

    # --- пресет (для быстрого) / модель (для AI) ---
    preset = PRESETS.get(auto.get("preset", ""), PRESETS["balanced"])
    model_key = auto.get("model", "")
    fps_out = auto.get("fps")          # None = спросить, 0 = не менять
    if fps_out is None:
        fps_out = 0.0
    preview_sec = auto.get("preview", 0.0) or 0.0

    if engine == "fast":
        if not auto.get("preset"):
            opts = [(k, v["name"]) for k, v in PRESETS.items()]
            c = ask_choice("Пресет обработки?", opts, "balanced")
            preset = PRESETS[c]
        if auto.get("fps") is None:
            opts = [("0", f"Оставить {src.fps or '?'} fps (быстро)"),
                    ("30", "Интерполяция до 30 fps"),
                    ("60", "Интерполяция до 60 fps (плавно, но МЕДЛЕННО)")]
            c = ask_choice("Повысить плавность (fps)?", opts, "0",
                           "minterpolate считает движение между кадрами — на 4K это долго.")
            fps_out = {"0": 0.0, "30": 30.0, "60": 60.0}[c]
    else:
        if not RES_EXE or not RES_MODELS:
            warn("AI-движок ещё не установлен.")
            if not (IS_WIN and ask_yes("Скачать Real-ESRGAN + модели (~45 МБ)?", True)
                    and install_realesrgan()):
                warn("Переключаюсь на БЫСТРЫЙ движок.")
                engine = "fast"
        if engine == "ai":
            has_gpu, dev = gpu_is_available()
            print()
            if has_gpu:
                ok(f"Видеокарта найдена: {cl(dev, 'green')}")
            else:
                warn(f"Быстрая видеокарта (Vulkan) не найдена: {dev}")
                warn("AI-обработка без видеокарты будет ОЧЕНЬ медленной.")
                if auto.get("engine") == "ai":
                    warn("Но раз движок AI выбран явно — продолжаю с AI, как просили.")
                elif not preview_sec and ask_yes("Всё равно попробовать AI?", False):
                    pass
                else:
                    info("Переключаюсь на БЫСТРЫЙ движок (он тоже даёт хорошую картинку).")
                    engine = "fast"
        if engine == "ai":
            if not model_key:
                opts = [(k, v["name"]) for k, v in AI_MODELS.items()]
                model_key = ask_choice("Какая модель нейросети?", opts, "general")
            if not preview_sec:
                if ask_yes("Сделать сначала тестовый фрагмент 5 секунд?", True):
                    preview_sec = 5.0

    out = auto.get("out") or make_output_path(src.path, w, h, engine)
    if preview_sec and not auto.get("out"):
        RESULT_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULT_DIR / f"{src.path.stem}_{w}x{h}_AI_превью.mp4"

    # --- подтверждение ---
    print()
    hr("─", 74, "dim")
    print(f"  {cl('Итог:', 'bold')} {src.width}×{src.height}  →  "
          f"{cl(f'{w}×{h}', 'green', 'bold')}   "
          f"движок: {cl('AI Real-ESRGAN' if engine == 'ai' else 'Быстрый (ffmpeg)', 'cyan')}")
    print(f"  {cl('Файл:', 'bold')} {out}")
    est = src.duration * (0.35 if engine == "fast" else 1)
    print(f"  {cl('Оценка времени:', 'bold')} ~{fmt_time(max(est, 5))}"
          + (" (AI — сильно зависит от видеокарты)" if engine == "ai" else ""))
    hr("─", 74, "dim")
    if not auto.get("yes"):
        if not ask_yes("Запускаем?", True):
            warn("Отменено.")
            return False

    t0 = time.time()
    try:
        if engine == "ai":
            good = run_ai_engine(src, out, w, h, model_key or "general",
                                 crf=17, threads="1:2:2", preview_sec=preview_sec)
        else:
            good = run_fast_engine(src, out, w, h, preset, fps_out)
    except KeyboardInterrupt:
        print()
        warn("Прервано (Ctrl+C).")
        return False

    dt = time.time() - t0
    print()
    if good and preview_sec and engine == "ai":
        ok(f"Тестовый фрагмент готов: {cl(str(out), 'dim')}")
        info("Посмотри его и кадр «сравнение» — так будет выглядеть всё видео.")
        interactive = sys.stdin.isatty()
        if interactive and ask_yes("Обработать теперь ВСЁ видео с этими настройками?", True):
            a2 = dict(auto)
            a2.update({"preview": 0.0, "yes": True, "engine": "ai",
                       "model": model_key or "general", "size": (tw, th),
                       "out": None})
            return process_one(path, a2)
        if not interactive:
            info("Неинтерактивный режим: полный прогон не запускаю. "
                 "Убери --preview, чтобы обработать всё видео сразу.")
        return True
    if good:
        fin = MediaInfo(out)
        title("ГОТОВО!")
        ok(f"Файл: {cl(str(out), 'bold', 'green')}")
        info(f"Разрешение: {fin.width}×{fin.height}   Размер: {human(out.stat().st_size)}"
             f"   Время обработки: {fmt_time(dt)}")
        if not auto.get("no_preview"):
            cmp_img = make_comparison(src.path, out)
            if cmp_img:
                ok(f"Кадр «ДО | ПОСЛЕ»: {cl(str(cmp_img), 'dim')}")
        if IS_WIN:
            if ask_yes("Открыть папку с результатом?", False):
                try:
                    subprocess.Popen(["explorer", "/select,", str(out)])
                except Exception:
                    pass
        return True
    err("Что-то пошло не так. Файл не создан.")
    return False


# ----------------------------------------------------------------------------- #
#  Пакетная обработка
# ----------------------------------------------------------------------------- #
def process_batch(folder: Path, auto: dict) -> None:
    vids = sorted([f for f in folder.iterdir()
                   if f.is_file() and f.suffix.lower() in VIDEO_EXTS])
    if not vids:
        err("Видео в папке не найдено.")
        return
    print()
    title(f"Пакетная обработка · {len(vids)} файл(ов)")
    for i, v in enumerate(vids, 1):
        print()
        print(cl(f"  ── [{i}/{len(vids)}] {v.name} " + "─" * 20, "bold", "blue"))
        a = dict(auto)
        a["yes"] = True
        try:
            process_one(v, a)
        except Exception as e:
            err(f"Сбой на {v.name}: {e}")
    print()
    ok("Пакетная обработка завершена.")


# ----------------------------------------------------------------------------- #
#  Главное меню
# ----------------------------------------------------------------------------- #
BANNER = r"""
 ██╗   ██╗██╗██████╗ ███████╗ █████╗  ██████╗
 ██║   ██║██║██╔══██╗██╔════╝██╔══██╗██╔════╝
 ██║   ██║██║██║  ██║█████╗  ███████║██║
 ╚██╗ ██╔╝██║██║  ██║██╔══╝  ██╔══██║██║
  ╚████╔╝ ██║██████╔╝███████╗██║  ██║╚██████╗
   ╚═══╝  ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝
"""


def show_status() -> None:
    print(cl(BANNER, "cyan"))
    print(cl("   А П С Е Й Л Е Р   В И Д Е О   ·   бесплатно и без ограничений", "bold"))
    print(cl("   улучшение качества и повышение разрешения до 4K / 8K", "dim"))
    print()
    if FFMPEG:
        ok(f"FFmpeg: {cl(FFMPEG, 'dim')}")
    else:
        err("FFmpeg: НЕ НАЙДЕН  (программа предложит установить)")
    if RES_EXE and RES_MODELS:
        ok(f"AI Real-ESRGAN: {cl(RES_EXE, 'dim')}")
    else:
        warn("AI Real-ESRGAN: не установлен (можно скачать из меню, ~45 МБ)")
    try:
        import cv2  # noqa: F401
        ok("OpenCV: установлен")
    except Exception:
        info("OpenCV: не установлен (не обязателен)")
    print()


def main_menu() -> None:
    show_status()
    while True:
        opts = [
            ("1", cl("Улучшить видео", "bold", "green") + "  — открыть файл или папку"),
            ("2", "Пакетная обработка папки (все видео сразу)"),
            ("3", "Установить / обновить FFmpeg"),
            ("4", "Установить AI-движок Real-ESRGAN (нейросеть)"),
            ("5", "Проверить видеокарту (Vulkan) и готовность AI"),
            ("6", "Открыть папку программы"),
            ("0", cl("Выход", "red")),
        ]
        c = ask_choice("ГЛАВНОЕ МЕНЮ", opts, "1")
        if c == "0":
            print()
            print(cl("  Пока! Хорошего качества картинке 😉", "bold", "cyan"))
            return
        elif c == "1":
            p = pick_file()
            if p is None:
                continue
            if p.is_dir():
                process_batch(p, {})
            else:
                process_one(p)
        elif c == "2":
            raw = ask("Папка с видео (можно перетащить её в окно)", "")
            if not raw:
                continue
            p = clean_path(raw)
            if p.is_dir():
                process_batch(p, ask_settings())
            elif p.is_file():
                process_one(p, {})
            else:
                err("Папка не найдена.")
        elif c == "3":
            global FFMPEG, FFPROBE
            if FFMPEG:
                ok(f"Уже установлен: {FFMPEG}")
                if not ask_yes("Переустановить?", False):
                    continue
            install_ffmpeg()
            FFMPEG, FFPROBE = find_ffmpeg()
        elif c == "4":
            if RES_EXE:
                ok(f"Уже установлен: {RES_EXE}")
                if not ask_yes("Переустановить?", False):
                    continue
            install_realesrgan()
        elif c == "5":
            print()
            if not RES_EXE:
                warn("AI-движок не установлен — сначала пункт 4.")
                continue
            has, dev = gpu_is_available()
            if has:
                ok(f"Видеокарта готова к AI: {cl(dev, 'green')}")
                info("Можно смело брать движок AI — будет быстро.")
            else:
                err(f"Подходящего Vulkan-устройства нет: {dev}")
                info("Рекомендую БЫСТРЫЙ движок. AI на процессоре = очень медленно.")
        elif c == "6":
            if IS_WIN:
                try:
                    os.startfile(str(BASE_DIR))  # type: ignore[attr-defined]
                except Exception:
                    pass
            else:
                info(str(BASE_DIR))
        print()


# ----------------------------------------------------------------------------- #
#  CLI-режим
# ----------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="upscaler.py",
        description="Бесплатный апскейлер видео до HD/4K/8K (ffmpeg + Real-ESRGAN AI).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python upscaler.py                                    # интерактивное меню
  python upscaler.py "D:\\video\\clip.mp4" --target 4k     # быстро, в 4K
  python upscaler.py clip.mp4 --engine ai --target 1080 # нейросетью в Full HD
  python upscaler.py clip.mp4 --target x2 --preset max  # увеличить вдвое
  python upscaler.py "D:\\Папка"  --batch --engine fast --target 4k --yes
""")
    p.add_argument("input", nargs="?", help="видеофайл или папка")
    p.add_argument("--engine", "-e", choices=["fast", "ai"], help="движок")
    p.add_argument("--target", "-t", default=None,
                   help="720 | 1080 | 1440 | 4k | 8k | x2 | x3 | ШИРИНАxВЫСОТА")
    p.add_argument("--preset", "-p", choices=list(PRESETS), default="balanced",
                   help="пресет быстрого движка")
    p.add_argument("--model", "-m", choices=list(AI_MODELS), default="general",
                   help="модель Real-ESRGAN")
    p.add_argument("--fps", type=float, default=0, help="интерполяция до N fps (0 = не менять)")
    p.add_argument("--out", "-o", default=None, help="имя выходного файла")
    p.add_argument("--preview", type=float, default=0,
                   help="обработать только первые N секунд (тест)")
    p.add_argument("--no-preview", action="store_true",
                   help="не создавать кадр «ДО/ПОСЛЕ»")
    p.add_argument("--batch", action="store_true", help="обработать всю папку")
    p.add_argument("--yes", "-y", action="store_true", help="без подтверждения")
    p.add_argument("--install-ffmpeg", action="store_true", help="установить ffmpeg и выйти")
    p.add_argument("--install-ai", action="store_true", help="установить Real-ESRGAN и выйти")
    p.add_argument("--check-gpu", action="store_true", help="проверить Vulkan/GPU и выйти")
    return p


def resolve_target(spec: Optional[str], src: MediaInfo) -> Tuple[int, int]:
    if not spec:
        return 3840, 2160
    s = spec.strip().lower()
    if s in TARGETS:
        return TARGETS[s][0], TARGETS[s][1]
    if s in ("x2", "2x"):
        return src.width * 2, src.height * 2
    if s in ("x3", "3x"):
        return src.width * 3, src.height * 3
    if s in ("x4", "4x"):
        return src.width * 4, src.height * 4
    m = re.match(r"(\d+)\s*[xх*]\s*(\d+)", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    raise ValueError(f"Не понял --target '{spec}'. Варианты: 720 1080 1440 4k 8k x2 x3 2560x1440")


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()
    purge_temp()          # убираем хвосты от прошлых (прерванных) запусков

    global FFMPEG, FFPROBE, RES_EXE, RES_MODELS

    if args.install_ffmpeg:
        FFMPEG, FFPROBE = find_ffmpeg()
        if FFMPEG and FFPROBE:
            ok(f"FFmpeg уже установлен: {FFMPEG}")
            return 0
        return 0 if install_ffmpeg() else 1
    if args.install_ai:
        RES_EXE, RES_MODELS = find_realesrgan()
        return 0 if (RES_EXE or install_realesrgan()) else 1
    if args.check_gpu:
        has, dev = gpu_is_available()
        print(("GPU OK: " if has else "GPU NOT FOUND: ") + dev)
        return 0 if has else 2

    if not args.input:
        try:
            main_menu()
        except KeyboardInterrupt:
            print()
            warn("Выход (Ctrl+C).")
        return 0

    src_path = clean_path(args.input)
    if src_path.is_dir() or args.batch:
        auto = {
            "engine": args.engine or "fast",
            "preset": args.preset,
            "model": args.model,
            "fps": args.fps if args.fps else 0.0,
            "yes": True,
            "preview": args.preview,
        }
        if args.target:
            # для пакета считаем от первого файла
            first = next((f for f in src_path.iterdir()
                          if f.suffix.lower() in VIDEO_EXTS), None)
            if first:
                auto["size"] = resolve_target(args.target, MediaInfo(first))
        process_batch(src_path, auto)
        return 0

    if not src_path.exists():
        err(f"Файл не найден: {src_path}")
        return 1

    src = MediaInfo(src_path)
    auto = {
        "engine": args.engine,
        "preset": args.preset,
        "model": args.model,
        "fps": args.fps if args.fps else 0.0,
        "yes": args.yes,
        "preview": args.preview,
        "no_preview": args.no_preview,
    }
    if args.target:
        auto["size"] = resolve_target(args.target, src)
    if args.out:
        auto["out"] = Path(args.out)
    return 0 if process_one(src_path, auto) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
