#!/usr/bin/env python3
"""假网易云播放器：真客户端没在跑时，用它验证整条链路（MPRIS → 歌词 → 顶栏）。

会占一个 `org.mpris.MediaPlayer2.netease_cloud_music_gtk4` 总线名（真客户端在跑就占不上），
对外提供标准 MPRIS 属性，进度自己往前走，歌词由 lyrics.py 从网易云现抓。

    tools/fake_mpris.py                     默认放《晴天》(id 186016)
    tools/fake_mpris.py --seconds 30        跑 30 秒自动退
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from gi.repository import GLib, Gio

BUS_NAME = 'org.mpris.MediaPlayer2.netease_cloud_music_gtk4'
PATH = '/org/mpris/MediaPlayer2'

INTROSPECTION = """
<node>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg type="s" direction="in"/><arg type="a{sv}" direction="out"/>
    </method>
    <method name="Set">
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="v" direction="in"/><arg type="b" direction="out"/>
    </method>
    <signal name="PropertiesChanged"><arg type="s"/><arg type="a{sv}"/><arg type="as"/></signal>
  </interface>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="Identity" type="s" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="PlayPause"/>
    <method name="Play"/>
    <method name="Pause"/>
    <method name="Next"/>
    <method name="Previous"/>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Position" type="x" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
  </interface>
</node>
"""


class FakePlayer:
    def __init__(self, song_id: str, title: str, artist: str, album: str, duration_ms: int):
        self.started = time.monotonic()
        self.paused_at = 0.0
        self.playing = True
        self.volume = 0.8
        self.song_id = song_id
        self.title = title
        self.artist = artist
        self.album = album
        self.duration_ms = duration_ms

        self.node_info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        for interface in self.node_info.interfaces:
            self.connection.register_object(PATH, interface, self._on_method_call, None, None)
        self.owner = Gio.bus_own_name_on_connection(
            self.connection, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)

    def position_us(self) -> int:
        if not self.playing:
            return int(self.paused_at * 1e6)
        elapsed = (time.monotonic() - self.started) % (self.duration_ms / 1000.0)
        return int(elapsed * 1e6)

    def properties(self, interface: str) -> dict:
        if interface == 'org.mpris.MediaPlayer2':
            return {'Identity': GLib.Variant('s', 'NetEase Cloud Music Gtk4 (fake)'),
                    'CanQuit': GLib.Variant('b', False)}
        if interface == 'org.mpris.MediaPlayer2.Player':
            metadata = {
                'mpris:trackid': GLib.Variant('o', '/org/mpris/MediaPlayer2/Track/1'),
                'mpris:length': GLib.Variant('x', self.duration_ms * 1000),
                'xesam:title': GLib.Variant('s', self.title),
                'xesam:artist': GLib.Variant('as', [self.artist]),
                'xesam:album': GLib.Variant('s', self.album),
                'xesam:url': GLib.Variant('s', f'https://music.163.com/song?id={self.song_id}'),
            }
            return {
                'PlaybackStatus': GLib.Variant('s', 'Playing' if self.playing else 'Paused'),
                'Metadata': GLib.Variant('a{sv}', metadata),
                'Position': GLib.Variant('x', self.position_us()),
                'Volume': GLib.Variant('d', self.volume),
            }
        return {}

    def _on_method_call(self, _conn, _sender, _path, _interface, method, parameters, invocation):
        try:
            if method == 'Get':
                interface, prop = parameters.unpack()
                props = self.properties(interface)
                value = props.get(prop, GLib.Variant('s', ''))
                invocation.return_value(GLib.Variant('(v)', (value,)))
            elif method == 'GetAll':
                (interface,) = parameters.unpack()
                invocation.return_value(GLib.Variant('(a{sv})', (self.properties(interface),)))
            elif method == 'PlayPause':
                if self.playing:
                    self.paused_at = time.monotonic() - self.started
                else:
                    self.started = time.monotonic() - self.paused_at
                self.playing = not self.playing
                invocation.return_value(None)
            elif method in ('Play', 'Pause', 'Next', 'Previous', 'Raise', 'Quit'):
                if method == 'Pause':
                    self.paused_at = time.monotonic() - self.started
                    self.playing = False
                if method == 'Play':
                    self.started = time.monotonic() - self.paused_at
                    self.playing = True
                invocation.return_value(None)
            else:
                invocation.return_value(GLib.Variant('(b)', (True,)))
        except Exception as exc:
            print(f'[fake-mpris] {method} 出错：{exc}', file=sys.stderr, flush=True)
            try:
                invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed', str(exc))
            except Exception:
                pass
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description='假网易云 MPRIS 播放器')
    parser.add_argument('--song-id', default='186016')
    parser.add_argument('--title', default='晴天')
    parser.add_argument('--artist', default='周杰伦')
    parser.add_argument('--album', default='叶惠美')
    parser.add_argument('--duration-ms', type=int, default=269000)
    parser.add_argument('--seconds', type=float, default=0.0, help='跑多久后退出，0 = 一直跑')
    args = parser.parse_args()

    player = FakePlayer(args.song_id, args.title, args.artist, args.album, args.duration_ms)
    loop = GLib.MainLoop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: loop.quit())
    if args.seconds:
        GLib.timeout_add(int(args.seconds * 1000), lambda: (loop.quit(), False)[1])

    print(f'假网易云已上线：{BUS_NAME} · 《{args.title}》id={args.song_id}', flush=True)
    loop.run()
    Gio.bus_unown_name(player.owner)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
