#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Video Upscaler — original-style console, bounded cache, playable growing MP4.
Python >= 3.9. Runtime dependencies: FFmpeg + FFprobe; AI: Real-ESRGAN ncnn Vulkan.
No pip dependencies. See README.md for guarantees and limitations.
"""
from __future__ import annotations

import argparse
import contextlib
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import copy
import time
import urllib.request
import unicodedata
import zipfile

__version__ = '1.0.1'
BASE = Path(__file__).resolve().parent
WIN = os.name == 'nt'
EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.mpg', '.mpeg',
        '.ts', '.flv', '.wmv', '.3gp', '.vob', '.mts', '.m2ts', '.ogv'}
TARGETS = {'720': (1280, 720), '1080': (1920, 1080), '1440': (2560, 1440),
           '4k': (3840, 2160), '8k': (7680, 4320)}
MODELS = {'general': ('realesrgan-x4plus', (4,)),
          'anime': ('realesrgan-x4plus-anime', (4,)),
          'fast': ('realesr-animevideov3', (2, 3, 4))}
PRESETS = {'soft': (0, 0), 'balanced': (0.6, 0.45),
           'max': (1.3, 0.75), 'restore': (2.4, 0.45)}
PNG_SIG = b'\x89PNG\r\n\x1a\n'
MIB = 1024 ** 2
GIB = 1024 ** 3
ACTIVE = set()
_ACTIVE_PROGRESS = None
_OUTPUT_LOCK = threading.RLock()


def say(s):
    progress = _ACTIVE_PROGRESS
    if progress is not None and not progress.closed:
        progress.notice(s)
        return
    with _OUTPUT_LOCK:
        print(s, flush=True)


def stop(p):
    if p.poll() is None:
        with contextlib.suppress(OSError):
            p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                p.kill()
            p.wait()


def terminate(signum, frame):
    raise KeyboardInterrupt


@contextlib.contextmanager
def child(cmd, **kwargs):
    p = subprocess.Popen([str(x) for x in cmd], **kwargs)
    ACTIVE.add(p)
    try:
        yield p
    finally:
        stop(p)
        for stream in (p.stdin, p.stdout, p.stderr):
            if stream:
                with contextlib.suppress(OSError):
                    stream.close()
        ACTIVE.discard(p)


def executable(name, env=None):
    filename = name + ('.exe' if WIN else '')
    if env and os.environ.get(env):
        p = Path(os.environ[env]).expanduser()
        if p.is_dir():
            p = p / filename
        if p.is_file():
            return str(p.resolve())
    for folder in (BASE / 'tools', BASE / 'apps', BASE / 'assets', BASE):
        p = folder / filename
        if p.is_file():
            return str(p)
    # Do not recursively scan the whole project, input videos or cache.
    for folder in (BASE / 'tools', BASE / 'apps'):
        if folder.is_dir():
            found = next(folder.rglob(filename), None)
            if found:
                return str(found)
    return shutil.which(name)


def tools():
    ff = executable('ffmpeg', 'FFMPEG_PATH')
    probe = executable('ffprobe', 'FFPROBE_PATH')
    if ff and not probe:
        sibling = Path(ff).with_name('ffprobe.exe' if WIN else 'ffprobe')
        if sibling.is_file():
            probe = str(sibling)
    if not ff or not probe:
        raise RuntimeError('Нужны FFmpeg И FFprobe в PATH или tools/. Windows: --install-ffmpeg; '
                           'Linux: sudo apt install ffmpeg; macOS: brew install ffmpeg')
    return ff, probe


def ai_tools():
    exe = executable('realesrgan-ncnn-vulkan', 'RES_PATH')
    candidates = [Path(os.environ['RES_MODELS']).expanduser()] if os.environ.get('RES_MODELS') else []
    if exe:
        candidates += [Path(exe).parent / 'models', BASE / 'assets' / 'realesrgan-models']
    models = next((p.resolve() for p in candidates if p.is_dir()), None)
    if not exe or not models:
        raise RuntimeError('Не найден Real-ESRGAN или models/. Windows: --install-ai; '
                           'другие ОС: распакуйте сборку в apps/. RES_PATH — бинарник или его папка, '
                           'RES_MODELS — папка моделей.')
    if not WIN:
        Path(exe).chmod(Path(exe).stat().st_mode | 0o111)
    return exe, str(models)


def safe_extract(archive, dest):
    dest = dest.resolve()
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            p = (dest / item.filename).resolve()
            if p != dest and dest not in p.parents:
                raise RuntimeError('Небезопасный путь в ZIP')
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError('Символические ссылки в ZIP запрещены')
        z.extractall(dest)


def install(kind):
    if not WIN:
        raise RuntimeError('Автозагрузка только для Windows x64. Для Linux/macOS установите '
                           'FFmpeg пакетным менеджером, Real-ESRGAN — сборку под вашу ОС.')
    if kind == 'ffmpeg':
        url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
        dest = BASE / 'tools'
    else:
        url = ('https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/'
               'realesrgan-ncnn-vulkan-20220424-windows.zip')
        dest = BASE / 'apps' / 'realesrgan'
    dest.mkdir(parents=True, exist_ok=True)
    say('Скачивание официального архива: ' + url)
    with tempfile.TemporaryDirectory(prefix='install_', dir=dest) as tmp:
        archive = Path(tmp) / 'download.zip'
        with urllib.request.urlopen(url, timeout=60) as response, archive.open('wb') as f:
            shutil.copyfileobj(response, f)
        unpacked = Path(tmp) / 'unpacked'
        safe_extract(archive, unpacked)
        if kind == 'ffmpeg':
            for name in ('ffmpeg.exe', 'ffprobe.exe'):
                src = next(unpacked.rglob(name), None)
                if not src:
                    raise RuntimeError('В архиве не найден ' + name)
                shutil.copy2(src, dest / name)
        else:
            src = next(unpacked.rglob('realesrgan-ncnn-vulkan.exe'), None)
            if not src or not (src.parent / 'models').is_dir():
                raise RuntimeError('Неполный архив Real-ESRGAN')
            shutil.copytree(src.parent, dest, dirs_exist_ok=True)
    say('Установлено: ' + str(dest))


def number(raw, default=0.0):
    try:
        value = float(Fraction(str(raw)))
        return value if math.isfinite(value) else default
    except (ValueError, ZeroDivisionError, TypeError):
        return default


def inspect(path, ffprobe):
    r = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-show_format',
                        '-of', 'json', str(path)], capture_output=True, timeout=120,
                       encoding='utf-8', errors='replace')
    if r.returncode:
        raise RuntimeError('FFprobe: ' + r.stderr[-3000:])
    data = json.loads(r.stdout)
    videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    if not videos:
        raise RuntimeError('Видеопоток не найден: ' + str(path))
    v = videos[0]
    w, h = int(v['width']), int(v['height'])
    sar = number(v.get('sample_aspect_ratio', '1:1').replace(':', '/'), 1) or 1
    # FFmpeg autorotates; normalize non-square pixels to square pixels too.
    w = max(2, round(w * sar))
    rotation = number(v.get('tags', {}).get('rotate', 0))
    for side in v.get('side_data_list', []):
        rotation = number(side.get('rotation', rotation))
    if abs(round(rotation)) % 180 == 90:
        w, h = h, w
    rate = v.get('avg_frame_rate')
    if number(rate) <= 0:
        rate = v.get('r_frame_rate', '25/1')
    if number(rate) <= 0:
        rate = '25/1'
    rate = str(Fraction(str(rate)).limit_denominator(1000000))
    return dict(width=w, height=h, rate=rate, fps=number(rate), index=v['index'],
                duration=number(v.get('duration')) or number(data.get('format', {}).get('duration')),
                audio=any(s.get('codec_type') == 'audio' for s in data.get('streams', [])),
                hdr=v.get('color_transfer') in ('smpte2084', 'arib-std-b67'),
                interlaced=v.get('field_order') in ('tt', 'bb', 'tb', 'bt'),
                stream=v)


def dimensions(spec, media):
    spec = spec.lower().strip()
    sw, sh = media['width'], media['height']
    if spec in TARGETS:
        tw, th = TARGETS[spec]
    elif re.fullmatch(r'x[234]|[234]x', spec):
        factor = int(spec.replace('x', ''))
        tw, th = sw * factor, sh * factor
    else:
        match = re.fullmatch(r'(\d+)\s*[xх*]\s*(\d+)', spec)
        if not match:
            raise ValueError('Неверный --target: ' + spec)
        tw, th = map(int, match.groups())
    if min(tw, th) < 2 or max(tw, th) > 16384:
        raise ValueError('Размеры должны быть от 2 до 16384')
    factor = min(tw / sw, th / sh)
    # Floor, not round: never exceed the target bounding box.
    return max(2, int(sw * factor) // 2 * 2), max(2, int(sh * factor) // 2 * 2)


def ai_scale(media, w, h, key):
    scales = MODELS[key][1]
    need = max(w / media['width'], h / media['height'])
    return next((s for s in scales if s >= need - 1e-6), scales[-1])


def cache_plan(width, height, scale, budget_mib, available, reserve, requested=0):
    # RGB/RGBA PNG's deflate worst case is slightly larger than raw data.
    # Include alpha, row filters, PNG chunks and conservative 15% overhead.
    pair = math.ceil(width * height * 4 * (1 + scale * scale) * 1.15 + 2 * MIB)
    budget = min(int(budget_mib * MIB), available - reserve)
    if budget < pair:
        raise RuntimeError(f'Для ОДНОЙ пары кадров нужен бюджет примерно {pair/MIB:.0f} МиБ. '
                           f'Доступный бюджет {max(0, budget)/MIB:.0f} МиБ. '
                           'Увеличьте --cache-mb/освободите диск или используйте модель fast с x2/x3.')
    count = max(1, min(120, budget // pair))
    if requested:
        count = min(count, requested)
    return int(count), pair


def free_check(path, needed, reserve):
    free = shutil.disk_usage(path).free
    if free < needed + reserve:
        raise RuntimeError(f'Мало места на {path}: свободно {free/GIB:.2f} ГиБ, '
                           f'нужно ещё {needed/GIB:.2f} ГиБ + резерв {reserve/GIB:.2f} ГиБ. '
                           'Обработка остановлена, кэш будет удалён.')


def tail(path):
    try:
        with Path(path).open('rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 6000))
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def read_exact(stream, n, eof_ok=False):
    chunks = bytearray()
    while len(chunks) < n:
        block = stream.read(n - len(chunks))
        if not block:
            if eof_ok and not chunks:
                return None
            raise RuntimeError('Оборванный PNG-поток FFmpeg')
        chunks.extend(block)
    return bytes(chunks)


def read_png_to(stream, path, max_bytes):
    """Read one PNG, never buffering a complete frame in Python RAM."""
    sig = read_exact(stream, 8, eof_ok=True)
    if sig is None:
        return False
    if sig != PNG_SIG:
        raise RuntimeError('FFmpeg вернул не PNG')
    size = 8
    with path.open('wb') as out:
        out.write(sig)
        while True:
            header = read_exact(stream, 8)
            length, kind = struct.unpack('>I4s', header)
            size += 12 + length
            if size > max_bytes:
                raise RuntimeError('PNG превысил расчётный размер кэша')
            out.write(header)
            remaining = length + 4  # data + CRC
            while remaining:
                n = min(remaining, MIB)
                out.write(read_exact(stream, n))
                remaining -= n
            if kind == b'IEND':
                return True


def encoder_options(args):
    live = args.live
    if args.codec == 'h265':
        params = f'pools={args.threads}:frame-threads={1 if live else 2}:rc-lookahead={0 if live else 12}'
        if live:
            params += ':bframes=0'
        result = ['-c:v', 'libx265', '-preset', args.encode_preset, '-crf', str(args.crf),
                  '-x265-params', params, '-tag:v', 'hvc1', '-pix_fmt', 'yuv420p']
        if live:
            result += ['-tune', 'zerolatency']
        return result
    if args.codec == 'nvenc':
        result = ['-c:v', 'h264_nvenc', '-preset', 'p4', '-rc', 'vbr', '-cq', str(args.crf),
                  '-b:v', '0', '-pix_fmt', 'yuv420p']
        if live:
            result += ['-bf', '0', '-rc-lookahead', '0', '-zerolatency', '1']
        return result
    result = ['-c:v', 'libx264', '-preset', args.encode_preset, '-crf', str(args.crf),
              '-threads', str(args.threads), '-x264-params',
              'ref=2:rc-lookahead=' + ('0' if live else '12'), '-pix_fmt', 'yuv420p']
    if live:
        result += ['-tune', 'zerolatency', '-bf', '0']
    return result


def output_args(args, media):
    result = encoder_options(args)
    if media['audio']:
        result += ['-c:a', 'aac', '-b:a', '192k', '-af', 'apad', '-shortest']
    else:
        result += ['-an']
    if args.live:
        # Initialization is written first; each complete moof+mdat is playable
        # without a final moov pass. No full-file rewrite and no second copy.
        result += ['-movflags', '+empty_moov+default_base_moof+frag_keyframe+negative_cts_offsets',
                   '-frag_duration', '1000000', '-flush_packets', '1',
                   '-force_key_frames', 'expr:gte(t,n_forced*2)']
    result += ['-max_muxing_queue_size', '1024', '-colorspace', 'bt709']
    return result


def base_filters(media, args, for_ai=False):
    filters = []
    if media['interlaced'] or args.preset == 'restore':
        filters.append('yadif=mode=send_frame:deint=interlaced')
    if for_ai:
        filters += [f"scale={media['width']}:{media['height']}:flags=bicubic", 'setsar=1',
                    f"fps={media['rate']}"]
    return filters


def run_fast(src, media, partial, args, ff, work, progress):
    w, h = dimensions(args.target, media)
    filters = base_filters(media, args)
    denoise, sharp = PRESETS[args.preset]
    if denoise:
        filters += [f'hqdn3d={denoise}:{denoise*0.8}:{denoise*4}:{denoise*3}']
    filters += [f'scale={w}:{h}:flags=lanczos:out_color_matrix=bt709', 'setsar=1']
    if sharp:
        filters += [f'unsharp=5:5:{sharp}:3:3:0']
    if args.fps and args.fps > media['fps'] + 0.5:
        # Interpolate before upscale would be faster, but here favor final quality.
        filters += [f'minterpolate=fps={args.fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir']
    filters += ['format=yuv420p']
    cmd = [ff, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
           '-threads', str(args.threads), '-filter_threads', '2']
    if args.preview:
        cmd += ['-t', str(args.preview)]
    cmd += ['-i', str(src), '-map', f"0:{media['index']}", '-map', '0:a?', '-sn', '-dn',
            '-vf', ','.join(filters)]
    cmd += output_args(args, media) + ['-progress', 'pipe:1', '-nostats', str(partial)]
    log = work / 'fast.log'
    progress.stage('Быстрый апскейл')
    with log.open('wb') as errors, child(cmd, stdout=subprocess.PIPE, stderr=errors) as p:
        for line in iter(p.stdout.readline, b''):
            text = line.decode('ascii', 'replace').strip()
            if text.startswith('frame='):
                progress.update(int(number(text.split('=', 1)[1])))
            elif text.startswith('out_time='):
                free_check(partial.parent, 0, int(args.reserve_gb * GIB))
        rc = p.wait()
    if rc:
        raise RuntimeError('FFmpeg:\n' + tail(log))


def run_ai(src, media, partial, args, ff, work, progress):
    exe, models = ai_tools()
    w, h = dimensions(args.target, media)
    scale = ai_scale(media, w, h, args.model)
    if scale * media['width'] < w or scale * media['height'] < h:
        warn('Цель выше максимального AI-масштаба; остаток увеличит Lanczos.')
    model = MODELS[args.model][0]
    required = ([f'{model}-x{scale}.param', f'{model}-x{scale}.bin']
                if args.model == 'fast' else [f'{model}.param', f'{model}.bin'])
    for name in required:
        if not (Path(models) / name).is_file():
            raise RuntimeError('Нет файла модели: ' + str(Path(models) / name))
    reserve = int(args.reserve_gb * GIB)
    nmax, pair = cache_plan(media['width'], media['height'], scale, args.cache_mb,
                           shutil.disk_usage(work).free, reserve, args.chunk)
    say(f'  AI x{scale}; кэш до {args.cache_mb:g} МиБ; порция до {nmax} кадров '
        f'(консервативно {pair/MIB:.1f} МиБ/пара).')
    say('  После каждой порции PNG кодируются в ОДИН MP4 и немедленно удаляются.')
    say('  VFR нормализуется к CFR ' + media['rate'] + ' fps; исходные VFR-таймстампы не сохраняются.')
    ins, outs = work / 'in', work / 'out'
    ins.mkdir()
    outs.mkdir()
    # Both processes live throughout the file. OS pipes provide backpressure;
    # decoder cannot silently fill the disk while GPU works.
    decode = [ff, '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', str(args.threads),
              '-filter_threads', '2', '-i', str(src), '-map', f"0:{media['index']}", '-an', '-sn', '-dn']
    if args.preview:
        decode += ['-t', str(args.preview)]
    decode += ['-vf', ','.join(base_filters(media, args, True)), '-fps_mode', 'passthrough',
               '-c:v', 'png', '-threads:v', '1', '-pix_fmt', 'rgb24', '-compression_level', '1',
               '-f', 'image2pipe', 'pipe:1']
    encode = [ff, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
              '-threads', '1', '-filter_threads', '2', '-f', 'image2pipe', '-framerate', media['rate'],
              '-probesize', '32', '-analyzeduration', '0', '-fpsprobesize', '0',
              '-c:v', 'png', '-i', 'pipe:0']
    if media['audio']:
        encode += ['-i', str(src), '-map', '0:v:0', '-map', '1:a?']
    else:
        encode += ['-map', '0:v:0']
    # -fps does not pretend to interpolate on the AI path.
    encode += ['-vf', f'scale={w}:{h}:flags=lanczos:out_color_matrix=bt709,setsar=1,format=yuv420p',
               '-fps_mode', 'passthrough']
    encode += output_args(args, media) + [str(partial)]
    dec_log, enc_log, ai_log = work / 'decode.log', work / 'encode.log', work / 'ai.log'
    frames, ended = 0, False
    with dec_log.open('wb') as dl, enc_log.open('wb') as el, \
            child(decode, stdout=subprocess.PIPE, stderr=dl, bufsize=0) as dec, \
            child(encode, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=el, bufsize=0) as enc:
        while not ended:
            if enc.poll() is not None:
                raise RuntimeError('Кодировщик остановился:\n' + tail(enc_log))
            free_check(work, pair, reserve)
            free_check(partial.parent, 0, reserve)
            # Shrink next batch as the growing result consumes disk space.
            batch, _ = cache_plan(media['width'], media['height'], scale, args.cache_mb,
                                  shutil.disk_usage(work).free, reserve, nmax)
            progress.stage('Извлечение', f'порция от кадра {frames+1}')
            got = []
            for i in range(batch):
                path = ins / f'f_{i:08d}.png'
                if not read_png_to(dec.stdout, path, pair):
                    ended = True
                    break
                got.append(path)
            if not got:
                break
            progress.stage('AI', f'порция {frames+1}–{frames+len(got)}')
            command = [exe, '-i', str(ins), '-o', str(outs), '-m', models, '-n', model,
                       '-s', str(scale), '-t', str(args.tile), '-j', args.jobs, '-f', 'png']
            if args.gpu is not None:
                command += ['-g', str(args.gpu)]
            with ai_log.open('wb') as al, child(command, stdout=al, stderr=subprocess.STDOUT) as ai:
                while True:
                    try:
                        rc = ai.wait(timeout=.3)
                        break
                    except subprocess.TimeoutExpired:
                        # An existing output PNG may still be in the middle of a write.
                        saved = max(0, min(len(list(outs.glob('*.png')))-1, len(got)))
                        progress.notice(f'порция {frames+1}–{frames+len(got)} · AI {saved}/{len(got)}')
            if rc:
                raise RuntimeError(f'Real-ESRGAN: код {rc}\n' + tail(ai_log))
            expected = {f.name for f in got}
            actual = {f.name for f in outs.glob('*.png')}
            if expected != actual:
                raise RuntimeError(f'Неполная AI-порция: ожидалось {len(expected)}, получено {len(actual)}')
            used = sum(f.stat().st_size for f in ins.iterdir()) + sum(f.stat().st_size for f in outs.iterdir())
            if used > args.cache_mb * MIB:
                raise RuntimeError('AI-кэш превысил бюджет; обработка остановлена')
            progress.stage('Кодирование', f'порция {frames+1}–{frames+len(got)}')
            for original in got:
                improved = outs / original.name
                # Unbuffered pipe writes can be partial: loop until ALL bytes sent.
                try:
                    with improved.open('rb') as f:
                        while True:
                            data = f.read(MIB)
                            if not data:
                                break
                            view = memoryview(data)
                            while view:
                                sent = enc.stdin.write(view)
                                if not sent:
                                    raise BrokenPipeError('Пустая запись в pipe')
                                view = view[sent:]
                except (BrokenPipeError, OSError) as e:
                    raise RuntimeError('Ошибка передачи в кодировщик: ' + str(e) + '\n' + tail(enc_log)) from e
                original.unlink()
                improved.unlink()
                frames += 1
                progress.update(frames, sample=False)
                free_check(partial.parent, 0, reserve)
            # Commit only a complete end-to-end batch: includes extraction,
            # model loading, GPU work, PNG writes, pipe backpressure and cleanup.
            progress.update(frames)
        rc = dec.wait()
        if rc:
            raise RuntimeError('Декодирование:\n' + tail(dec_log))
        if not frames:
            raise RuntimeError('Нет кадров')
        enc.stdin.close()  # EOF flushes the codec and the final MP4 fragment.
        progress.stage('Завершение')
        rc = enc.wait()
        if rc:
            raise RuntimeError('Кодирование:\n' + tail(enc_log))
    return frames


def collect_inputs(values, recursive=False):
    result, seen = [], set()
    for raw in values:
        p = Path(raw.strip().strip('"').strip("'")).expanduser().resolve()
        if p.is_dir():
            found = sorted(p.rglob('*') if recursive else p.iterdir())
            candidates = [f for f in found if f.is_file() and f.suffix.lower() in EXTS]
        elif p.is_file():
            candidates = [p]
        else:
            raise ValueError('Не найдено: ' + str(p))
        for item in candidates:
            key = os.path.normcase(str(item.resolve()))
            if key not in seen:
                result.append(item)
                seen.add(key)
    return result


def select_indices(text, count):
    if text.strip().lower() in ('a', 'all', 'все', '*'):
        return list(range(count))
    picked = set()
    for token in re.split(r'[,;\s]+', text.strip()):
        match = re.fullmatch(r'(\d+)(?:-(\d+))?', token)
        if not match:
            raise ValueError('Введите номера, например 1,3,5-8, или «все»')
        first = int(match[1])
        last = int(match[2] or first)
        if not 1 <= first <= last <= count:
            raise ValueError('Номера за пределами списка')
        picked.update(range(first - 1, last))
    return sorted(picked)


def unique_output(folder, src, w, h, engine):
    stem = f'{src.stem}_{w}x{h}_{engine.upper()}'
    out = folder / (stem + '.mp4')
    n = 2
    while out.exists():
        out = folder / f'{stem}_{n}.mp4'
        n += 1
    return out


def process(src, args, ff, ffprobe, out):
    media = inspect(src, ffprobe)
    if media['hdr']:
        raise RuntimeError('HDR PQ/HLG: сначала нужен корректный HDR→SDR tone mapping.')
    w, h = dimensions(args.target, media)
    if args.engine == 'ai' and args.fps:
        raise ValueError('--fps поддерживается только в fast')
    out = out.resolve()
    if out == src.resolve() or out.exists():
        raise ValueError('Выходной файл уже существует или совпадает с исходником: ' + str(out))
    out.parent.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache_dir).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    free_check(out.parent, 0, int(args.reserve_gb * GIB))
    # Exclusive create reserves OUR filename; no rename while a player holds it.
    with out.open('xb'):
        pass
    work = None
    watcher = None
    success = False
    start = time.monotonic()
    try:
        work = Path(tempfile.mkdtemp(prefix='upscaler_', dir=cache))
        info(f'{media["width"]}×{media["height"]} → {w}×{h}; {media["rate"]} fps')
        info('Результат: ' + str(out))
        if args.live:
            info('Живой MP4: можно открыть после записи первого фрагмента.')
            watcher = LiveWatch(out, args.open_live)
            watcher.start()
        with ConsoleProgress(frame_goal(media, args), args.engine) as progress:
            if args.engine == 'ai':
                frames = run_ai(src, media, out, args, ff, work, progress)
            else:
                run_fast(src, media, out, args, ff, work, progress)
                frames = None
            progress.stage('Проверка MP4')
            final = inspect(out, ffprobe)
            if (final['stream']['width'], final['stream']['height']) != (w, h):
                raise RuntimeError('Проверка результата: неверное разрешение')
            if out.stat().st_size < 1000:
                raise RuntimeError('Результат слишком мал')
            actual = number(final['stream'].get('nb_frames'))
            if frames and actual and actual != frames:
                raise RuntimeError(f'Потеря кадров: отправлено {frames}, в результате {actual}')
        success = True
        ok(f'ГОТОВО: {out.stat().st_size/GIB:.2f} ГиБ; {(time.monotonic()-start)/60:.1f} мин')
    except BaseException:
        if work:
            diagnostics = '\n'.join(f'--- {p.name} ---\n{tail(p)}' for p in work.glob('*.log'))
            if diagnostics.strip():
                with contextlib.suppress(OSError):
                    out.with_suffix('.error.log').write_text(diagnostics, encoding='utf-8')
        raise
    finally:
        if watcher:
            watcher.close()
        if not success:
            kept = False
            if args.live:
                with contextlib.suppress(OSError):
                    boundary = playable_end(out)
                    if boundary:
                        with out.open('r+b') as f:
                            f.truncate(boundary)
                        kept = True
                        out.with_suffix('.incomplete.txt').write_text(
                            'Обработка НЕ ЗАВЕРШЕНА. MP4 содержит только записанные фрагменты.\n'
                            'Кэш удалён; автоматического возобновления нет.\n', encoding='utf-8')
                        warn('Обработка не завершена. Доступная для просмотра часть оставлена: ' + str(out))
            if not kept:
                with contextlib.suppress(OSError):
                    out.unlink()
        if work:
            shutil.rmtree(work, ignore_errors=True)


def parser():
    p = argparse.ArgumentParser(description='Видео-апскейлер: исходное меню, ограниченный кэш, живой MP4')
    p.add_argument('--version', action='version', version='VideoUpscaler '+__version__)
    p.add_argument('input', nargs='*', help='несколько файлов и/или папок')
    p.add_argument('--engine', '-e', choices=['fast', 'ai'], default='fast')
    p.add_argument('--target', '-t', default='4k', help='720/1080/1440/4k/8k/x2/x3/x4/2560x1440')
    p.add_argument('--model', '-m', choices=list(MODELS), default='general')
    p.add_argument('--preset', '-p', choices=list(PRESETS), default='balanced')
    p.add_argument('--cache-mb', type=float, default=512, help='бюджет PNG-кэша, МиБ (по умолчанию 512)')
    p.add_argument('--cache-dir', default=str(BASE / 'temp'), help='можно указать другой диск')
    p.add_argument('--chunk', type=int, default=0, help='дополнительный лимит кадров порции; 0=авто')
    p.add_argument('--reserve-gb', type=float, default=0.5, help='резерв места, ГиБ')
    p.add_argument('--tile', type=int, default=0, help='AI tile: 0=авто, 128/256 при нехватке VRAM')
    p.add_argument('--jobs', default='1:2:2', help='потоки NCNN load:proc:save')
    p.add_argument('--gpu', type=int, default=None, help='индекс Vulkan GPU')
    p.add_argument('--codec', choices=['h264', 'h265', 'nvenc'], default='h264')
    p.add_argument('--crf', type=int, default=18, help='CRF/CQ: больше = меньше файл, ниже качество')
    p.add_argument('--encode-preset', choices=['ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
                    'medium', 'slow'], default='fast')
    p.add_argument('--threads', type=int, default=min(8, os.cpu_count() or 2))
    p.add_argument('--preview', type=float, default=0, help='только первые N секунд; 0=всё')
    p.add_argument('--fps', type=float, default=0, help='интерполяция, только fast, медленно')
    p.add_argument('--out', '-o', help='выходной MP4 (только для одного входа)')
    p.add_argument('--output-dir', default=str(BASE / 'result'))
    p.add_argument('--recursive', action='store_true', help='искать видео в подпапках')
    p.add_argument('--select', help='номера из списка файлов: 1,3,5-8 или все')
    p.add_argument('--batch', action='store_true', help='совместимость: папки и так обрабатываются очередью')
    p.add_argument('--yes', '-y', action='store_true')
    p.add_argument('--no-preview', action='store_true', help='не создавать кадр ДО/ПОСЛЕ')
    p.add_argument('--no-live', dest='live', action='store_false', default=True,
                   help='обычный MP4 вместо просматриваемого во время обработки')
    p.add_argument('--open-live', action='store_true', help='открыть MP4 после первого готового фрагмента')
    p.add_argument('--check-gpu', action='store_true', help='проверить Vulkan/GPU и выйти')
    p.add_argument('--install-ffmpeg', action='store_true')
    p.add_argument('--install-ai', action='store_true')
    return p


def validate(args):
    for key in ('cache_mb', 'reserve_gb', 'preview', 'fps'):
        value = getattr(args, key)
        if not math.isfinite(value) or value < 0:
            raise ValueError(key + ' должен быть конечным неотрицательным числом')
    if args.cache_mb <= 0 or args.threads < 1 or not 0 <= args.crf <= 51:
        raise ValueError('cache-mb > 0, threads >= 1, crf от 0 до 51')
    if args.chunk < 0 or args.tile < 0 or (args.tile and args.tile < 32):
        raise ValueError('chunk >= 0, tile = 0 или >= 32')
    if not re.fullmatch(r'[1-9]\d*:[1-9]\d*:[1-9]\d*', args.jobs):
        raise ValueError('--jobs: три положительных числа, например 1:2:2')


# ----------------------------------------------------------------------------- #
# Исходный консольный интерфейс: оформление и пункты 1–6 сохранены.
# Новые пункты добавлены, а не заменяют привычное меню.
# ----------------------------------------------------------------------------- #
C = {
    'reset': '\033[0m', 'bold': '\033[1m', 'dim': '\033[2m',
    'red': '\033[91m', 'green': '\033[92m', 'yellow': '\033[93m',
    'blue': '\033[94m', 'magenta': '\033[95m', 'cyan': '\033[96m', 'white': '\033[97m',
}
if os.environ.get('NO_COLOR') or not sys.stdout.isatty():
    C = {key: '' for key in C}


def cl(text, *styles):
    return ''.join(C.get(s, '') for s in styles) + str(text) + C['reset']


def hr(ch='─', n=74, color='dim'):
    say(cl(ch*n, color))


def title(text):
    hr('═')
    say(cl(text, 'bold', 'cyan'))
    hr('═')


def ok(text):
    say('  ' + cl('✓', 'green') + ' ' + str(text))


def warn(text):
    if _ACTIVE_PROGRESS is not None and not _ACTIVE_PROGRESS.closed:
        _ACTIVE_PROGRESS.warning(text)
    else:
        say('  ' + cl('!', 'yellow') + ' ' + cl(text, 'yellow'))


def err(text):
    say('  ' + cl('✗', 'red') + ' ' + cl(text, 'red'))


def info(text):
    say('  ' + cl('·', 'blue') + ' ' + str(text))


def fmt_time(seconds):
    seconds = max(0, int(seconds or 0))
    h, m, s = seconds//3600, seconds//60 % 60, seconds % 60
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def human(n):
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} ТБ'


def ask(prompt, default=''):
    suffix = ' ' + cl('['+str(default)+']', 'dim') if default != '' else ''
    try:
        value = input('  '+cl(prompt, 'bold')+suffix+' > ').strip()
    except EOFError:
        return str(default)
    return value or str(default)


def ask_choice(prompt, options, default='1', note=None):
    say('')
    say('  '+cl(prompt, 'bold', 'cyan'))
    for key, label in options:
        mark = cl('►', 'green') if key == default else ' '
        say('   '+mark+' '+cl('['+key+']', 'yellow')+' '+label)
    if note:
        say('     '+cl(note, 'dim'))
    while True:
        value = ask('Выбор', default).lower()
        if value in [key for key, _ in options]:
            return value
        err('Введи одну из: '+', '.join(key for key, _ in options))


def ask_yes(prompt, default=False):
    while True:
        value = ask(prompt+(' (Y/n)' if default else ' (y/N)')).lower()
        if not value:
            return default
        if value in ('y', 'yes', 'д', 'да', '1', '+'):
            return True
        if value in ('n', 'no', 'н', 'нет', '0', '-'):
            return False
        err('Ответь y (да) или n (нет)')


# ----------------------------------------------------------------------------- #
# One animated terminal line. ETA uses measured end-to-end batch/frame throughput.
# No per-frame log spam; redirected stdout gets start/end summaries only.
# ----------------------------------------------------------------------------- #
ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def plain_text(text):
    return ANSI_RE.sub('', str(text)).replace('\r', ' ').replace('\n', ' ')


def text_width(text):
    return sum(0 if unicodedata.combining(ch) else
               2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1 for ch in text)


def fit_text(text, columns):
    result, used = [], 0
    for ch in text:
        width = text_width(ch)
        if used+width > columns:
            break
        result.append(ch)
        used += width
    return ''.join(result)


class ETAEstimator:
    """Smoothed seconds/frame, based on complete batches, including I/O and reloads.

    We intentionally do not infer completed frames from NCNN's percentage lines:
    they restart for each image/tile and would give a misleading global ETA.
    """
    def __init__(self, total, now=None):
        self.total = max(0, int(total or 0))
        self.started = time.monotonic() if now is None else now
        self.last_at = self.started
        self.done = 0
        self.seconds_per_frame = None
        self.samples = 0
        self.last_interval = 0.0

    def update(self, done, now=None):
        now = time.monotonic() if now is None else now
        done = max(self.done, int(done))
        delta = done-self.done
        if not delta:
            return
        elapsed = max(.001, now-self.last_at)
        observed = elapsed/delta
        if self.seconds_per_frame is None or self.samples == 1:
            # The first interval also includes one-off decoder/GPU initialization.
            # Once a second full interval exists, bootstrap from steady-state work
            # rather than predicting that one-off startup cost for every frame.
            self.seconds_per_frame = observed
        else:
            # Time-based smoothing: adapt over ~20 seconds, rather than jumping
            # to the speed of the last frame. Long batches still get useful weight.
            alpha = min(.85, max(.08, 1-math.exp(-elapsed/20)))
            self.seconds_per_frame += alpha*(observed-self.seconds_per_frame)
        self.done = done
        self.last_at = now
        self.last_interval = elapsed
        self.samples += 1

    @property
    def fps(self):
        return 1/self.seconds_per_frame if self.seconds_per_frame else 0.0

    def remaining(self, now=None):
        now = time.monotonic() if now is None else now
        if not self.total or self.done >= self.total:
            return None
        if (self.samples < 2 or self.done < 4 or now-self.started < 3
                or self.seconds_per_frame is None):
            return None
        idle = max(0, now-self.last_at)
        # A long stall invalidates the old forecast: do not display fake 00:00.
        if idle > max(15, self.last_interval*2.5):
            return None
        seconds = (self.total-self.done)*self.seconds_per_frame-idle
        return seconds if seconds >= 1 else None


def frame_goal(media, args):
    duration = media['duration']
    if args.preview:
        duration = min(duration, args.preview) if duration else args.preview
    rate = args.fps if args.engine == 'fast' and args.fps > media['fps']+.5 else media['fps']
    known = number(media.get('stream', {}).get('nb_frames'))
    if known > 0 and not args.preview and abs(rate-media['fps']) < .001:
        # For CFR sources trust the stream count; VFR -> CFR needs a duration estimate.
        nominal = number(media.get('stream', {}).get('r_frame_rate'))
        if nominal and abs(nominal-media['fps']) < .01:
            return int(known)
    return max(1, math.ceil(duration*rate-1e-6)) if duration > 0 else 0


class ConsoleProgress:
    SPINNER = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')

    def __init__(self, total, label='Обработка', stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.tty = bool(getattr(self.stream, 'isatty', lambda: False)())
        self.label = label
        self.stage_name = 'Подготовка'
        self.total = max(0, int(total or 0))
        self.done = 0
        self.detail = ''
        self.live = False
        self.estimator = ETAEstimator(self.total)
        self.lock = threading.RLock()
        self.event = threading.Event()
        self.thread = None
        self.closed = False
        self.success = False
        self.warnings = []

    def __enter__(self):
        global _ACTIVE_PROGRESS
        with _OUTPUT_LOCK:
            if _ACTIVE_PROGRESS is not None:
                raise RuntimeError('Внутренняя ошибка: две строки прогресса одновременно')
            _ACTIVE_PROGRESS = self
        self.render()
        if self.tty:
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()
        return self

    def stage(self, name, detail=''):
        with self.lock:
            self.stage_name = plain_text(name)
            self.detail = plain_text(detail)

    def notice(self, text):
        text = plain_text(text).strip()
        if text:
            with self.lock:
                self.detail = text

    def warning(self, text):
        text = plain_text(text)
        with self.lock:
            self.detail = text
            if text not in self.warnings:
                self.warnings.append(text)

    def mark_live(self):
        with self.lock:
            self.live = True

    def update(self, frames, sample=True):
        with self.lock:
            self.done = max(self.done, int(frames))
            if sample:
                self.estimator.update(self.done)

    def snapshot(self, width=120, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            elapsed = max(0, now-self.estimator.started)
            spinner = '✓' if self.success else ('■' if self.closed else self.SPINNER[int(elapsed*8)%len(self.SPINNER)])
            percent = min(99.9, self.done/max(1, self.total)*100) if self.total else 0
            if self.success:
                percent = 100
            eta = self.estimator.remaining(now)
            remaining = '~'+fmt_time(math.ceil(eta)) if eta is not None else 'считаю…'
            if not self.total:
                remaining = 'неизвестно'
            if self.stage_name in ('Завершение', 'Проверка MP4'):
                remaining = 'завершаю…'
            if self.closed:
                remaining = 'готово' if self.success else 'остановлено'
            speed = ((f'{self.estimator.fps:.2f} fps' if self.estimator.fps < 1 else
                      f'{self.estimator.fps:.1f} fps') if self.estimator.fps else '— fps')
            bar_width = 16 if width >= 115 else (10 if width >= 85 else 5)
            filled = min(bar_width, int(bar_width*percent/100))
            bar = '█'*filled+'░'*(bar_width-filled)
            stage = 'Готово' if self.success else self.stage_name
            lead = spinner+(' MP4' if self.live else '')
            core = f' {lead} {stage} [{bar}] {percent:5.1f}% | {speed} | осталось {remaining}'
            if width < 78 or text_width(core) > width-1:
                core = f' {lead} {percent:5.1f}% | {speed} | осталось {remaining}'
            if width < 50:
                core = f' {lead} {percent:4.1f}% | ETA {remaining}'
            extras = []
            if self.total:
                extras.append(f'{self.done}/{self.total} кадр.')
            extras.append('прошло '+fmt_time(elapsed))
            if self.live:
                extras.append('MP4 можно смотреть')
            if self.detail and not self.closed:
                extras.append(self.detail)
            for extra in extras:
                candidate = core+' | '+extra
                if text_width(candidate) <= width-1:
                    core = candidate
                elif extra == self.detail:
                    room = width-1-text_width(core)-3
                    if room >= 8:
                        core += ' | '+fit_text(extra, room)
            return fit_text(core, max(1, width-1))

    def render(self, final=False):
        width = shutil.get_terminal_size((120, 25)).columns if self.tty else 160
        line = self.snapshot(width)
        with _OUTPUT_LOCK:
            if self.tty:
                # Clear and redraw one physical line; never wrap at terminal edge.
                self.stream.write('\r\033[2K'+cl(line, 'green' if self.success else 'cyan'))
                if final:
                    self.stream.write('\n')
            else:
                # A logfile is not a terminal: do not write a line every 125 ms.
                self.stream.write(line+'\n')
            self.stream.flush()

    def loop(self):
        while not self.event.wait(.125):
            self.render()

    def __exit__(self, kind, value, tb):
        global _ACTIVE_PROGRESS
        self.event.set()
        if self.thread:
            self.thread.join(timeout=2)
        with self.lock:
            self.closed = True
            self.success = kind is None
        # Logging errors must not mask a processing exception.
        try:
            self.render(final=True)
        finally:
            with _OUTPUT_LOCK:
                _ACTIVE_PROGRESS = None
        for warning in self.warnings:
            say('  '+cl('! '+warning, 'yellow'))
        return False



BANNER = r'''
 ██╗   ██╗██╗██████╗ ███████╗ █████╗  ██████╗
 ██║   ██║██║██╔══██╗██╔════╝██╔══██╗██╔════╝
 ██║   ██║██║██║  ██║█████╗  ███████║██║
 ╚██╗ ██╔╝██║██║  ██║██╔══╝  ██╔══██║██║
  ╚████╔╝ ██║██████╔╝███████╗██║  ██║╚██████╗
   ╚═══╝  ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝
'''
PRESET_NAMES = {
    'soft': 'Мягкий (естественно, без «перешарпа»)',
    'balanced': 'Сбалансированный (рекомендуется)',
    'max': 'Максимальная резкость и чистота',
    'restore': 'Реставрация старого/шумного видео (VHS, DVD, камера)',
}
MODEL_NAMES = {
    'general': 'Универсальная — фильм/сериал/видео с камеры',
    'anime': 'Аниме / мультфильмы (чистые линии)',
    'fast': 'Быстрая — animevideov3 (прежде всего для аниме)',
}


def show_status(args):
    say(cl(BANNER, 'cyan'))
    say(cl('   А П С Е Й Л Е Р   В И Д Е О   ·   бесплатно и без ограничений', 'bold'))
    say(cl('   улучшение качества и повышение разрешения до 4K / 8K · v'+__version__, 'dim'))
    say('')
    try:
        ff, _ = tools()
        ok('FFmpeg: '+cl(ff, 'dim'))
    except RuntimeError:
        err('FFmpeg / FFprobe: НЕ НАЙДЕНЫ (установка — пункт 3)')
    try:
        exe, _ = ai_tools()
        ok('AI Real-ESRGAN: '+cl(exe, 'dim'))
    except RuntimeError:
        warn('AI Real-ESRGAN: не установлен (установка — пункт 4)')
    info(f'Кэш: до {args.cache_mb:g} МиБ; '+cl(args.cache_dir, 'dim'))
    info('Просмотр во время обработки: '+cl('ВКЛЮЧЁН' if args.live else 'выключен', 'green'))
    say('')


def show_media(path, media):
    say('')
    hr()
    say(f'  {cl("Файл:", "bold")}        {path.name}')
    say(f'  {cl("Папка:", "bold")}       {cl(path.parent, "dim")}')
    say(f'  {cl("Разрешение:", "bold")}  {cl(str(media["width"])+"×"+str(media["height"]), "magenta")}')
    say(f'  {cl("Длительность:", "bold")} {fmt_time(media["duration"])}   '
        f'{cl("FPS:", "bold")} {media["fps"]:.3f}   {cl("Размер:", "bold")} {human(path.stat().st_size)}')
    say(f'  {cl("Кодек:", "bold")}       {media["stream"].get("codec_name", "?")} / '
        f'{media["stream"].get("pix_fmt", "?")}   {cl("Звук:", "bold")} '+('есть' if media['audio'] else 'нет'))
    hr()


def pick_file():
    say('')
    say(cl('  Перетащи видеофайл (или папку с видео) прямо в это окно и нажми Enter.', 'dim'))
    say(cl('  Либо введи полный путь. Пустая строка — выход.', 'dim'))
    while True:
        raw = ask('Файл / папка')
        if not raw:
            return []
        try:
            files = collect_inputs([raw])
            if not files:
                err('Видео не найдено.')
                continue
            if len(files) == 1:
                return files
            say('')
            say('  '+cl('В папке найдено видео:', 'bold'))
            for i, path in enumerate(files, 1):
                say(f'    {cl("["+str(i)+"]", "yellow")} {path.name}')
            info('Можно выбрать несколько: 1,3,5-8. «a» — обработать ВСЕ файлы.')
            while True:
                try:
                    return [files[i] for i in select_indices(ask('Выбор', '1'), len(files))]
                except ValueError as exc:
                    err(str(exc))
        except (ValueError, OSError) as exc:
            err(str(exc))


def pick_multiple():
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        names = filedialog.askopenfilenames(title='Выберите видео: Ctrl/Shift + щелчок',
                    filetypes=[('Видео', ' '.join('*'+e for e in sorted(EXTS))), ('Все файлы', '*')])
        return collect_inputs(names)
    except Exception as exc:
        warn('Окно выбора недоступно: '+str(exc))
        info('Вводи пути по одному. Пустая строка завершит список.')
        values = []
        while True:
            value = ask('Файл / папка')
            if not value:
                break
            values.append(value)
        return collect_inputs(values)
    finally:
        if root:
            root.destroy()


def gpu_is_available():
    try:
        ff, _ = tools()
        exe, models = ai_tools()
        with tempfile.TemporaryDirectory(prefix='gpu_check_') as folder:
            inp, out = Path(folder)/'test.png', Path(folder)/'out.png'
            subprocess.run([ff, '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=32x32',
                            '-frames:v', '1', str(inp)], check=True, capture_output=True, timeout=30)
            result = subprocess.run([exe, '-i', str(inp), '-o', str(out), '-n', 'realesrgan-x4plus',
                                     '-m', models, '-s', '4'], capture_output=True, timeout=120,
                                    encoding='utf-8', errors='replace')
            text = result.stdout+'\n'+result.stderr
            match = re.search(r'\[\d+\s+([^\]]+)\]', text)
            dev = match[1].strip() if match else 'имя устройства не сообщено'
            if result.returncode or not out.exists():
                return False, 'AI-тест не прошёл: '+text[-1500:]
            soft = any(k in text.lower() for k in ('llvmpipe', 'softpipe', 'swrast', 'software rasterizer'))
            return not soft, dev+(' (программный Vulkan)' if soft else '')
    except Exception as exc:
        return False, str(exc)


def ask_settings_ui(defaults, files, probe):
    args = copy.deepcopy(defaults)
    first = inspect(files[0], probe)
    if len(files) == 1:
        show_media(files[0], first)
    else:
        title(f'Пакетная обработка · {len(files)} файл(ов)')
        info('Настройки применяются ко всей очереди. x2/x3 считаются отдельно для каждого видео.')
    choice = ask_choice('Какой движок используем?', [
        ('1', 'БЫСТРЫЙ — ffmpeg (апскейл + резкость + шумодав). '+cl('без AI', 'green')),
        ('2', 'AI — Real-ESRGAN (нейросеть дорисовывает детали). '+cl('нужен Vulkan', 'magenta'))], '1')
    args.engine = 'fast' if choice == '1' else 'ai'
    options = [(key, f'{w}×{h}  '+name) for key, (w, h), name in [
        ('720', TARGETS['720'], 'HD 720p'), ('1080', TARGETS['1080'], 'Full HD 1080p'),
        ('1440', TARGETS['1440'], '2K QHD 1440p'), ('4k', TARGETS['4k'], '4K UHD 2160p'),
        ('8k', TARGETS['8k'], '8K UHD 4320p')]]
    options += [('x2', 'Увеличить в 2 раза'), ('x3', 'Увеличить в 3 раза'), ('custom', 'Своё разрешение')]
    args.target = ask_choice('Куда поднимаем?', options, '4k')
    if args.target == 'custom':
        while True:
            value = ask('Ширина x высота (например 2560x1440)', '2560x1440')
            try:
                dimensions(value, first)
                args.target = value
                break
            except ValueError as exc:
                err(str(exc))
    if args.engine == 'fast':
        args.preset = ask_choice('Пресет обработки?', list(PRESET_NAMES.items()), 'balanced')
        args.fps = float(ask_choice('Повысить плавность (fps)?', [
            ('0', f'Оставить исходную частоту (сейчас {first["fps"]:.3f} fps)'),
            ('30', 'Интерполяция до 30 fps'), ('60', 'Интерполяция до 60 fps — МЕДЛЕННО')], '0'))
    else:
        try:
            ai_tools()
        except RuntimeError:
            warn('AI-движок ещё не установлен.')
            if WIN and ask_yes('Скачать Real-ESRGAN + модели?', True):
                install('ai')
            else:
                raise RuntimeError('Установи Real-ESRGAN через пункт 4 или выбери БЫСТРЫЙ движок.')
        args.model = ask_choice('Какая модель нейросети?', list(MODEL_NAMES.items()), 'general')
        if ask_yes('Сделать сначала тестовый фрагмент 5 секунд?', True):
            args.preview = 5
    if args.live:
        args.open_live = ask_yes('Открыть MP4 в плеере, как только будет готов первый фрагмент?', True)
    args.yes = True
    return args


def advanced_ui(args):
    while True:
        choice = ask_choice('КЭШ И ПРОСМОТР — дополнительные настройки', [
            ('1', f'Бюджет PNG-кэша: {args.cache_mb:g} МиБ'),
            ('2', 'Папка кэша: '+str(args.cache_dir)),
            ('3', 'Папка результатов: '+str(args.output_dir)),
            ('4', 'Живой MP4: '+('ВКЛЮЧЁН' if args.live else 'выключен')),
            ('5', 'Кодирование: '+args.codec),
            ('0', 'Назад')], '0')
        try:
            if choice == '0':
                return
            if choice == '1':
                candidate = float(ask('МиБ', str(args.cache_mb)))
                check = copy.deepcopy(args)
                check.cache_mb = candidate
                validate(check)
                args.cache_mb = candidate
            elif choice == '2':
                args.cache_dir = ask('Папка кэша', args.cache_dir).strip('"')
            elif choice == '3':
                args.output_dir = ask('Папка результатов', args.output_dir).strip('"')
            elif choice == '4':
                args.live = ask_yes('Делать MP4 просматриваемым во время обработки?', True)
            elif choice == '5':
                args.codec = ask_choice('Кодек', [('h264', 'H.264 — совместимость'),
                    ('h265', 'H.265 — обычно меньше файл, но медленнее'),
                    ('nvenc', 'NVIDIA NVENC — аппаратное H.264')], args.codec)
        except ValueError as exc:
            err(str(exc))


def make_comparison(src, out, ff, probe):
    try:
        si, oi = inspect(src, probe), inspect(out, probe)
        duration = min(si['duration'], oi['duration'])
        when = max(0, min(duration*.35, duration-.1))
        h = min(max(si['height'], 360), 1080)//2*2
        dest = out.with_name(out.stem+'_сравнение.jpg')
        graph = f'[0:v]scale=-2:{h}[l];[1:v]scale=-2:{h}[r];[l][r]hstack=inputs=2[v]'
        cmd = [ff, '-v', 'error', '-y', '-ss', str(when), '-i', str(src),
               '-ss', str(when), '-i', str(out), '-filter_complex_threads', '1',
               '-filter_complex', graph, '-map', '[v]', '-frames:v', '1', '-q:v', '2', str(dest)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return dest
    except Exception:
        return None


def ui_run(files, args, ff, probe):
    failures = 0
    for i, src in enumerate(files, 1):
        say('')
        say(cl(f'  ── [{i}/{len(files)}] {src.name} '+'─'*20, 'bold', 'blue'))
        try:
            media = inspect(src, probe)
            if len(files) > 1:
                show_media(src, media)
            w, h = dimensions(args.target, media)
            out = unique_output(Path(args.output_dir).expanduser(), src, w, h,
                                args.engine+('_preview' if args.preview else ''))
            process(src, args, ff, probe, out)
            title('ТЕСТОВЫЙ ФРАГМЕНТ ГОТОВ!' if args.preview else 'ГОТОВО!')
            ok('Файл: '+cl(out, 'bold', 'green'))
            if not args.no_preview:
                image = make_comparison(src, out, ff, probe)
                if image:
                    ok('Кадр «ДО | ПОСЛЕ» (слева исходник): '+str(image))
        except KeyboardInterrupt:
            warn('Прервано (Ctrl+C). Записанные фрагменты сохранены, кэш очищен.')
            return
        except Exception as exc:
            failures += 1
            err(str(exc))
    if len(files) > 1:
        ok(f'Пакетная обработка завершена. Успешно: {len(files)-failures}; ошибок: {failures}.')
    if args.preview and failures == 0 and ask_yes('Обработать теперь ВСЁ видео с этими настройками?', True):
        full = copy.deepcopy(args)
        full.preview = 0
        ui_run(files, full, ff, probe)
    elif WIN and ask_yes('Открыть папку с результатом?', False):
        os.startfile(str(Path(args.output_dir).resolve()))


def main_menu(defaults):
    show_status(defaults)
    while True:
        choice = ask_choice('ГЛАВНОЕ МЕНЮ', [
            ('1', cl('Улучшить видео', 'bold', 'green')+'  — открыть файл или папку'),
            ('2', 'Пакетная обработка папки (все видео сразу)'),
            ('3', 'Установить / обновить FFmpeg'),
            ('4', 'Установить AI-движок Real-ESRGAN (нейросеть)'),
            ('5', 'Проверить видеокарту (Vulkan) и готовность AI'),
            ('6', 'Открыть папку программы'),
            ('7', cl('Выбрать НЕСКОЛЬКО видео', 'green')+' — окно выбора Ctrl/Shift'),
            ('8', 'Кэш, папки, кодек и просмотр MP4 во время обработки'),
            ('0', cl('Выход', 'red'))], '1')
        try:
            if choice == '0':
                say(cl('  Пока! Хорошего качества картинке 😉', 'bold', 'cyan'))
                return 0
            if choice in ('3', '4'):
                title('Установка FFmpeg' if choice == '3' else 'Установка AI-движка Real-ESRGAN')
                if ask_yes('Скачать / переустановить?', True):
                    install('ffmpeg' if choice == '3' else 'ai')
                continue
            if choice == '5':
                info('Проверяю Vulkan реальным тестовым кадром…')
                has, dev = gpu_is_available()
                (ok if has else warn)('Vulkan: '+dev)
                continue
            if choice == '6':
                if WIN:
                    os.startfile(str(BASE))
                else:
                    info(str(BASE))
                continue
            if choice == '8':
                advanced_ui(defaults)
                continue
            try:
                ff, probe = tools()
            except RuntimeError:
                if WIN and ask_yes('FFmpeg/FFprobe не найдены. Установить?', True):
                    install('ffmpeg')
                    ff, probe = tools()
                else:
                    raise
            if choice == '1':
                files = pick_file()
            elif choice == '2':
                raw = ask('Папка с видео (можно перетащить её в окно)')
                files = collect_inputs([raw]) if raw else []
            else:
                files = pick_multiple()
            if not files:
                continue
            settings = ask_settings_ui(defaults, files, probe)
            validate(settings)
            say('')
            hr()
            info(f'Итог: {len(files)} видео; движок {settings.engine}; цель {settings.target}')
            info(f'Кэш до {settings.cache_mb:g} МиБ; живой MP4: '+('да' if settings.live else 'нет'))
            hr()
            if ask_yes('Запускаем?', True):
                ui_run(files, settings, ff, probe)
        except KeyboardInterrupt:
            warn('Отменено. Возвращаюсь в меню.')
        except Exception as exc:
            err(str(exc))


# ----------------------------------------------------------------------------- #
# Растущий fragmented MP4: определение готового фрагмента и безопасное сохранение.
# ----------------------------------------------------------------------------- #
def playable_end(path):
    """Last complete top-level moof+mdat, without reading media payload into RAM."""
    try:
        size = Path(path).stat().st_size
        boundary, moov, pending, pos = 0, False, False, 0
        with Path(path).open('rb') as f:
            while pos+8 <= size:
                f.seek(pos)
                header = f.read(8)
                if len(header) != 8:
                    break
                length, kind = struct.unpack('>I4s', header)
                header_size = 8
                if length == 1:
                    extended = f.read(8)
                    if len(extended) != 8:
                        break
                    length = struct.unpack('>Q', extended)[0]
                    header_size = 16
                # size=0 extends to EOF: never treat a growing box as complete.
                if length < header_size or pos+length > size:
                    break
                if kind == b'moov':
                    moov = True
                elif kind == b'moof':
                    pending = True
                elif kind == b'mdat' and moov and pending:
                    boundary = pos+length
                    pending = False
                pos += length
        return boundary
    except (OSError, struct.error):
        return 0


def open_player(path):
    if WIN:
        os.startfile(str(path))
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class LiveWatch:
    def __init__(self, path, auto_open=False):
        self.path = Path(path)
        self.auto_open = auto_open
        self.event = threading.Event()
        self.ready = False
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.thread.start()

    def loop(self):
        while not self.event.wait(.25):
            if self.check():
                return

    def check(self):
        if self.ready or not playable_end(self.path):
            return self.ready
        self.ready = True
        progress = _ACTIVE_PROGRESS
        if progress is not None and not progress.closed:
            progress.mark_live()
        else:
            ok('MP4 УЖЕ МОЖНО СМОТРЕТЬ: '+cl(self.path, 'bold', 'green'))
            info('Пока доступна только записанная часть. Рекомендуется VLC или mpv.')
        if self.auto_open:
            try:
                open_player(self.path)
            except Exception as exc:
                warn('Не удалось открыть плеер автоматически: '+str(exc))
        return True

    def close(self):
        self.event.set()
        self.thread.join(timeout=2)
        self.check()


def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)
    if args.install_ffmpeg or args.install_ai:
        if args.install_ffmpeg:
            install('ffmpeg')
        if args.install_ai:
            install('ai')
        return 0
    if args.check_gpu:
        has, dev = gpu_is_available()
        say(('GPU OK: ' if has else 'GPU NOT READY: ') + dev)
        return 0 if has else 2
    if not args.input:
        if not sys.stdin.isatty():
            ap.error('Укажите входные файлы; меню доступно только в терминале')
        return main_menu(args)
    files = collect_inputs(args.input, args.recursive)
    validate(args)
    if not files:
        raise ValueError('Нет видео для обработки')
    if args.select:
        for i, f in enumerate(files, 1):
            say(f'{i}. {f}')
        files = [files[i] for i in select_indices(args.select, len(files))]
    if args.out and len(files) != 1:
        raise ValueError('--out допустим только для одного видео; для очереди используйте --output-dir')
    ff, probe = tools()
    if args.engine == 'ai':
        ai_tools()
    say(f'\nВ очереди: {len(files)} видео. Движок {args.engine}. Настройки общие.')
    if not args.yes and sys.stdin.isatty():
        if not ask_yes('Запускаем?', True):
            return 0
    failures = 0
    for i, src in enumerate(files, 1):
        say(f'\n[{i}/{len(files)}] {src}')
        try:
            media = inspect(src, probe)
            w, h = dimensions(args.target, media)
            out = (Path(args.out).expanduser() if args.out else
                   unique_output(Path(args.output_dir).expanduser(), src, w, h,
                                 args.engine + ('_preview' if args.preview else '')))
            process(src, args, ff, probe, out)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            failures += 1
            say('  ОШИБКА: ' + str(exc))
    say(f'\nОчередь завершена. Успешно: {len(files)-failures}; ошибок: {failures}.')
    return 1 if failures else 0


if __name__ == '__main__':
    if WIN:
        os.system('')  # VT colors in Windows console
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding='utf-8', errors='replace')
    for name in ('SIGTERM', 'SIGBREAK'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), terminate)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        for proc in list(ACTIVE):
            stop(proc)
        say('\nОстановлено. Дочерние процессы завершены; кэш текущего задания очищен.')
        sys.exit(130)
    except Exception as exc:
        say('ОШИБКА: ' + str(exc))
        sys.exit(1)
