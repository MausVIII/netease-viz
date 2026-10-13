import cairo from 'gi://cairo';

// ATRI 顶栏可视化 —— 线条绘制核心
//
// 这里是唯一的"画"的地方：扩展在顶栏里调用它，tools/render_preview.js 也在
// 离屏画布上调用它，两边共用同一段代码，改一处两边都变（DRY）。
//
// 设计：
//   · 每条线以顶栏竖直中线为锚，向上、向下对称生长 —— 就是"上下跳动的线条"
//   · 线的颜色沿调色板随振幅**线性**插值（色相线性过渡）
//   · 动态模糊用两层叠加：先按时间倒序把前几帧的线形淡画一遍（拖尾），
//     再用竖直渐变的笔触画当前帧（两端柔化），合起来就是运动模糊的观感

// 调色板：hueA → hueB 线性扫过，振幅越大越靠 hueB
export const PALETTES = {
    aqua: {label: '青 → 粉', hueA: 186, hueB: 322, sat: 0.88, val: 0.98},
    rainbow: {label: '彩虹', hueA: 200, hueB: 0, sat: 0.92, val: 1.0},
    ice: {label: '冰蓝', hueA: 208, hueB: 168, sat: 0.72, val: 1.0},
    sakura: {label: '樱 → 紫', hueA: 340, hueB: 278, sat: 0.82, val: 1.0},
};
export const PALETTE_ORDER = ['aqua', 'rainbow', 'ice', 'sakura'];

export function clamp01(v) {
    return v < 0 ? 0 : v > 1 ? 1 : v;
}

// h: 0..360, s/v: 0..1 → [r, g, b] 0..1
export function hsv2rgb(h, s, v) {
    h = (((h % 360) + 360) % 360) / 60;
    const c = v * s;
    const x = c * (1 - Math.abs((h % 2) - 1));
    const m = v - c;
    let rgb;
    if (h < 1)
        rgb = [c, x, 0];
    else if (h < 2)
        rgb = [x, c, 0];
    else if (h < 3)
        rgb = [0, c, x];
    else if (h < 4)
        rgb = [0, x, c];
    else if (h < 5)
        rgb = [x, 0, c];
    else
        rgb = [c, 0, x];
    return [rgb[0] + m, rgb[1] + m, rgb[2] + m];
}

// 给定调色板与线性位置 t(0..1)，返回 [r,g,b]
export function paletteColor(paletteName, t, level = 0) {
    const p = PALETTES[paletteName] ?? PALETTES.aqua;
    t = clamp01(t);
    const hue = p.hueA + (p.hueB - p.hueA) * t;
    // 整体响度点亮一点饱和度与明度，音乐一响颜色就"活"过来
    const sat = clamp01(p.sat * (0.78 + 0.22 * level));
    const val = clamp01(p.val * (0.74 + 0.26 * clamp01(level * 1.25)));
    return hsv2rgb(hue, sat, val);
}

// 几何参数：所有尺寸都从实际分配到的宽高推出来，主题/缩放变了也不会错位
function geometry(w, h, count) {
    const padX = Math.max(2, Math.round(w * 0.03));
    const padY = Math.max(2, Math.round(h * 0.16));
    const gap = Math.max(1, Math.min(3, Math.floor(w / (count * 8))));
    const barW = Math.max(1.4, (w - padX * 2 - gap * (count - 1)) / count);
    const maxLen = Math.max(4, h - padY * 2);
    const minLen = Math.max(2, maxLen * 0.12);
    return {padX, gap, barW, maxLen, minLen};
}

function drawLine(ctx, x, half, barW, rgb, alpha, softEnds) {
    if (half <= 0 || alpha <= 0.004)
        return;
    const [r, g, b] = rgb;
    ctx.save();
    ctx.setLineWidth(barW);
    ctx.setLineCap(cairo.LineCap.ROUND);
    if (softEnds) {
        // 两端柔化：竖直渐变让线尖慢慢化开，就是"运动模糊"的柔软感
        const grad = new cairo.LinearGradient(x, -half, x, half);
        grad.addColorStopRGBA(0, r, g, b, 0);
        grad.addColorStopRGBA(0.32, r, g, b, alpha * 0.85);
        grad.addColorStopRGBA(0.68, r, g, b, alpha * 0.85);
        grad.addColorStopRGBA(1, r, g, b, 0);
        ctx.setSource(grad);
    } else {
        ctx.setSourceRGBA(r, g, b, alpha);
    }
    ctx.moveTo(x, -half);
    ctx.lineTo(x, half);
    ctx.stroke();
    ctx.restore();
}

// 核心入口
//   ctx    : cairo.Context（由 St.DrawingArea 的 repaint 信号给出）
//   w, h   : 实际分配到的宽高
//   frame  : {bands: number[], level: number}
//   opts   : {trail: number[][], palette: string, glow: number}
//            trail[0] 是上一帧、trail[1] 是上上帧……（越靠后越老）
export function renderBars(ctx, w, h, frame, opts = {}) {
    const bands = frame?.bands ?? [];
    if (!bands.length || w <= 0 || h <= 0)
        return;

    const n = bands.length;
    const {trail = [], palette = 'aqua', glow = true, gamma = 1} = opts;
    const level = clamp01(frame?.level ?? 0);
    const {padX, gap, barW, maxLen, minLen} = geometry(w, h, n);
    const cy = h / 2;
    const step = barW + gap;
    // gamma < 1 更灵敏、> 1 更沉稳（就是菜单里的"灵敏度"）
    const shape = v => Math.pow(clamp01(v), gamma);
    const halfOf = v => (minLen + shape(v) * (maxLen - minLen)) / 2;

    ctx.save();
    ctx.translate(0, cy); // 之后所有 y 都相对中线，正负对称 = 上下跳动
    ctx.setOperator(cairo.Operator.OVER);

    // 1) 拖尾：老帧先画、新帧后画，透明度按帧龄二次衰减
    const depth = trail.length;
    for (let age = depth - 1; age >= 0; age--) {
        const ghost = trail[age];
        if (!ghost || ghost.length !== n)
            continue;
        const freshness = 1 - age / (depth + 1);
        const alpha = 0.30 * freshness * freshness * (0.45 + 0.55 * level);
        for (let i = 0; i < n; i++) {
            const value = clamp01(ghost[i]);
            const x = padX + i * step + barW / 2;
            const rgb = paletteColor(palette, value, level);
            drawLine(ctx, x, halfOf(value), barW * (0.86 + 0.14 * freshness), rgb, alpha, false);
        }
    }

    // 2) 当前帧：外圈微光 + 渐变笔触本体
    for (let i = 0; i < n; i++) {
        const value = clamp01(bands[i]);
        const x = padX + i * step + barW / 2;
        const half = halfOf(value);
        const rgb = paletteColor(palette, value, level);
        if (glow && value > 0.06)
            drawLine(ctx, x, half * 1.06, barW * 2.6, rgb, 0.13 * (0.35 + 0.65 * value), false);
        drawLine(ctx, x, half, barW, rgb, 0.55 + 0.45 * value, true);
    }

    ctx.restore();
}
