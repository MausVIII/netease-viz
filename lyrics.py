"""网易云「现在在放什么 + 该显示哪句词」——给顶栏可视化的后台服务用。

只做三件事：
    1. 在会话总线上自动找到网易云的 MPRIS 播放器（不写死总线名）；
    2. 拿到歌名/歌手/时长/播放进度；
    3. 按歌曲 id 去网易云取 LRC，算出当前该显示哪一句。

poll() 由 daemon 每帧调用（默认 60fps），所以这里所有重活都必须异步或者带缓存：
MPRIS 每 0.5 秒探一次，进度用本地单调时钟外推，歌词抓取丢到后台线程。
任何异常都不许冒泡出去，最差也只是「没有歌词」。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from gi.repository import Gio, GLib
    _HAS_GIO = True
except Exception:  # pragma: no cover - 没装 GIR 时降级成「永远没有播放器」
    Gio = GLib = None
    _HAS_GIO = False

MPRIS_PREFIX = 'org.mpris.MediaPlayer2'
PLAYER_IFACE = 'org.mpris.MediaPlayer2.Player'
PROPS_IFACE = 'org.freedesktop.DBus.Properties'
ROOT_PATH = '/org/mpris/MediaPlayer2'

# 认哪个播放器：总线名或 Identity 里出现这些词就算网易云
NETEASE_HINTS = ('netease', 'cloud_music', 'cloudmusic', '网易云')

PROBE_SEC = 0.5          # MPRIS 探测间隔（秒）
DISCOVER_SEC = 2.0       # 没找到播放器时重扫总线的间隔（秒）
LEAD_MS = 200            # 歌词提前一点点出现，配合顶栏的切句动画
HTTP_TIMEOUT = 3.0

LYRIC_API = 'https://music.163.com/api/song/lyric'
SEARCH_API = 'https://music.163.com/api/search/get'
HTTP_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0 Safari/537.36'),
    'Referer': 'https://music.163.com/',
    'Accept': 'application/json, text/plain, */*',
}

CACHE_DIR = Path(os.environ.get('XDG_CACHE_HOME') or (Path.home() / '.cache')) / 'netease-viz' / 'lyrics'

_TAG_RE = re.compile(r'\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]')
_META_RE = re.compile(r'^\[(ti|ar|al|by|offset|re|ve|kana):', re.IGNORECASE)

# 歌词正文里夹着的制作信息（"作词 : 方文山" 这种）不该占顶栏，过滤掉。
# 必须带冒号才算，免得把真歌词里以"曲"/"词"开头的句子误伤。
# 制作信息行：只认这些**固定标签**（闭集白名单），免得误伤真歌词。
_CREDIT_RE = re.compile(
    r'^(作词|作詞|作曲|编曲|編曲|制作人|製作人|制作|製作|监制|監製|出品人|出品|发行|發行|'
    r'录音师|錄音師|录音工程|錄音工程|录音助理|錄音助理|录音室|錄音室|录音|錄音|混音师|混音師|混音|母带|母帶|'
    r'和声编写|和聲編寫|和声|和聲|吉他手|吉他|贝斯手|貝斯手|贝斯|貝斯|鼓手|鼓|键盘手|鍵盤手|键盘|鍵盤|'
    r'钢琴|鋼琴|弦乐|弦樂|合声|合聲|配唱|演唱|歌手|歌曲|歌词|歌詞|'
    r'翻译|翻譯|插曲|插入歌|主题歌|主題歌|片头曲|片頭曲|片尾曲|原唱|翻唱|'
    r'arrange|program|mix|mastering|pv|mv|op|sp|lyric|composer)'
    r'\s*[:：]', re.IGNORECASE)


def _looks_like_credit(text: str) -> bool:
    """制作信息行：'作词 : xxx'、'混音师 : xxx' 以及开头的 '歌名 - 歌手'。"""
    if _CREDIT_RE.match(text):
        return True
    # "夜曲 - 周杰伦" 这种标题行：短、只有一个破折号、不像歌词
    if ' - ' in text and len(text) <= 40 and text.count(' - ') == 1:
        left, right = (part.strip() for part in text.split(' - ', 1))
        if 0 < len(left) <= 20 and 0 < len(right) <= 20:
            return True
    return False


# --------------------------------------------------------------------- 工具 --

def _first(value, default=''):
    """MPRIS 里有的字段是数组（比如 artist），统一取第一个字符串。"""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else default
    if value is None:
        return default
    return str(value)


def parse_lrc(text: str, offset_ms: int = 0) -> list[dict]:
    """把 LRC 文本解析成 [{t: 毫秒, text: 歌词}]，已按时间排序。"""
    entries = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or _META_RE.match(line):
            continue
        stamps = list(_TAG_RE.finditer(line))
        if not stamps:
            continue
        body = line[stamps[-1].end():].strip()
        if not body:
            continue
        if _looks_like_credit(body):
            continue
        for match in stamps:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            frac = match.group(3) or '0'
            millis = int(frac.ljust(3, '0')[:3])
            when = (minutes * 60 + seconds) * 1000 + millis + offset_ms
            entries.append({'t': max(0, when), 'text': body})
    entries.sort(key=lambda item: item['t'])
    return entries


def merge_translation(lines: list[dict], tlyric: str, offset_ms: int = 0) -> None:
    """把翻译按时间戳贴到原文上（差 120ms 以内就算同一句）。"""
    if not tlyric:
        return
    translated = parse_lrc(tlyric, offset_ms)
    if not translated:
        return
    index = 0
    for line in lines:
        while index < len(translated) and translated[index]['t'] < line['t'] - 120:
            index += 1
        if index < len(translated) and abs(translated[index]['t'] - line['t']) <= 120:
            line['tr'] = translated[index]['text']


def _http_json(url: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f'{url}?{query}', headers=HTTP_HEADERS)
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode('utf-8', 'replace'))


def _song_id_from_url(url: str) -> str:
    """xesam:url 形如 https://music.163.com/song?id=186016，也可能带 #/song?id="""
    if not url:
        return ''
    match = re.search(r'[?&#]id=(\d+)', url)
    return match.group(1) if match else ''


