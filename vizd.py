#!/usr/bin/env python3
"""网易云顶栏音频可视化 —— 后台服务

一帧一帧地把「现在该画什么」写进 $XDG_RUNTIME_DIR/netease-viz/state.json：

    · bands  : 24 个频段能量（音节跳动 + 颜色渐变的原料）
    · level  : 整体响度（线条亮度/染色跟着它走）
    · line   : 当前该显示的歌词（顶栏扩展负责"上移淡出、从下面升起"的动画）

顶栏扩展（GNOME Shell 扩展 netease-viz@maus.local）只读这个文件，
不联网、不做 DSP；音频采样在 audiobands.py，歌词在 lyrics.py，各自单文件。

两条省电规则：
    · 没画面（网易云没开 / 歌停了）时降频到空闲帧率，别空转 60fps；
    · 长时间没画面就把音频采集关掉，等网易云回来了再开。

用法：
    vizd.py                 # 正常跑：有播放器就采音频 + 抓歌词
    vizd.py --test          # 合成音频（不碰真实音频）便于验证画面
    vizd.py --print         # 跑在前台并把每帧状态打到 stdout
    vizd.py --fps 45        # 换帧率
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audiobands import Analyzer, SyntheticAnalyzer  # noqa: E402
from lyrics import LyricsTracker  # noqa: E402

RUNTIME_DIR = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}') / 'netease-viz'
CONFIG_PATH = Path(os.environ.get('XDG_CONFIG_HOME') or (Path.home() / '.config')) / 'netease-viz' / 'config.json'
STATE_PATH = RUNTIME_DIR / 'state.json'
LOCK_PATH = RUNTIME_DIR / 'daemon.lock'

# 音频频段数量（扩展侧按收到的数组长度画，不写死；这里只用于占位）
NBARS = 48

# 暂停后仍然把歌词留在顶栏一会儿，别一按暂停就消失
PAUSE_LINGER_SEC = 12.0
# 配置（含歌词延迟）多久重读一次
CONFIG_RELOAD_SEC = 1.0
# 没画面时的空闲帧率（每秒写几次 state.json）
IDLE_FPS = 2.0
# 没画面多久后把音频采集停掉（秒）
ANALYZER_IDLE_SEC = 15.0


def load_offset_ms() -> int:
    """从扩展写的那份 config.json 里读歌词延迟（毫秒）。"""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        value = int(data.get('offset_ms', 0))
    except Exception:
        return 0
    return max(-2000, min(2000, value))


class VisualizerDaemon:
    def __init__(self, fps: int = 60, test: bool = False, verbose: bool = False):
        self.fps = max(10, min(120, fps))
        self.period = 1.0 / self.fps
        self.verbose = verbose
        self.test_mode = test
        self.analyzer = SyntheticAnalyzer() if test else Analyzer()
        self.tracker = None if test else LyricsTracker()
        self.running = True
        self.seq = 0
        self.last_playing_at = 0.0
        self.offset_ms = 0
        self.next_config_at = 0.0
        self.last_frames: list[float] = []
        self._lock_file = None
        self.analyzer_running = False
        self.analyzer_can_restart = True
        self.last_visible_at = 0.0
        self.visible = False

    # ------------------------------------------------------------ 生命周期 --
    def start(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        # 单实例：第二个实例直接退出，免得两边抢着写同一个 state.json
        self._lock_file = open(LOCK_PATH, 'w')
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print('[vizd] 已经有一个后台服务在跑了，这个实例退出', file=sys.stderr, flush=True)
            raise SystemExit(0)
        self._lock_file.write(f'{os.getpid()}\n')
        self._lock_file.flush()
        self.analyzer.start()
        self.analyzer_running = True
        self.offset_ms = load_offset_ms()
        if self.tracker is not None:
            try:
                self.tracker.set_offset_ms(self.offset_ms)
            except Exception:
                pass
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._on_signal)

    def stop(self) -> None:
        self.running = False
        try:
            self.analyzer.stop()
        finally:
            if self.tracker is not None:
                try:
                    self.tracker.close()
                except Exception:
                    pass

    def _on_signal(self, *_args) -> None:
        self.running = False

    # ---------------------------------------------------------------- 主循环 --
    def run(self) -> None:
        self.start()
        next_frame = time.monotonic()
        try:
            while self.running:
                self.step()
                # 有画面就跑满 fps，没画面就降到 IDLE_FPS
                period = self.period if self.visible else 1.0 / IDLE_FPS
                next_frame += period
                delay = next_frame - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    # 落后太多就别硬追，重新对齐节拍
                    next_frame = time.monotonic()
        finally:
            self.stop()

    # ------------------------------------------------------------ 音频采集 --
    def _ensure_analyzer(self, want_running: bool) -> None:
        """长时间没画面就把采集关掉；播放器回来再开（关不掉就一直开着，不影响正确性）。"""
        if want_running == self.analyzer_running:
            return
        if not self.analyzer_can_restart:
            return
        try:
            if want_running:
                self.analyzer.start()
            else:
                self.analyzer.stop()
            self.analyzer_running = want_running
        except Exception as exc:
            # 采集器不支持重启：以后就一直开着，只是多花一点电
            self.analyzer_can_restart = False
            self.analyzer_running = True
            print(f'[vizd] 音频采集开关失败，改为常开：{exc}', file=sys.stderr, flush=True)

    def step(self) -> dict:
        now = time.monotonic()
        if now >= self.next_config_at:
            self.next_config_at = now + CONFIG_RELOAD_SEC
            offset = load_offset_ms()
            if offset != self.offset_ms:
                self.offset_ms = offset
                if self.tracker is not None:
                    try:
                        self.tracker.set_offset_ms(offset)
                    except Exception:
                        pass

        lyric = self._poll_lyrics()

        playing = bool(lyric.get('playing'))
        if playing:
            self.last_playing_at = now
        visible = bool(lyric.get('player')) and (playing or now - self.last_playing_at < PAUSE_LINGER_SEC)
        self.visible = visible

        if visible:
            self.last_visible_at = now
        # 没画面超过一会儿就停采集；有画面则确保采集开着
        if not visible and now - self.last_visible_at > ANALYZER_IDLE_SEC:
            self._ensure_analyzer(False)
        elif visible:
            self._ensure_analyzer(True)

        if self.analyzer_running:
            bands = self.analyzer.read(timeout=0.0)
            if bands is None:
                bands = self.last_frames or [0.0] * NBARS
            self.last_frames = list(bands)
        else:
            # 采集关着：线条压平，网易云一回来立刻重新采
            bands = [0.0] * NBARS
            self.last_frames = []

        # 响度优先用引擎自己算的（它知道 dB 基准），拿不到再退回按频段估
        level = getattr(self.analyzer, 'level', None)
        if level is None:
            level = self._level_of(bands)
        state = {
            'v': 1,
            'ts': round(time.time(), 4),   # 墙上时钟：扩展靠它判断后台是不是还活着
            'seq': self.seq,
            'visible': visible,
            'playing': playing,
            'player': lyric.get('player'),
            'title': lyric.get('title', ''),
            'artist': lyric.get('artist', ''),
            'album': lyric.get('album', ''),
            'duration_ms': int(lyric.get('duration_ms') or 0),
            'position_ms': int(lyric.get('position_ms') or 0),
            'line': lyric.get('line', ''),
            'translation': lyric.get('translation', ''),
            'next_line': lyric.get('next_line', ''),
            'line_idx': int(lyric.get('line_idx', -1)),
            'line_count': int(lyric.get('line_count', 0)),
            'lyric_status': lyric.get('status', 'idle'),
            'offset_ms': self.offset_ms,
            'level': round(level, 4),
            'bands': [round(float(v), 4) for v in bands],
        }
        self.seq += 1
        self._publish(state)
        if self.verbose:
            print(json.dumps({k: state[k] for k in
                              ('playing', 'title', 'line', 'level', 'lyric_status')},
                             ensure_ascii=False), flush=True)
        return state

    def _poll_lyrics(self) -> dict:
        if self.tracker is None:
            return {'player': 'synthetic', 'playing': True, 'line': '', 'status': 'test'}
        try:
            return self.tracker.poll() or {}
        except Exception as exc:  # 歌词引擎出错也不能拖垮画面
            print(f'[vizd] 歌词引擎异常：{exc}', file=sys.stderr, flush=True)
            return {'player': None, 'playing': False, 'line': '', 'status': 'idle'}

    @staticmethod
    def _level_of(bands) -> float:
        if not bands:
            return 0.0
        peaks = sorted(bands, reverse=True)[:max(1, len(bands) // 4)]
        return max(0.0, min(1.0, sum(peaks) / len(peaks)))

    def _publish(self, state: dict) -> None:
        """原子落盘：扩展永远要么读到旧的一帧、要么读到完整的新一帧。"""
        # 临时文件名带上 pid：万一真有第二个实例在跑，也不会互相把临时文件"吃掉"
        tmp_path = STATE_PATH.with_name(f'state.json.{os.getpid()}.tmp')
        try:
            tmp_path.write_text(json.dumps(state, ensure_ascii=False, separators=(',', ':')),
                                encoding='utf-8')
            os.replace(tmp_path, STATE_PATH)
        except Exception as exc:
            print(f'[vizd] 写状态失败：{exc}', file=sys.stderr, flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='网易云顶栏可视化后台服务')
    parser.add_argument('--fps', type=int, default=60, help='状态刷新帧率（默认 60）')
    parser.add_argument('--test', action='store_true', help='用合成音频，不抓真实声音')
    parser.add_argument('--print', dest='verbose', action='store_true', help='把每帧摘要打到 stdout')
    args = parser.parse_args(argv)

    daemon = VisualizerDaemon(fps=args.fps, test=args.test, verbose=args.verbose)
    try:
        daemon.run()
    except KeyboardInterrupt:
        daemon.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
