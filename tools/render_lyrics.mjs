// 离屏预览：用和 stylesheet.css 相同的字体/字号把歌词画到 PNG，检查萝莉体的观感。
// 用法：gjs -m tools/render_lyrics.mjs [输出路径]

import cairo from 'gi://cairo';
import Pango from 'gi://Pango';
import PangoCairo from 'gi://PangoCairo';

const OUT = ARGV[0] ?? '/tmp/atri-lyrics-preview.png';
const PANEL_W = 300;   // 歌词区宽度（面板像素）
const PANEL_H = 28;    // 顶栏高度
const ZOOM = 4;
const LINES = [
    '一群嗜血的蚂蚁 被腐肉所吸引',
    '我面无表情 看孤独的风景',
    '失去你 爱恨开始分明',
    'あの日の言叶 まだ覚えてる',
    '哼哼，我可是高性能的嘛！',
];

const surface = new cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                       PANEL_W * ZOOM,
                                       PANEL_H * ZOOM * LINES.length);
const ctx = new cairo.Context(surface);

for (let i = 0; i < LINES.length; i++) {
    const top = i * PANEL_H * ZOOM;
    ctx.save();
    ctx.translate(0, top);
    ctx.setSourceRGBA(0.11, 0.11, 0.14, 1.0);
    ctx.rectangle(0, 0, PANEL_W * ZOOM, PANEL_H * ZOOM);
    ctx.fill();
    ctx.scale(ZOOM, ZOOM);

    const layout = PangoCairo.create_layout(ctx);
    const desc = Pango.FontDescription.from_string('萝莉体 第二版 15px');
    layout.set_font_description(desc);
    layout.set_text(LINES[i], -1);
    layout.set_width(PANEL_W * Pango.SCALE);
    layout.set_ellipsize(Pango.EllipsizeMode.END);
    layout.set_single_paragraph_mode(true);
    ctx.setSourceRGBA(0.96, 0.96, 0.98, 1.0);
    ctx.moveTo(2, (PANEL_H - 15) / 2 + 1);
    PangoCairo.show_layout(ctx, layout);
    ctx.restore();
}

surface.writeToPNG(OUT);
ctx.$dispose();
print(`已写出 ${OUT}（${surface.getWidth()}x${surface.getHeight()}）`);
