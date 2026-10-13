// ATRI 顶栏音频可视化 —— GNOME Shell 扩展（Shell 45 ~ 50）
//
// 顶栏左侧：跟着音频上下跳动、颜色随振幅线性过渡的线条，带动态模糊拖尾。
// 顶栏右侧：跟着网易云播放进度逐句切换的歌词（上句上移淡出、下句从下方升起）。
//
// 两条硬约束（Maus 的要求）：
//   1. 不遮挡时钟与右侧状态图标 —— 两个控件都插进面板的真实布局里，由面板分配空间；
//      空间不够时先缩歌词、再缩线条、最后整体隐藏，绝不盖在别人身上。
//   2. 跟着网易云启动 —— 后台服务盯着 MPRIS，网易云一关，顶栏立刻干净。
//
// 分工：扩展只负责「读状态 + 画」。音频采样、FFT、MPRIS、歌词抓取都在后台服务
// atri-visualizer（~/.local/share/atri-visualizer/atri_visd.py）里做，状态通过
// $XDG_RUNTIME_DIR/atri-visualizer/state.json 传递。扩展不联网、不做 DSP，
// 后台挂了顶栏也只是安静地隐藏。

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

import {renderBars, paletteColor, PALETTES, PALETTE_ORDER, clamp01} from './bars.js';

const CONFIG_PATH = GLib.build_filenamev([GLib.get_user_config_dir(), 'atri-visualizer', 'config.json']);
const RUNTIME_DIR = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'atri-visualizer']);
const STATE_PATH = GLib.build_filenamev([RUNTIME_DIR, 'state.json']);
const CACHE_DIR = GLib.build_filenamev([GLib.get_user_cache_dir(), 'atri-visualizer']);
const FORCE_SHOW = GLib.build_filenamev([CACHE_DIR, 'force-show']);
const DEBUG_FLAG = GLib.build_filenamev([CACHE_DIR, 'debug']);
const SERVICE = 'atri-visualizer';

const FAST_MS = 16;          // 有画面时约 60fps
const SLOW_MS = 400;         // 没画面时低频轮询，省电
const BUDGET_MS = 250;       // 宽度预算复查间隔
const TRAIL_FRAMES = 7;      // 拖尾帧数（动态模糊的长度）
const STALE_SEC = 2.5;       // 状态文件超过这么久没更新就当后台挂了
const HIDE_AFTER_MISSES = 8; // 连续读不到状态多少次后隐藏
const GAP = 12;              // 与时钟 / 状态图标的最小间距
const MIN_BARS_WIDTH = 56;   // 线条区最小可用宽度
const MIN_LYRIC_RIGHT = 120; // 右侧歌词最小可用宽度
const MIN_LYRIC_LEFT = 84;   // 线条后歌词最小可用宽度
const DIVIDER_W = 13;        // 线条与歌词之间的分隔间距

const DEFAULT_CONFIG = {
    bars_position: 'leftmost',   // leftmost = Activities 右边；end = 左 box 末尾
    lyric_position: 'auto',      // auto | right（顶栏右侧，状态图标左边）| after-bars（紧挨线条）
    palette: 'aqua',
    bars_width: 236,
    lyric_width: 330,
    show_lyrics: true,
    show_translation: true,      // 非中文歌：左中文、右原词
    sensitivity: 1.0,            // 线条灵敏度：>1 更跳，<1 更沉稳
    autostart_daemon: true,
    offset_ms: 0,                // 歌词延迟（后台服务读同一个文件）
};

const BARS_WIDTHS = [176, 236, 300, 380];
const LYRIC_WIDTHS = [260, 330, 420, 520];
const LYRIC_POSITIONS = ['auto', 'right', 'after-bars'];
const SENSITIVITIES = [1.0, 1.4, 1.9, 0.7];           // 标准 → 灵敏 → 很灵敏 → 沉稳
const SENSITIVITY_LABELS = {0.7: '沉稳', 1.0: '标准', 1.4: '灵敏', 1.9: '很灵敏'};

