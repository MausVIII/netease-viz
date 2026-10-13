// 把冒烟截图裁一块放大，用来肉眼检查顶栏细节。
// 用法：gjs -m tools/crop.mjs <输入.png> <x> <y> <宽> <高> <倍数> <输出.png>

import cairo from 'gi://cairo';
import system from 'system';

const [src, x, y, w, h, scale, dst] = ARGV;
if (!dst) {
    printerr('用法：gjs -m tools/crop.mjs <输入.png> <x> <y> <宽> <高> <倍数> <输出.png>');
    system.exit(1);
}

const surface = cairo.ImageSurface.createFromPNG(src);
const sx = Number(x), sy = Number(y), sw = Number(w), sh = Number(h), k = Number(scale);
const out = new cairo.ImageSurface(cairo.FORMAT_ARGB32, Math.round(sw * k), Math.round(sh * k));
const ctx = new cairo.Context(out);
ctx.scale(k, k);
ctx.setSourceSurface(surface, -sx, -sy);
ctx.paint();
out.writeToPNG(dst);
ctx.$dispose();
print(`${dst} ← ${src} (${sw}x${sh} @ ${k}x)`);
