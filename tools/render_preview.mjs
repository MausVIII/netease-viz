// 离屏预览：把顶栏线条画到 PNG 上，用来肉眼检查画法（会话里截图被系统禁掉了）。
//
// 用法：gjs tools/render_preview.mjs [输出路径] [调色板]
// 依赖扩展目录里的 bars.js —— 与线上是同一段绘制代码。

import cairo from 'gi://cairo';
import GLib from 'gi://GLib';
import system from 'system';

import {renderBars, PALETTE_ORDER} from
    'file:///home/maus/.local/share/gnome-shell/extensions/atri-visualizer@maus.local/bars.js';

const PANEL_W = 236;   // 线条区宽度（面板像素）
const PANEL_H = 28;    // 顶栏高度（面板像素）
const ZOOM = 5;        // 放大倍数，方便看清动态模糊
const GAP = 6;         // 格子间距（面板像素）
const N_BANDS = 48;
const TRAIL = 7;

// 造一段"像音乐"的频段序列：低频有鼓点、中频有旋律、高频有沙沙的镲
function synthFrame(t) {
    const beat = Math.pow(Math.max(0, (t % 30) / 30 - 0.0), 0); // 0..1 每 30 帧一次
    const kick = Math.pow(1 - beat, 3);
    const bands = [];
    for (let i = 0; i < N_BANDS; i++) {
        const f = i / (N_BANDS - 1);
        const bass = kick * Math.exp(-f * 6.5);
        const mid = 0.45 * Math.exp(-Math.pow((f - 0.42) * 3.2, 2)) *
                    (0.5 + 0.5 * Math.sin(t * 0.21 + f * 7));
        const air = 0.30 * Math.pow(f, 1.6) * (0.4 + 0.6 * Math.abs(Math.sin(t * 0.09 + f * 13)));
        bands.push(Math.max(0.02, Math.min(1, bass + mid + air)));
    }
    return bands;
}

function makeSurface() {
    return new cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                  PANEL_W * ZOOM, PANEL_H * ZOOM);
}

function paintPanel(bands, trail, palette) {
    const surface = makeSurface();
    const ctx = new cairo.Context(surface);
    // 顶栏底色（GNOME 的 #panel 近似值）
    ctx.setSourceRGBA(0.11, 0.11, 0.14, 1.0);
    ctx.paint();
    ctx.scale(ZOOM, ZOOM);
    renderBars(ctx, PANEL_W, PANEL_H, {bands, level: 0.65},
               {trail, palette, glow: true});
    ctx.$dispose();
    return surface;
}

const outPath = ARGV[0] ?? '/tmp/atri-bars-preview.png';
const palette = ARGV[1] ?? 'aqua';
if (!PALETTE_ORDER.includes(palette))
    log(`未知调色板 ${palette}，用 aqua`);

// 生成 60 帧，取 6 帧（鼓点前后）做对比
const history = [];
const picks = [];
for (let t = 0; t < 60; t++) {
    const bands = synthFrame(t);
    history.unshift(bands);
    if (history.length > TRAIL + 1)
        history.length = TRAIL + 1;
    if (t >= 40 && t <= 45)
        picks.push({bands, trail: history.slice(1)});
}

const cols = 3;
const rows = Math.ceil(picks.length / cols);
const cellW = PANEL_W * ZOOM + GAP * ZOOM;
const cellH = PANEL_H * ZOOM + GAP * ZOOM;
const sheet = new cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                     cols * cellW + GAP * ZOOM,
                                     rows * cellH + GAP * ZOOM);
const sheetCtx = new cairo.Context(sheet);
sheetCtx.setSourceRGBA(0.04, 0.04, 0.06, 1.0);
sheetCtx.paint();

picks.forEach(({bands, trail}, index) => {
    const col = index % cols;
    const row = Math.floor(index / cols);
    const surface = paintPanel(bands, trail, palette);
    sheetCtx.setSourceSurface(surface, GAP * ZOOM + col * cellW, GAP * ZOOM + row * cellH);
    sheetCtx.paint();
});

sheet.writeToPNG(outPath);
sheetCtx.$dispose();
print(`已写出 ${outPath}（${sheet.getWidth()}x${sheet.getHeight()}，调色板 ${palette}）`);
