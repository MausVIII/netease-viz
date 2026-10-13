#!/usr/bin/env python3
"""修网易云（gmg137 GTK4 客户端）打不开的毛病：播放列表存档自相矛盾。

症状：点图标没反应 / 闪一下就没了，日志里有
    thread 'main' panicked at src/audio/playlist.rs:335:
    removal index (is N) should be < len (is N)
原因：客户端自身的 bug——在"随机播放"模式下删歌时，主列表删了、shuffle 队列没删
（或反过来），两边长度差 1；下次启动恢复列表时按越界下标去删，直接 panic 退出。

做法：备份 playlist.json，然后把 shuffle 里已经不在主列表的歌去掉，并把越界的位置
夹回合法范围。幂等，可以反复跑。

    python3 tools/fix_netease_playlist.py --check     # 只看问题，不改
    python3 tools/fix_netease_playlist.py             # 备份并修复
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

PLAYLIST = (Path.home() / '.var/app/com.github.gmg137.netease-cloud-music-gtk'
            / 'config/netease-cloud-music-gtk4/playlist.json')


def analyse(data: dict) -> list[str]:
    problems = []
    songs = data.get('list') or []
    shuffle = data.get('shuffle') or []
    ids = {song.get('id') for song in songs}
    stale = [song for song in shuffle if song.get('id') not in ids]
    if stale:
        names = ', '.join(repr(song.get('name')) for song in stale[:3])
        problems.append(f'shuffle 里有 {len(stale)} 首不在主列表里：{names}')
    if len(songs) != len(shuffle) and not stale:
        problems.append(f'主列表 {len(songs)} 首、shuffle {len(shuffle)} 首，长度不一致')
    position = data.get('position')
    if not isinstance(position, int) or not (0 <= position < max(1, len(songs))):
        problems.append(f'position={position} 越界（主列表 {len(songs)} 首，合法 0~{len(songs) - 1}）')
    return problems


def repair(data: dict) -> dict:
    songs = data.get('list') or []
    ids = {song.get('id') for song in songs}
    data['shuffle'] = [song for song in (data.get('shuffle') or []) if song.get('id') in ids]
    if ids:
        index = next((i for i, song in enumerate(songs)
                      if song.get('id') == data.get('current_song_id')), None)
        if index is None:
            index = len(songs) - 1
        position = data.get('position')
        if not isinstance(position, int) or not (0 <= position < len(songs)):
            data['position'] = index
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description='修复网易云播放列表存档')
    parser.add_argument('--check', action='store_true', help='只检查不修改')
    parser.add_argument('--path', type=Path, default=PLAYLIST)
    args = parser.parse_args()

    if not args.path.exists():
        print(f'找不到 {args.path}')
        return 1

    data = json.loads(args.path.read_text(encoding='utf-8'))
    problems = analyse(data)
    if not problems:
        print('播放列表存档正常，不用修 ✓')
        return 0

    print('发现问题：')
    for line in problems:
        print(f'  · {line}')
    if args.check:
        return 0

    backup = args.path.with_name(f'playlist.json.bak-fix-{time.strftime("%Y%m%d-%H%M%S")}')
    shutil.copy2(args.path, backup)
    args.path.write_text(json.dumps(repair(data), ensure_ascii=False), encoding='utf-8')
    print(f'已修复，原文件备份在 {backup.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