function readConfig() {
    try {
        const [ok, bytes] = GLib.file_get_contents(CONFIG_PATH);
        if (!ok)
            return {...DEFAULT_CONFIG};
        return {...DEFAULT_CONFIG, ...JSON.parse(new TextDecoder().decode(bytes))};
    } catch (e) {
        return {...DEFAULT_CONFIG};
    }
}

function writeConfig(config) {
    try {
        GLib.mkdir_with_parents(GLib.path_get_dirname(CONFIG_PATH), 0o755);
        GLib.file_set_contents(CONFIG_PATH, JSON.stringify(config, null, 2));
    } catch (e) {
        logError(e, 'atri-visualizer: 写配置失败');
    }
}

// ---------------------------------------------------------------- 歌词视图 --

class LyricView {
    constructor(width) {
        this._width = width;
        this._text = '';
        this._translation = '';
        this._current = null;
        this._palette = 'aqua';
        this._level = 0;
        this._levelStep = -1;
        this._showTranslation = true;
        this._align = 'center';   // center = 在可用跨度里居中；start = 贴着左边起头
        this._pending = null;
        this._mappedId = 0;
        this._closed = false;

        this.actor = new St.Widget({
            name: 'atri-lyric-view',
            style_class: 'atri-lyric-view',
            layout_manager: new Clutter.BinLayout(),
            clip_to_allocation: true,
            y_expand: true,
            x_expand: false,
            reactive: false,
        });
        this.actor.set_width(width);
    }

    destroy() {
        this._closed = true;
        this.actor.destroy();
        this._current = null;
    }

    _labelAlign() {
        return this._align === 'center' ? Clutter.ActorAlign.CENTER : Clutter.ActorAlign.START;
    }

    setWidth(width) {
        this._width = width;
        this.actor.set_width(width);
        if (this._current)
            this._fitRow(this._current);   // 宽度变了要重排，不然双栏按旧宽度截断
    }

    // 'center' = 在「时钟 → 状态图标」这段空档里居中；'start' = 贴着左边起头
    setAlign(align) {
        if (this._align === align)
            return;
        this._align = align;
        for (const child of this.actor.get_children())
            child.x_align = this._labelAlign();
    }

    setVisible(visible) {
        this.actor.visible = visible;
    }

    setOptions({palette, showTranslation}) {
        if (palette)
            this._palette = palette;
        if (showTranslation !== undefined && showTranslation !== this._showTranslation) {
            this._showTranslation = showTranslation;
            this._redraw();
        }
    }

    _makeLabel(text, extraClass = '') {
        const label = new St.Label({
            text,
            style_class: extraClass ? `atri-lyric-label ${extraClass}` : 'atri-lyric-label',
            x_align: this._labelAlign(),
            y_align: Clutter.ActorAlign.CENTER,
            reactive: false,
        });
        // 单行 + 末尾省略号：宽度不够时自己截断，不挤到时钟/状态图标那边
        label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        label.clutter_text.single_line_mode = true;
        return label;
    }

    // 一行的内容：非中文歌是「左中文（翻译）+ 右原词」两栏，中文歌就一栏。
    // 两栏整个作为一个整体参与后面的上移淡出动画。
    _buildRow(line, translation) {
        const row = new St.BoxLayout({
            style_class: 'atri-lyric-row',
            x_align: this._labelAlign(),
            y_align: Clutter.ActorAlign.CENTER,
            reactive: false,
        });
        const cells = translation && line && translation !== line
            ? [[translation, 'atri-lyric-zh'], [line, 'atri-lyric-orig']]
            : [[line || translation, '']];

        row._labels = cells.map(([value, cls]) => {
            const label = this._makeLabel(value, cls);
            row.add_child(label);
            return label;
        });
        this._fitRow(row);
        return row;
    }

    // 一排内容超出可用宽度时按比例收窄（每栏各自出省略号），够宽就保持自然宽度
    _fitRow(row) {
        const labels = row?._labels ?? [];
        if (!labels.length)
            return;
        const content = Math.max(60, (this._width || 0) - 16);
        const natural = labels.map(label => label.get_preferred_width(-1)[1]);
        const total = natural.reduce((sum, value) => sum + value, 0);
        if (total <= content) {
            for (const label of labels)
                label.set_width(-1);
            return;
        }
        const scale = content / total;
        labels.forEach((label, index) => label.set_width(Math.max(28, Math.floor(natural[index] * scale))));
    }

