#!/usr/bin/env python3
"""音频频段引擎 —— 把系统正在放的声音变成 24 个 0..1 的跳动数值。

顶层设计：
    默认输出设备（sink）的 monitor 端口 → pw-record 抓原始 f32 流 → numpy FFT
    → 24 个近似对数频段 → 自适应增益 + 快攻击慢释放 → 给守护进程读。

安全红线：只抓 **输出设备**（别人的歌声），永远不抓默认输入设备（麦克风）。
解析不出 sink 就直接静音，绝不退化成录音。

用法：
    python3 audiobands.py --probe        # 只打印解析到的输出设备
    python3 audiobands.py --test --fps 60  # 合成频段（不碰真实音频），每行一个 JSON 数组
    python3 audiobands.py --fps 60       # 接真实音频
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time

import numpy as np

N_BANDS = 48
SAMPLE_RATE = 48000
CHANNELS = 2
FFT_SIZE = 1024          # 21ms 窗，够分辨低频又不至于糊
FFT_PAD = 2048           # 零填充：频点细一倍，48 段在低频才分得开（不增加延迟）
HOP = 512                # 11.6ms 一帧 → 约 86fps，守护进程按自己的节奏取最新一帧
F_MIN = 40.0             # 低频起点
F_MAX = 16000.0          # 高频终点
DB_RANGE = 45.0          # 自适应增益的动态范围上限（dB）
MIN_SPAN_DB = 26.0       # 动态范围下限：再安静的歌也要有个参照跨度，别一有信号就满格
GATE_DB = 8.0            # 噪声底之上留一点门限，静音时才是纯 0
FLOOR_RISE = 0.03        # 噪声底每帧最多往上爬多少 dB（约 2.6 dB/秒，慢一点才不会被安静段落带着跑）
PEAK_FALL = 0.05         # 峰值参照每帧最多回落多少 dB（约 4.3 dB/秒）
CONTRAST = 1.35          # 归一化后再压一次小信号：有对比才看得出"在跳什么"
# 采集缓冲：pw-record 默认 100ms，实测端到端延迟 ~92ms；降到 10ms 只有 ~36ms
CAPTURE_LATENCY = '10ms'
ATTACK = 0.55            # 上升快（约 60ms）
RELEASE = 0.12           # 回落慢（约 250ms）


# --------------------------------------------------------------- 设备解析 --

def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.stdout or ''
    except Exception:
        return ''


def _default_sink_name() -> str:
    """WirePlumber 的默认输出设备名。"""
    out = _run(['pw-metadata', '-n', 'default', '0'])
    for line in out.splitlines():
        if 'default.audio.sink' not in line:
            continue
        start = line.find('{')
        if start < 0:
            continue
        try:
            data = json.loads(line[start:line.rfind('}') + 1])
        except Exception:
            continue
        name = data.get('name')
        if name:
            return str(name)
    return ''


def _sink_node_by_name(name: str) -> tuple[int | None, str]:
    """在 pw-dump 里按 node.name 找输出设备，返回 (节点 id, media.class)。"""
    out = _run(['pw-dump'])
    if not out:
        return None, ''
    try:
        nodes = json.loads(out)
    except Exception:
        return None, ''
    for node in nodes:
        if node.get('type') != 'PipeWire:Interface:Node':
            continue
        props = ((node.get('info') or {}).get('props')) or {}
        if props.get('node.name') != name:
            continue
        media_class = props.get('media.class', '')
        return int(node['id']), media_class
    return None, ''


def _sink_name_by_id(node_id: int) -> str:
    out = _run(['pw-dump'])
    if not out:
        return ''
    try:
        nodes = json.loads(out)
    except Exception:
        return ''
    for node in nodes:
        if node.get('type') != 'PipeWire:Interface:Node' or int(node.get('id', -1)) != node_id:
            continue
        props = ((node.get('info') or {}).get('props')) or {}
        if 'Sink' in props.get('media.class', ''):
            return str(props.get('node.name', ''))
    return ''


def _node_id_from_wpctl() -> int | None:
    """兜底：解析 wpctl status 里带星号的输出设备。"""
    out = _run(['wpctl', 'status'])
    in_sinks = False
    for line in out.splitlines():
        if 'Sinks:' in line:
            in_sinks = True
            continue
        if in_sinks and ('Sources:' in line or 'Filters:' in line or 'Streams:' in line):
            break
        if in_sinks and '*' in line:
            for token in line.replace('*', ' ').split():
                if token.endswith('.') and token[:-1].isdigit():
                    return int(token[:-1])
    return None


def resolve_default_sink() -> tuple[str | None, str]:
    """返回 (sink 节点名, 说明)。解析失败返回 (None, 原因)——宁可静音，不许录麦克风。

    注意：一定要 node **name** 而不是 id。`pw-record --target <数字>` 会被当成
    串号，结果默默连到默认输入设备（麦克风）上去 —— 这条踩过。
    """
    name = _default_sink_name()
    if name:
        node_id, media_class = _sink_node_by_name(name)
        if node_id is not None and 'Sink' in media_class:
            return name, f'{name} (#{node_id})'
    node_id = _node_id_from_wpctl()
    if node_id is not None:
        name = _sink_name_by_id(node_id)
        if name:
            return name, f'{name} (#{node_id}, wpctl)'
    return None, '找不到默认输出设备'


# --------------------------------------------------------------- 频段划分 --

def _band_edges(rfft_bins: int) -> np.ndarray:
    """近似对数的频段边界（低频密、高频疏）。"""
    edges = np.zeros(N_BANDS + 1, dtype=int)
    nyquist = SAMPLE_RATE / 2
    for i in range(N_BANDS + 1):
        freq = F_MIN * (F_MAX / F_MIN) ** (i / N_BANDS)
        freq = min(freq, nyquist - 1)
        edges[i] = int(round(freq / nyquist * (rfft_bins - 1)))
    for i in range(1, N_BANDS + 1):
        if edges[i] <= edges[i - 1]:
            edges[i] = edges[i - 1] + 1
    edges = np.clip(edges, 0, rfft_bins - 1)
    return edges


# ------------------------------------------------------------------ 引擎 --

class Analyzer:
    """真实音频：pw-record 抓 sink monitor，后台线程算频段。"""

    def __init__(self, bands: int = N_BANDS, samplerate: int = SAMPLE_RATE, device=None):
        if bands != N_BANDS:
            raise ValueError(f'本实现固定 {N_BANDS} 个频段')
        self.bands_count = bands
        self.samplerate = samplerate
        # device 可以是 sink 节点名（推荐），也可以是节点 id（会翻译成名字）
        self.device = device
        self._window = np.hanning(FFT_SIZE).astype(np.float32)
        self._edges = _band_edges(FFT_PAD // 2 + 1)
        self._floor = np.full(N_BANDS, np.nan, dtype=np.float32)  # 每个频段的噪声底（首帧确定）
        self._peak = np.full(N_BANDS, np.nan, dtype=np.float32)   # 每个频段的峰值参照
        self._global_floor = float('nan')                         # 整体响度的噪声底
        self._global_peak = float('nan')
        self.level = 0.0                                          # 0..1 整体响度（配色/亮度用）
        self._value = np.zeros(N_BANDS, dtype=np.float32)
        self._raw_pos = 0.0

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._frame_id = 0
        self._last_frame_id = 0
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._ring = np.zeros(FFT_SIZE * 4, dtype=np.float32)
        self._pending = 0
        self._bands = np.zeros(N_BANDS, dtype=np.float32)
        self._alive = False
        self._mic_guard_tripped = False
        self._stop = False
        self._current_target = None      # 正在抓的输出设备名
        self._next_target_check = 0.0    # 下次复查"默认输出有没有换"
        self.last_error = ''

    # --------------------------------------------------------------- 生命周期 --
    @property
    def running(self) -> bool:
        return self._alive

    def start(self) -> None:
        if self._alive:
            return
        self._stop = False
        self._alive = True
        self._worker = threading.Thread(target=self._worker_loop, name='viz-bands-worker', daemon=True)
        self._reader = threading.Thread(target=self._reader_loop, name='viz-bands-reader', daemon=True)
        self._worker.start()
        self._reader.start()

    def stop(self) -> None:
        self._stop = True
        self._alive = False
        self._kill_capture()
        for thread in (self._reader, self._worker):
            if thread and thread.is_alive():
                thread.join(timeout=2.0)
        self._reader = self._worker = None

    def _kill_capture(self) -> None:
        proc, self._proc = self._proc, None
        if not proc:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                stream and stream.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ 采集 --
    def _spawn_capture(self) -> subprocess.Popen | None:
        target = self.device
        if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
            target = _sink_name_by_id(int(target))
        if not target:
            target, why = resolve_default_sink()
            if not target:
                self.last_error = why
                print(f'[audiobands] {why}：不抓麦克风，保持静音', file=sys.stderr, flush=True)
                return None
        # stream.capture.sink=true 是必须的：没有它，pw-record 会无视 --target
        # 去连默认输入设备（麦克风）。这是本模块最重要的一个开关。
        cmd = ['pw-record', '-P', 'stream.capture.sink=true', '--target', str(target),
               '--latency', CAPTURE_LATENCY,
               '--format', 'f32', '--rate', str(self.samplerate),
               '--channels', str(CHANNELS), '-']
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except Exception as exc:
            self.last_error = f'启动 pw-record 失败：{exc}'
            print(f'[audiobands] {self.last_error}', file=sys.stderr, flush=True)
            return None
        self.last_error = ''
        self._current_target = str(target)
        self._verify_capture(proc, str(target))
        return proc

    # 安全阀：确认这条抓音流真的挂在输出的 monitor 上，一旦发现连到了输入设备
    # （麦克风）立刻掐掉——宁可没有可视化，也不能偷偷录房间。
    def _verify_capture(self, proc: subprocess.Popen, target: str) -> None:
        def check():
            time.sleep(0.6)
            if proc.poll() is not None:
                return
            out = _run(['pw-link', '-l'], timeout=3.0)
            if not out:
                return
            sources = []
            for block in out.split('\n\n'):
                if 'pw-record:input' not in block:
                    continue
                for line in block.splitlines():
                    if '|<--' in line:
                        sources.append(line.split('|<--')[1].strip().split(':')[0])
            if not sources:
                return
            bad = [s for s in sources if 'monitor' not in s]
            if bad:
                self.last_error = f'抓音流连到了输入设备 {bad}，已强制掐断'
                print(f'[audiobands] {self.last_error}', file=sys.stderr, flush=True)
                self._mic_guard_tripped = True
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            elif not any('monitor' in s for s in sources):
                self.last_error = '抓音流没有连到 sink 的 monitor'

        threading.Thread(target=check, name='viz-bands-guard', daemon=True).start()

    def _reader_loop(self) -> None:
        backoff = 1.0
        while not self._stop:
            # 插拔耳机 / 连蓝牙会换默认输出，这时旧目标的 monitor 就没声了，
            # 定期复查一下，换了就重连（每 2 秒一次 pw-metadata，开销可忽略）。
            if self.device is None and time.monotonic() >= self._next_target_check:
                self._next_target_check = time.monotonic() + 2.0
                name, _why = resolve_default_sink()
                if name and self._current_target and name != self._current_target:
                    print(f'[audiobands] 默认输出从 {self._current_target} 换成 {name}，重连采集',
                          file=sys.stderr, flush=True)
                    self._kill_capture()
                    self._current_target = None
                    continue
            if self._proc is None:
                self._proc = self._spawn_capture()
                if self._proc is None:
                    wait = 30.0 if self._mic_guard_tripped else backoff
                    time.sleep(wait)
                    backoff = min(10.0, backoff * 1.6)
                    continue
                backoff = 1.0
            proc = self._proc
            chunk = np.empty(HOP * CHANNELS, dtype=np.float32)
            need = chunk.nbytes
            buf = bytearray()
            try:
                while not self._stop and len(buf) < need:
                    data = proc.stdout.read(need - len(buf))
                    if not data:
                        break
                    buf.extend(data)
            except Exception:
                buf = bytearray()
            if not buf:
                # 子进程退出了：回收后重来
                self._kill_capture()
                if not self._stop:
                    time.sleep(backoff)
                    backoff = min(10.0, backoff * 1.6)
                continue
            samples = np.frombuffer(bytes(buf), dtype=np.float32)
            if samples.size < 2:
                continue
            stereo = samples.reshape(-1, CHANNELS) if samples.size % CHANNELS == 0 \
                else samples.reshape(-1, 1)
            mono = stereo.mean(axis=1)
            # pw-record 的头一个缓冲里可能带垃圾（甚至 NaN），一律清干净再送进 FFT
            mono = np.nan_to_num(mono, nan=0.0, posinf=0.0, neginf=0.0)
            self._enqueue(mono)

    # 环形缓冲：worker 线程按 HOP 步进做 FFT
    def _enqueue(self, mono: np.ndarray) -> None:
        with self._lock:
            n = mono.size
            if n >= self._ring.size:
                self._ring[:] = mono[-self._ring.size:]
            else:
                self._ring[:-n] = self._ring[n:]
                self._ring[-n:] = mono
            self._pending = min(self._pending + n, self._ring.size)
            self._cond.notify_all()

    def _worker_loop(self) -> None:
        pos = 0
        while not self._stop:
            with self._lock:
                if self._pending < HOP:
                    self._cond.wait(0.05)
                    continue
                buf = self._ring.copy()
                self._pending -= HOP
            frame = buf[-FFT_SIZE:]
            self._analyze(frame)

    def _analyze(self, frame: np.ndarray) -> None:
        frame = np.nan_to_num(frame, nan=0.0, posinf=0.0, neginf=0.0)
        frame = frame - float(np.mean(frame))          # 去直流
        spectrum = np.abs(np.fft.rfft(frame * self._window, n=FFT_PAD))
        power = spectrum * spectrum
        bands_db = np.empty(N_BANDS, dtype=np.float32)
        for i in range(N_BANDS):
            lo, hi = self._edges[i], self._edges[i + 1]
            hi = max(hi, lo + 1)
            bands_db[i] = 10.0 * math.log10(float(power[lo:hi].mean()) + 1e-12)

        # 自适应增益：噪声底只许慢慢往上爬、遇到更安静立刻掉下来；峰值参照反过来，
        # 立刻跟上、慢慢回落。两者之间就是"这首歌现在的动态范围"，于是安静的歌也
        # 能跳出满格，而静音时稳稳贴在 0。
        if np.isnan(self._floor).any():
            self._floor = bands_db - GATE_DB
            self._peak = bands_db - GATE_DB
        self._floor = np.minimum(bands_db, self._floor + FLOOR_RISE)
        self._peak = np.maximum(bands_db, self._peak - PEAK_FALL)
        # 数字静音（-120dB）别当基准，否则一出声整排全满格：基准最多比峰值低 DB_RANGE
        floor = np.maximum(self._floor, self._peak - DB_RANGE)
        span = np.clip(self._peak - floor, MIN_SPAN_DB, DB_RANGE)
        # 门限只影响"底部什么时候归零"，不压低顶部：峰值那一档要能顶到 1.0
        denom = np.maximum(span - GATE_DB, 8.0)
        normalized = np.clip((bands_db - floor - GATE_DB) / denom, 0.0, 1.0).astype(np.float32)
        # 再压一次小信号：不然压缩过的流行歌整排都发亮，"在跳什么"反而看不见
        normalized = np.power(normalized, CONTRAST, dtype=np.float32)

        # 快攻击 / 慢释放：跳得干脆、落得柔和
        rising = normalized > self._value
        self._value = np.where(rising,
                               self._value + (normalized - self._value) * ATTACK,
                               self._value + (normalized - self._value) * RELEASE)

        # 整体响度单独算（用它驱动配色/亮度）：按总能量的 dB 走同款慢参照，
        # 这样安静段落真的会暗下去，副歌才亮起来。
        rms_db = 10.0 * math.log10(float(power[1:].mean()) + 1e-12)
        if np.isnan(self._global_floor):
            self._global_floor = rms_db - GATE_DB
            self._global_peak = rms_db - GATE_DB
        self._global_floor = min(rms_db, self._global_floor + FLOOR_RISE)
        self._global_peak = max(rms_db, self._global_peak - PEAK_FALL)
        floor_g = max(self._global_floor, self._global_peak - DB_RANGE)
        # 响度的门限给得比频段松（3dB）：安静的歌也该亮起来，但不能失真成一条直线
        denom_g = max(min(self._global_peak - floor_g, DB_RANGE) - 3.0, 10.0)
        level = min(1.0, max(0.0, (rms_db - floor_g - 3.0) / denom_g))

        with self._lock:
            self._frame_id += 1
            self._bands = self._value.copy()
            self.level = float(level ** 0.9)
            self._cond.notify_all()

    # ------------------------------------------------------------------ 读取 --
    def read(self, timeout: float = 0.0):
        with self._lock:
            if timeout > 0:
                deadline = time.monotonic() + timeout
                while self._frame_id == self._last_frame_id:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        break
                    self._cond.wait(remain)
            if self._frame_id == 0:
                return None
            self._last_frame_id = self._frame_id
            return [float(v) if v == v else 0.0 for v in self._bands]


class SyntheticAnalyzer(Analyzer):
    """合成音频：按"鼓点 + 旋律 + 镲"造出像歌一样的频段，用来验证画面而不碰真实声音。"""

    def __init__(self, bands: int = N_BANDS, samplerate: int = SAMPLE_RATE, device=None):
        super().__init__(bands=bands, samplerate=samplerate, device=device)
        self._t = 0.0

    def start(self) -> None:      # 不启动任何子进程
        self._alive = True
        self._stop = False
        self._frame_id = 0
        self._bands = np.zeros(N_BANDS, dtype=np.float32)

    def stop(self) -> None:
        self._alive = False
        self._stop = True

    def read(self, timeout: float = 0.0):
        time.sleep(max(0.0, min(timeout, 0.02)))
        self._t += 1.0 / 60.0
        beat = (self._t * 2.0) % 1.0                     # 120 BPM
        kick = max(0.0, 1.0 - beat * 3.0) ** 2
        values = []
        for i in range(N_BANDS):
            pos = i / (N_BANDS - 1)
            bass = kick * math.exp(-pos * 6.0)
            mid = 0.45 * math.exp(-((pos - 0.42) * 3.0) ** 2) * \
                (0.5 + 0.5 * math.sin(self._t * 2.1 + pos * 7.0))
            air = 0.30 * (pos ** 1.6) * (0.4 + 0.6 * abs(math.sin(self._t * 0.9 + pos * 13.0)))
            values.append(max(0.02, min(1.0, bass + mid + air)))
        with self._lock:
            self._frame_id += 1
            self._bands = np.array(values, dtype=np.float32)
            self.level = float(self._bands.mean())
            self._cond.notify_all()
            self._last_frame_id = self._frame_id
            return list(map(float, self._bands))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='音频频段引擎')
    parser.add_argument('--probe', action='store_true', help='只打印解析到的输出设备')
    parser.add_argument('--test', action='store_true', help='使用合成频段')
    parser.add_argument('--fps', type=int, default=60)
    parser.add_argument('--device', type=int, default=None, help='显式指定 sink 节点 id')
    args = parser.parse_args(argv)

    if args.probe:
        node_id, why = resolve_default_sink()
        print(f'sink 节点 = {node_id}  （{why}）')
        return 0 if node_id is not None else 1

    analyzer = SyntheticAnalyzer() if args.test else Analyzer(device=args.device)
    analyzer.start()
    period = 1.0 / max(1, args.fps)
    try:
        while True:
            frame = analyzer.read(timeout=period)
            try:
                print(json.dumps(frame, separators=(',', ':')) if frame else 'null', flush=True)
            except BrokenPipeError:
                break
            time.sleep(max(0.0, period - 0.002))
    except KeyboardInterrupt:
        pass
    finally:
        analyzer.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
