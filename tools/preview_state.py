#!/usr/bin/env python3
"""预览用假状态源（零依赖）。

不抓音频、不连网易云，直接按协议往 $XDG_RUNTIME_DIR/atri-visualizer/state.json
写假数据，让顶栏扩展可以单独调画面：

    tools/preview_state.py                       一直跑到 Ctrl-C
    tools/preview_state.py --seconds 30          跑 30 秒就停
    tools/preview_state.py --line-seconds 2      每 2 秒换一句（冒烟测试用）

真机播放走 atri_visd.py，两者写同一种 state.json，扩展不需要区分。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path

RUNTIME_DIR = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}') / 'atri-visualizer'
STATE_PATH = RUNTIME_DIR / 'state.json'

NBARS = 48

# (原词, 翻译)：中文歌翻译留空 → 单栏；外文歌有翻译 → 左中文、右原词双栏
LINES = [
    ("続く物語の真ん中で", "在这延续的故事中"),
    ("『高性能』的顶栏，是亚托莉亲自做的", ""),
    ("沈むように溶けてゆくように", "如同沉没般、如同融化般"),
    ("上句向上淡出，下句从下面慢慢升上来", ""),
    ("Never gonna give you up", "永远不会放弃你"),
]

SEQ = 0


def synth_bands(t: float) -> list[float]:
    """一段好看的假频谱：低频脉冲 + 中频游走 + 高频细碎抖动。"""
    beat = 0.5 + 0.5 * math.sin(t * 2.4)
    bands = []
    for i in range(NBARS):
        x = i / (NBARS - 1)
        low = (1.0 - x) ** 2.2 * beat
        mid = 0.42 * math.sin(t * 1.7 - x * 5.0) * (1 - abs(x - 0.5) * 1.4)
        high = 0.16 * math.sin(t * 11.0 + i * 2.1)
        bands.append(max(0.0, min(1.0, 0.18 + 1.05 * abs(low + mid + high))))
    return bands


def publish(bands: list[float], level: float, line: str, translation: str, index: int,
            seconds_per_line: float) -> None:
    global SEQ
    state = {
        'v': 1,
        'ts': round(time.time(), 4),
        'seq': SEQ,
        'visible': True,
        'playing': True,
        'player': 'org.mpris.MediaPlayer2.netease_cloud_music_gtk4',
        'title': '预览：ATRI 顶栏可视化',
        'artist': 'ATRI',
        'album': '',
        'duration_ms': int(seconds_per_line * len(LINES) * 1000),
        'position_ms': int((time.monotonic() % (seconds_per_line * len(LINES))) * 1000),
        'line': line,
        'translation': translation,
        'next_line': LINES[(index + 1) % len(LINES)][0],
        'line_idx': index,
        'line_count': len(LINES),
        'lyric_status': 'preview',
        'offset_ms': 0,
        'level': round(level, 4),
        'bands': [round(v, 4) for v in bands],
    }
    SEQ += 1
    tmp = STATE_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    os.replace(tmp, STATE_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description='ATRI 顶栏可视化 · 预览假状态源')
    parser.add_argument('--fps', type=int, default=60, help='刷新率（默认 60）')
    parser.add_argument('--seconds', type=float, default=0.0, help='跑多久后退出，0 = 一直跑')
    parser.add_argument('--line-seconds', type=float, default=4.6, help='每句歌词停留秒数')
    args = parser.parse_args()

    fps = max(10, min(120, args.fps))
    period = 1.0 / fps
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    next_frame = started
    while True:
        elapsed = time.monotonic() - started
        if args.seconds and elapsed > args.seconds:
            break
        index = int(elapsed / args.line_seconds) % len(LINES)
        line, translation = LINES[index]
        bands = synth_bands(elapsed)
        top = sorted(bands, reverse=True)[:NBARS // 4]
        publish(bands, sum(top) / len(top), line, translation, index, args.line_seconds)
        next_frame += period
        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_frame = time.monotonic()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