    // 选项变了（比如切翻译）：拿当前这句重排，不播动画
    _redraw() {
        const line = this._text;
        const translation = this._translation;
        this._text = '';
        this._translation = '';
        this.setLine(line, translation);
        if (this.actor.mapped && this._pending)
            this._flush(true);
    }

    // 歌词换句：上句上移淡出，下句从下面慢慢升上来
    setLine(line, translation = '') {
        const want = (line ?? '').trim();
        const wantTranslation = (translation ?? '').trim();
        if (want === this._text && wantTranslation === this._translation)
            return;
        this._pending = [want, wantTranslation];
        // Shell 的 ease() 对还没 mapped 的控件会把时长压成 0（会"啪"地跳出来），
        // 所以没上屏之前先攒着，等它真的显示出来再放动画。
        if (this.actor.mapped) {
            this._flush();
        } else if (!this._mappedId) {
            this._mappedId = this.actor.connect('notify::mapped', () => {
                if (this.actor.mapped)
                    this._flush();
            });
        }
    }

    // instant = true 时不播动画（切换翻译这类重排用它，避免闪一下）
    _flush(instant = false) {
        const pending = this._pending;
        this._pending = null;
        if (pending === null)
            return;
        const [line, translation] = pending;
        this._text = line;
        this._translation = translation;

        const outgoing = this._current;
        this._current = null;
        if (outgoing) {
            if (instant) {
                outgoing.destroy();
            } else {
                const distance = Math.max(12, this.actor.height || 26);
                outgoing.ease({
                    translation_y: -Math.round(distance * 0.85),
                    opacity: 0,
                    duration: 360,
                    mode: Clutter.AnimationMode.EASE_IN_QUAD,
                    onComplete: () => outgoing.destroy(),
                });
            }
        }

        if (!line && !translation)
            return;

        const incoming = this._buildRow(line, this._showTranslation ? translation : '');
        incoming.opacity = 0;
        incoming.translation_y = Math.round(Math.max(12, this.actor.height || 26) * 0.95);
        this.actor.add_child(incoming);
        this._current = incoming;
        this._levelStep = -1;   // 强制下一帧刷新颜色
        this._applyColor(this._level);

        if (instant) {
            incoming.opacity = 255;
            incoming.translation_y = 0;
            return;
        }

        // 坑：Shell 的 ease() 对**还没 mapped** 的 actor 会把时长直接压成 0
        // （ui/environment.js: `if (!actor?.mapped) duration = 0;`），刚 add_child 的标签
        // 还没参加过一次布局，这时候 ease 就是"啪"地出现。等它真正上屏再动。
        const play = () => {
            if (this._closed)
                return GLib.SOURCE_REMOVE;
            if (incoming.get_parent() !== this.actor)
                return GLib.SOURCE_REMOVE;   // 已经被下一句顶掉了
            if (!incoming.mapped)
                return GLib.SOURCE_CONTINUE; // 还没上屏，再等一帧
            incoming.ease({
                translation_y: 0,
                opacity: 255,
                duration: 520,
                delay: 110,   // 等上句让开一点，再「慢慢上来」
                mode: Clutter.AnimationMode.EASE_OUT_CUBIC,
            });
            return GLib.SOURCE_REMOVE;
        };
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 16, play);
    }

    // 歌词颜色跟着整体响度微微染色（主要变色还是交给线条）
    setLevel(level) {
        this._level = clamp01(level);
        const step = Math.round(this._level * 12);
        if (step === this._levelStep)
            return;
        this._levelStep = step;
        this._applyColor(this._level);
    }

    _applyColor(level = 0) {
        if (!this._current)
            return;
        const [r, g, b] = paletteColor(this._palette, 0.55, level);
        const mix = 0.16;   // 只染一点点，保证歌词始终清楚
        const rr = Math.min(255, Math.round(255 * (0.74 + mix * r)));
        const gg = Math.min(255, Math.round(255 * (0.74 + mix * g)));
        const bb = Math.min(255, Math.round(255 * (0.74 + mix * b)));
        const css = `color: rgb(${rr}, ${gg}, ${bb});`;
        for (const label of this._current._labels ?? [])
            label.style = css;
    }
}