# ------------------------------------------------------------- 假播放器给测试 --

class _Dummy:  # pragma: no cover - 占位，避免 None 判空散落各处
    pass


class LyricsTracker:
    """轮询 MPRIS + 歌词，poll() 永远快速返回（拿不到就返回空状态）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._closed = False
        self._offset_ms = 0
        self._cache: dict[str, dict] = {}
        self._fetching: set[str] = set()

        self._bus = None
        if _HAS_GIO:
            try:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except Exception:
                self._bus = None

        self._player: str | None = None
        self._identity = ''
        self._last_discover = 0.0
        self._last_probe = 0.0

        self._playing = False
        self._title = ''
        self._artist = ''
        self._album = ''
        self._duration_ms = 0
        self._track_key: str | None = None
        self._base_pos_ms = 0.0
        self._base_at = 0.0
        self._saw_position = False

        self._lines: list[dict] = []
        self._status = 'idle'

    # ------------------------------------------------------------- 对外接口 --
    def set_offset_ms(self, offset_ms: int) -> None:
        """正数 = 歌词慢一点（比音频晚出现）。"""
        self._offset_ms = max(-5000, min(5000, int(offset_ms)))

    def close(self) -> None:
        self._closed = True
        self._bus = None

    def poll(self) -> dict:
        try:
            now = time.monotonic()
            if now - self._last_probe >= PROBE_SEC:
                self._last_probe = now
                self._probe(now)
            return self._snapshot(now)
        except Exception as exc:  # 任何意外都降级，绝不拖垮顶栏
            return {
                'player': self._player, 'playing': False, 'title': self._title,
                'artist': self._artist, 'album': self._album, 'duration_ms': 0,
                'position_ms': 0, 'line': '', 'translation': '', 'next_line': '',
                'line_idx': -1, 'line_count': 0, 'status': f'error: {exc}',
            }

    # ------------------------------------------------------------- MPRIS --
    def _call(self, bus_name: str, interface: str, method: str, params=None, timeout=800):
        return self._bus.call_sync(bus_name, ROOT_PATH, interface, method, params,
                                   None, Gio.DBusCallFlags.NONE, timeout, None)

    def _get_prop(self, bus_name: str, interface: str, prop: str):
        reply = self._call(bus_name, PROPS_IFACE, 'Get',
                           GLib.Variant('(ss)', (interface, prop)))
        return reply.unpack()[0]

    def _discover(self, now: float) -> None:
        """在会话总线上找网易云。总线名/Identity 命中即认。"""
        override = os.environ.get('VIZ_MPRIS')
        candidates: list[str] = []
        if override:
            candidates = [override]
        else:
            reply = self._bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                                        'org.freedesktop.DBus', 'ListNames', None,
                                        None, Gio.DBusCallFlags.NONE, 800, None)
            names = [str(name) for name in reply.unpack()[0]]
            candidates = [name for name in names if name.startswith(MPRIS_PREFIX)]

        best = None
        for name in candidates:
            if any(hint in name.lower() for hint in NETEASE_HINTS):
                best = name
                break
            if override:
                best = name
                break
            try:
                identity = str(self._get_prop(name, 'org.mpris.MediaPlayer2', 'Identity') or '')
            except Exception:
                continue
            if any(hint in identity.lower() for hint in NETEASE_HINTS):
                best = (name, identity)
                break

        if isinstance(best, tuple):
            self._player, self._identity = best
        elif best:
            try:
                identity = str(self._get_prop(best, 'org.mpris.MediaPlayer2', 'Identity') or '')
            except Exception:
                identity = ''
            self._player, self._identity = best, identity
        else:
            self._player, self._identity = None, ''
        self._last_discover = now

    def _probe(self, now: float) -> None:
        if self._bus is None:
            self._player, self._playing = None, False
            self._status = 'idle'
            return

        # 没有播放器：偶尔重扫总线（网易云开没开由这里决定）
        if self._player is None:
            if now - self._last_discover >= DISCOVER_SEC:
                self._discover(now)
            if self._player is None:
                self._reset_track()
                self._playing = False
                self._status = 'idle'
                return

        try:
            metadata = self._get_prop(self._player, PLAYER_IFACE, 'Metadata') or {}
            playback = str(self._get_prop(self._player, PLAYER_IFACE, 'PlaybackStatus') or '')
            try:
                position_us = int(self._get_prop(self._player, PLAYER_IFACE, 'Position') or 0)
            except Exception:
                position_us = 0
        except Exception:
            # 播放器刚关掉：下一次重扫
            self._player = None
            self._last_discover = 0.0
            self._playing = False
            self._status = 'idle'
            return

        title = _first(metadata.get('xesam:title'))
        artist = _first(metadata.get('xesam:artist'))
        album = _first(metadata.get('xesam:album'))
        try:
            duration_ms = int(metadata.get('mpris:length') or 0) // 1000
        except Exception:
            duration_ms = 0
        url = _first(metadata.get('xesam:url'))
        track_id = _first(metadata.get('mpris:trackid'))
        key = track_id or f'{title}|{artist}'

        self._title, self._artist, self._album = title, artist, album
        self._duration_ms = duration_ms
        self._playing = playback.lower() == 'playing'

        if key != self._track_key:
            self._track_key = key
            self._lines = []
            self._status = 'loading'
            self._base_pos_ms = 0.0
            self._base_at = now
            self._saw_position = False
            self._start_fetch(key, title, artist, url)

        # 进度：以播放器报的值为准，但播放器有时只回 0，别被它拖回开头
        reported_ms = position_us / 1000.0
        extrapolated = self._extrapolate(now)
        if self._playing and reported_ms <= 0.0 and extrapolated > 5000.0:
            pass                       # 忽略这个 0，继续本地外推
        else:
            self._base_pos_ms = reported_ms
            self._base_at = now
            self._saw_position = reported_ms > 0.0

    # ------------------------------------------------------------- 进度 --
    def _extrapolate(self, now: float) -> float:
        if not self._playing:
            return self._base_pos_ms
        elapsed_ms = (now - self._base_at) * 1000.0
        position = self._base_pos_ms + elapsed_ms
        if self._duration_ms > 0:
            position = min(position, self._duration_ms)
        return position

    # ------------------------------------------------------------- 歌词 --
    def _start_fetch(self, key: str, title: str, artist: str, url: str) -> None:
        cached = self._cache.get(key)
        if cached is not None:
            self._lines = cached.get('lines', [])
            self._status = 'ok' if self._lines else 'none'
            return
        if key in self._fetching:
            return
        self._fetching.add(key)
        thread = threading.Thread(target=self._fetch_worker,
                                  args=(key, title, artist, url), daemon=True)
        thread.start()

    def _fetch_worker(self, key: str, title: str, artist: str, url: str) -> None:
        try:
            data = self._load_lyric(title, artist, url)
        except Exception:
            data = None
        finally:
            self._fetching.discard(key)
        if self._closed:
            return
        with self._lock:
            self._cache[key] = data or {'lines': []}
            if key == self._track_key:
                self._lines = (data or {}).get('lines', [])
                self._status = 'ok' if self._lines else 'none'

    def _load_lyric(self, title: str, artist: str, url: str) -> dict | None:
        song_id = _song_id_from_url(url)
        if not song_id:
            song_id = self._search_song(title, artist)
        if not song_id:
            return None

        payload = self._cached_payload(song_id)
        if payload is None:
            payload = _http_json(LYRIC_API, {'id': song_id, 'lv': -1, 'kv': -1, 'tv': -1})
            self._store_payload(song_id, payload)

        lrc = ((payload or {}).get('lrc') or {}).get('lyric') or ''
        tlyric = ((payload or {}).get('tlyric') or {}).get('lyric') or ''
        lines = parse_lrc(lrc)
        if not lines:
            return {'id': song_id, 'lines': []}
        merge_translation(lines, tlyric)
        return {'id': song_id, 'lines': lines}

    def _search_song(self, title: str, artist: str) -> str:
        keyword = f'{title} {artist}'.strip()
        if not keyword:
            return ''
        try:
            payload = _http_json(SEARCH_API, {'s': keyword, 'type': 1, 'limit': 1})
            songs = ((payload or {}).get('result') or {}).get('songs') or []
            if songs:
                return str(songs[0].get('id', '')) or ''
        except Exception:
            return ''
        return ''

    def _cached_payload(self, song_id: str) -> dict | None:
        path = CACHE_DIR / f'{song_id}.json'
        try:
            if path.exists():
                return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return None
        return None

    def _store_payload(self, song_id: str, payload: dict) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_DIR / f'.{song_id}.tmp'
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            os.replace(tmp, CACHE_DIR / f'{song_id}.json')
        except Exception:
            pass   # 缓存失败无所谓，下次再抓

    # ------------------------------------------------------------- 汇总 --
    def _reset_track(self) -> None:
        self._track_key = None
        self._lines = []
        self._title = ''
        self._artist = ''
        self._album = ''
        self._duration_ms = 0
        self._base_pos_ms = 0.0
        self._base_at = 0.0

    def _snapshot(self, now: float) -> dict:
        position_ms = int(self._extrapolate(now))
        # 正数 offset = 歌词慢一点：拿更早的位置去找该显示哪句
        lyric_ms = position_ms - self._offset_ms + LEAD_MS

        index = -1
        lines = self._lines
        for i, line in enumerate(lines):
            if line['t'] <= lyric_ms:
                index = i
            else:
                break

        current = lines[index] if 0 <= index < len(lines) else {}
        nxt = lines[index + 1] if 0 <= index + 1 < len(lines) else {}
        status = self._status
        if self._player and status == 'idle':
            status = 'loading'

        return {
            'player': self._player,
            'playing': self._playing,
            'title': self._title,
            'artist': self._artist,
            'album': self._album,
            'duration_ms': self._duration_ms,
            'position_ms': position_ms,
            'line': current.get('text', ''),
            'translation': current.get('tr', ''),
            'next_line': nxt.get('text', ''),
            'line_idx': index,
            'line_count': len(lines),
            'status': status,
        }