// ---------------------------------------------------------------- 顶栏组件 --

export default class AtriVisualizerExtension extends Extension {
    enable() {
        this._destroyed = false;
        this._config = readConfig();
        this._trail = [];
        this._frame = null;
        this._lastTs = -1;
        this._misses = 0;
        this._visible = false;
        this._fast = false;
        this._barsWidth = 0;
        this._lyricHome = 'right';
        this._lastBudget = 0;
        this._lastDebug = 0;
        this._debug = GLib.file_test(DEBUG_FLAG, GLib.FileTest.EXISTS);
        this._menu = null;

        this._buildBars();
        this._buildLyrics();
        this._insertBars();
        this._reparentLyrics(this._config.lyric_position === 'after-bars' ? 'left' : 'right');
        this._rebuildTrail();
        this._scheduleFast(false);
        this._updateBudget();

        if (this._config.autostart_daemon && !GLib.file_test(STATE_PATH, GLib.FileTest.EXISTS))
            this._startDaemon();

        this._log(`已启用（面板 ${Main.panel.width}x${Main.panel.height}）`);
        this._log(`状态文件 ${STATE_PATH}（可读=${GLib.file_test(STATE_PATH, GLib.FileTest.EXISTS)}）`);
    }

    disable() {
        this._destroyed = true;
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = null;
        }
        this._menu?.destroy();
        this._menu = null;
        this._lyrics?.destroy();
        this._lyrics = null;
        this._divider?.destroy();
        this._divider = null;
        this._bars?.destroy();
        this._bars = null;
        this._barRoot?.destroy();
        this._barRoot = null;
    }

    _log(message) {
        if (this._debug)
            log(`atri-visualizer: ${message}`);
    }

    // ------------------------------------------------------------------ 后台 --
    _startDaemon() {
        try {
            GLib.spawn_async(null, ['systemctl', '--user', 'start', SERVICE], null,
                GLib.SpawnFlags.SEARCH_PATH, null);
        } catch (e) {
            // 服务没装过：忽略，扩展会一直保持隐藏
        }
    }

    _restartDaemon() {
        try {
            GLib.spawn_async(null, ['systemctl', '--user', 'restart', SERVICE], null,
                GLib.SpawnFlags.SEARCH_PATH, null);
        } catch (e) {
            logError(e, 'atri-visualizer: 重启后台服务失败');
        }
    }

    // ------------------------------------------------------------------ 构建 --
    _buildBars() {
        this._barRoot = new St.BoxLayout({
            name: 'atri-visualizer',
            style_class: 'atri-visualizer',
            reactive: true,
            y_expand: true,
            x_expand: false,
        });

        this._bars = new St.DrawingArea({
            style_class: 'atri-bars',
            y_expand: true,
            x_expand: false,
        });
        this._bars.set_width(this._config.bars_width);
        this._bars.connect('repaint', area => this._onRepaint(area));
        this._barRoot.add_child(this._bars);

        this._divider = new St.Widget({
            style_class: 'atri-divider',
            y_expand: false,
            y_align: Clutter.ActorAlign.CENTER,
            x_expand: false,
        });
        this._barRoot.add_child(this._divider);

        this._barRoot.connect('button-press-event', (_actor, event) => this._onButtonPress(event));
    }

    _buildLyrics() {
        this._lyrics = new LyricView(this._config.lyric_width);
        this._lyrics.setOptions({
            palette: this._config.palette,
            showTranslation: this._config.show_translation,
        });
        this._applyConfig();
    }

    _applyConfig() {
        const cfg = this._config;
        this._setBarsWidth(cfg.bars_width);
        this._lyrics.setOptions({palette: cfg.palette, showTranslation: cfg.show_translation});
        this._lyrics.setWidth(cfg.lyric_width);
        this._lyrics.setVisible(cfg.show_lyrics);
        this._divider.visible = cfg.show_lyrics;
    }

    _setBarsWidth(width) {
        this._barsWidth = width;
        this._bars.set_width(width);
        if (width > 0)
            this._bars.queue_repaint();
    }

    // 线条区挂在顶栏左侧；歌词区挂在顶栏右侧（状态图标左边）
    _insertBars() {
        const panel = Main.panel;
        const leftBox = panel._leftBox ?? panel.statusArea?.activities?.container?.get_parent();
        if (!leftBox) {
            logError(new Error('找不到顶栏左侧 box'), 'atri-visualizer');
            this._barRoot.visible = false;
            return;
        }
        let index = leftBox.get_n_children();
        if (this._config.bars_position === 'leftmost') {
            const activities = panel.statusArea?.activities?.container;
            const activitiesIndex = activities ? leftBox.get_children().indexOf(activities) : -1;
            index = activitiesIndex >= 0 ? activitiesIndex + 1 : 0;
        }
        leftBox.insert_child_at_index(this._barRoot, index);
        this._barRoot.visible = false;
    }

    _reinsertBars() {
        this._barRoot.visible = false;
        this._barRoot.get_parent()?.remove_child(this._barRoot);
        this._insertBars();
        this._updateBudget();
    }

    // side: 'right' = 顶栏右侧状态图标左边；'left' = 紧挨线条（左区）
    _reparentLyrics(side) {
        if (this._lyricHome === side && this._lyrics.actor.get_parent())
            return;
        this._lyricHome = side;
        const target = side === 'right' ? Main.panel._rightBox : this._barRoot;
        if (!target)
            return;
        this._lyrics.actor.get_parent()?.remove_child(this._lyrics.actor);
        if (side === 'right')
            target.insert_child_at_index(this._lyrics.actor, 0);
        else
            target.add_child(this._lyrics.actor);
        this._lyrics.setAlign(side === 'right' ? 'center' : 'start');
    }

    _rebuildTrail() {
        const bands = this._frame?.bands;
        if (!Array.isArray(bands) || !bands.length)
            return;
        this._trail = Array.from({length: TRAIL_FRAMES}, () => bands.map(v => clamp01(v)));
    }

    // ------------------------------------------------------------------ 定时 --
    _scheduleFast(fast) {
        if (this._destroyed)
            return;
        if (this._fast === fast && this._timerId)
            return;
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = null;
        }
        this._fast = fast;
        this._timerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, fast ? FAST_MS : SLOW_MS, () => {
            this._tick();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _readState() {
        try {
            const [ok, bytes] = GLib.file_get_contents(STATE_PATH);
            if (!ok)
                return null;
            const state = JSON.parse(new TextDecoder().decode(bytes));
            const age = Date.now() / 1000 - (state.ts ?? 0);
            if (!Number.isFinite(age) || age > STALE_SEC)
                return null;   // 后台挂了：别把最后一帧冻在顶栏上
            return state;
        } catch (e) {
            return null;
        }
    }

    _tick() {
        if (this._destroyed)
            return;

        const state = this._readState();
        if (!state) {
            if (++this._misses >= HIDE_AFTER_MISSES) {
                this._setVisible(false);
                this._scheduleFast(false);
            }
            return;
        }
        this._misses = 0;

        const forced = GLib.file_test(FORCE_SHOW, GLib.FileTest.EXISTS);
        const show = !!(forced || state.visible);
        this._setVisible(show);
        this._scheduleFast(show);
        if (!show)
            return;

        if (state.ts === this._lastTs)
            return;   // 没有新一帧，省下重绘
        this._lastTs = state.ts;
        this._frame = state;

        const bands = Array.isArray(state.bands) ? state.bands : [];
        const fade = state.playing === false ? 0.35 : 1;   // 暂停时拖尾收一点
        this._trail.unshift(bands.map(v => clamp01(v) * fade));
        if (this._trail.length > TRAIL_FRAMES)
            this._trail.length = TRAIL_FRAMES;

        this._bars.queue_repaint();
        this._lyrics.setLine(state.line ?? '', state.translation ?? '');
        this._lyrics.setLevel(clamp01(state.level ?? 0));

        const now = GLib.get_monotonic_time() / 1000;
        if (this._debug && now - this._lastDebug > 1000) {
            this._lastDebug = now;
            this._debugLog(state);
        }
        if (now - this._lastBudget > BUDGET_MS) {
            this._lastBudget = now;
            this._updateBudget();
        }
    }

    // 开 ~/.cache/atri-visualizer/debug 后每秒往 journal 打一行：用来确认真的画出来了
    _debugLog(state) {
        try {
            const box = this._bars.get_allocation_box();
            const [widgetX] = this._barRoot.get_transformed_position();
            const [lyricX] = this._lyrics.actor.get_transformed_position();
            const anchor = Main.panel._centerBox;
            const [centerX] = anchor ? anchor.get_transformed_position() : [NaN];
            const centerW = anchor ? anchor.get_preferred_width(-1)[1] : 0;
            log(`atri-visualizer: 可见=${this._visible} 线区=${box.x2 - box.x1}x${box.y2 - box.y1}` +
                ` 线@${Math.round(widgetX)} 词@${Math.round(lyricX)}(宽${this._lyrics._width},${this._lyricHome})` +
                ` 时钟=${Math.round(centerX)}..${Math.round(centerX + centerW)}` +
                ` 响度=${state.level} 歌词="${state.line ?? ''}"`);
            if (this._updateBudget_trace)
                log(`atri-visualizer: 右区控件 ${this._updateBudget_trace}`);
        } catch (e) {
            logError(e, 'atri-visualizer: 调试输出失败');
        }
    }

    _setVisible(visible) {
        if (!this._barRoot || !this._lyrics)
            return;
        if (this._visible === visible)
            return;
        this._visible = visible;
        this._barRoot.visible = visible;
        if (visible) {
            this._updateBudget();
            this._bars.queue_repaint();
        }
    }

    // ------------------------------------------------------- 空间自适应布局 --
    //
    // 顶栏把屏幕横向切成「左 / 中(时钟) / 右」三块，各自的可用宽度是有数的：
    //   左区最右 = 时钟左边缘；右区最左 = 时钟右边缘往后、再避开状态图标那一坨。
    // 这里实时量一遍，把两个控件压进各自的预算里——宁可缩到看不见，也不压到时钟和状态。
    _updateBudget() {
        if (this._destroyed || !this._barRoot || !this._lyrics)
            return;
        try {
            const center = Main.panel._centerBox;
            const monitor = Main.layoutManager.findMonitorForActor(Main.panel) ??
                            Main.layoutManager.primaryMonitor;
            if (!center || !monitor)
                return;

            const [centerX] = center.get_transformed_position();
            const centerW = center.get_preferred_width(-1)[1];
            const clockRight = centerX + centerW;
            const screenRight = monitor.x + monitor.width;

            // 1) 线条：从自己在顶栏里的左边缘，量到时钟左边缘
            const [barsX] = this._barRoot.get_transformed_position();
            if (!Number.isFinite(barsX) || !Number.isFinite(centerX))
                return;
            const availBars = Math.floor(centerX - barsX - GAP);
            if (availBars < MIN_BARS_WIDTH) {
                this._setVisible(false);   // 连线条都放不下
                return;
            }

            const cfg = this._config;
            const barsWidth = Math.max(MIN_BARS_WIDTH, Math.min(cfg.bars_width, availBars));
            if (this._barsWidth !== barsWidth)
                this._setBarsWidth(barsWidth);

            // 2) 歌词：优先顶栏右侧（状态图标左边），放不下再退回线条后面
            let mode = cfg.show_lyrics ? cfg.lyric_position : 'off';
            if (mode !== 'after-bars')
                this._reparentLyrics('right');
            // 只用"跟自己的宽度无关"的量来算可用空档：
            //   sideWidth = 面板分给右侧的宽度 = 面板宽的一半（时钟居中，由 panel.js 的 vfunc_allocate 决定）
            //   iconsWidth = 状态图标那几坨的**已分配**宽度之和
            // 关键：lyricWidth ≤ sideWidth - iconsWidth，这样 box 的 natural 永远不超过分配宽度，
            // 图标绝不会被挤到屏幕外。（早先用"邻居的实际坐标"算会正反馈：块变宽→图标被挤出
            // 屏幕→量到的空档更大→块更宽，实测能涨到 3898px 把整个状态区顶没了。）
            const sideWidth = Math.max(0, Math.floor((monitor.width - centerW) / 2));
            const iconsWidth = this._iconsWidth(Main.panel._rightBox, this._lyrics.actor);
            const availRight = Math.max(0, Math.floor(sideWidth - iconsWidth - GAP * 2));
            const availLeft = Math.floor(availBars - barsWidth - DIVIDER_W - GAP);

            if (mode === 'auto')
                mode = availRight >= MIN_LYRIC_RIGHT ? 'right' : 'after-bars';

            let lyricWidth = 0;
            if (mode === 'right')
                // 右侧这栏铺满「时钟 → 状态图标」之间的空档，正文在空档里居中 ——
                // 也就是"以右侧到时间的中点为心"。超长的行在空档内省略，绝不碰状态图标。
                lyricWidth = Math.min(availRight, sideWidth);
            else if (mode === 'after-bars')
                lyricWidth = Math.min(cfg.lyric_width, availLeft);

            const floor = mode === 'right' ? MIN_LYRIC_RIGHT : MIN_LYRIC_LEFT;
            if (lyricWidth < floor)
                mode = 'off';

            if (mode === 'off') {
                this._lyrics.setVisible(false);
                this._divider.visible = false;
                return;
            }

            if (mode === 'right')
                this._reparentLyrics('right');
            else
                this._reparentLyrics('left');
            if (this._debug) {
                this._updateBudget_trace = Main.panel._rightBox.get_children().map(child => {
                    const [x] = child.get_transformed_position();
                    const [, width] = child.get_size();
                    const [, preferred] = child.get_preferred_width(-1);
                    const tag = child === this._lyrics.actor ? '[词]' : (child.get_name() || '·');
                    return `${tag}@${Math.round(x)} alloc=${Math.round(width)} pref=${Math.round(preferred)}`;
                }).join(' ');
            }
            if (this._lyrics._width !== lyricWidth)
                this._lyrics.setWidth(lyricWidth);
            this._lyrics.setVisible(true);
            this._divider.visible = mode === 'after-bars';
        } catch (e) {
            logError(e, 'atri-visualizer: 宽度预算计算失败');
        }
    }

    // 我们在右侧 box 里除自己之外的占用宽度（状态图标那一坨）——取**已分配**宽度之和。
    // 偏好宽度会虚高（实测能差 400px），实际位置又会随自己被撑宽而变（正反馈），
    // 只有"已分配宽度"既贴近真实观感、又跟我们的宽度无关。
    _iconsWidth(box, exclude) {
        if (!box)
            return 0;
        let width = 0;
        for (const child of box.get_children()) {
            if (child === exclude)
                continue;
            const [childWidth] = child.get_size();
            if (Number.isFinite(childWidth) && childWidth > 0)
                width += childWidth;
        }
        return width;
    }

    // ------------------------------------------------------------------ 绘制 --
    _onRepaint(area) {
        const ctx = area.get_context();
        if (!ctx)
            return;
        try {
            const box = area.get_allocation_box();
            const width = box.x2 - box.x1;
            const height = box.y2 - box.y1;
            renderBars(ctx, width, height,
                {bands: this._frame?.bands ?? [], level: clamp01(this._frame?.level ?? 0)},
                {trail: this._trail, palette: this._config.palette, glow: true,
                 gamma: 1 / (this._config.sensitivity || 1)});
        } catch (e) {
            logError(e, 'atri-visualizer: 绘制失败');
        } finally {
            try {
                ctx.$dispose();
            } catch (e) {
                // 老版本 GJS 没这个方法，忽略
            }
        }
    }

    // ------------------------------------------------------------------ 菜单 --
    _onButtonPress(event) {
        const button = event.get_button();
        const ctrl = event.get_state() & Clutter.ModifierType.CONTROL_MASK;
        if (button !== 3 && !(button === 1 && ctrl))
            return Clutter.EVENT_PROPAGATE;
        this._menu?.destroy();
        this._menu = this._buildMenu();
        this._menu.open();
        return Clutter.EVENT_STOP;
    }

    _buildMenu() {
        const menu = new PopupMenu.PopupMenu(this._barRoot, 0.5, St.Side.BOTTOM);
        Main.uiGroup.add_child(menu.actor);

        const add = (label, callback) => {
            const item = new PopupMenu.PopupMenuItem(label);
            item.connect('activate', () => {
                callback();
                this._menu?.close();
            });
            menu.addMenuItem(item);
            return item;
        };
        const persist = () => {
            writeConfig(this._config);
            this._applyConfig();
            this._updateBudget();
        };
        const lyricLabel = {auto: '自动', right: '顶栏右侧', 'after-bars': '线条右边'};

        add(`${this._config.show_lyrics ? '隐藏' : '显示'}歌词`, () => {
            this._config.show_lyrics = !this._config.show_lyrics;
            this._lyrics.setLine('', '');
            persist();
        });
        add(`歌词位置：${lyricLabel[this._config.lyric_position] ?? this._config.lyric_position}`, () => {
            const index = LYRIC_POSITIONS.indexOf(this._config.lyric_position);
            this._config.lyric_position = LYRIC_POSITIONS[(index + 1) % LYRIC_POSITIONS.length];
            persist();
        });
        add(`${this._config.show_translation ? '不显示' : '显示'}翻译`, () => {
            this._config.show_translation = !this._config.show_translation;
            this._lyrics.setLine('', '');
            persist();
        });
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        add(`调色板：${PALETTES[this._config.palette]?.label ?? this._config.palette}`, () => {
            const index = PALETTE_ORDER.indexOf(this._config.palette);
            this._config.palette = PALETTE_ORDER[(index + 1) % PALETTE_ORDER.length];
            persist();
        });
        add(`线条宽度：${this._config.bars_width}px`, () => {
            const index = BARS_WIDTHS.indexOf(this._config.bars_width);
            this._config.bars_width = BARS_WIDTHS[(index + 1) % BARS_WIDTHS.length];
            persist();
        });
        add(`灵敏度：${SENSITIVITY_LABELS[this._config.sensitivity] ?? this._config.sensitivity}`, () => {
            const index = SENSITIVITIES.indexOf(this._config.sensitivity);
            this._config.sensitivity = SENSITIVITIES[(index + 1) % SENSITIVITIES.length];
            persist();
            this._bars.queue_repaint();
        });
        add(`歌词宽度：${this._config.lyric_width}px`, () => {
            const index = LYRIC_WIDTHS.indexOf(this._config.lyric_width);
            this._config.lyric_width = LYRIC_WIDTHS[(index + 1) % LYRIC_WIDTHS.length];
            persist();
        });
        add(`线条位置：${this._config.bars_position === 'leftmost' ? '最左' : '面板末'}`, () => {
            this._config.bars_position = this._config.bars_position === 'leftmost' ? 'end' : 'leftmost';
            writeConfig(this._config);
            this._reinsertBars();
        });
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        for (const [label, step] of [['歌词慢一点 (+0.2s)', 200], ['歌词快一点 (-0.2s)', -200]]) {
            add(`${label}　当前 ${(this._config.offset_ms / 1000).toFixed(1)}s`, () => {
                this._config.offset_ms = Math.max(-2000, Math.min(2000, this._config.offset_ms + step));
                writeConfig(this._config);
            });
        }
        add('歌词延迟归零', () => {
            this._config.offset_ms = 0;
            writeConfig(this._config);
        });
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        add('重启后台服务', () => this._restartDaemon());
        return menu;
    }
}
