// 冒烟测试专用扩展：在无头 GNOME Shell 里截图顶栏 + 写几何报告。
//
// 用进程内的 Shell.Screenshot（走 D-Bus 会被权限白名单拦掉，进程内不会），
// 把面板左/中/右三个 box 和我们的两个控件的位置尺寸写进 $VIZ_SMOKE_DIR/report.txt。
// 只在 smoke.sh 造的隔离会话里启用，不会进真实会话。

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

const OUT_DIR = GLib.getenv('VIZ_SMOKE_DIR') ?? '/tmp/viz-smoke';

function append(text) {
    GLib.mkdir_with_parents(OUT_DIR, 0o755);
    const stream = Gio.File.new_for_path(`${OUT_DIR}/report.txt`)
        .append_to(Gio.FileCreateFlags.NONE, null);
    stream.write_all(new TextEncoder().encode(text), null);
    stream.close(null);
}

function describe(actor) {
    const [x, y] = actor.get_transformed_position();
    const [w, h] = actor.get_size();
    return `    ${(actor.get_name() || '(匿名)').padEnd(20)} ${actor.constructor.$gtype.name.padEnd(16)}` +
        ` @(${x.toFixed(0)},${y.toFixed(0)}) ${w.toFixed(0)}x${h.toFixed(0)}` +
        ` vis=${actor.visible} op=${actor.opacity}`;
}

function findByName(actor, name) {
    if (actor.get_name?.() === name)
        return actor;
    for (const child of actor.get_children?.() ?? []) {
        const hit = findByName(child, name);
        if (hit)
            return hit;
    }
    return null;
}

export default class VizShotExtension extends Extension {
    enable() {
        this._timers = [];
        const schedule = [
            [1200, 'strip'], [1500, 'strip'], [1800, 'strip'], [2100, 'strip'],
            [2400, 'strip'], [2700, 'strip'], [3000, 'strip'], [3300, 'strip'],
            [3800, 'full'], [4400, 'full'],
        ];
        let index = 0;
        for (const [delay, kind] of schedule) {
            index += 1;
            const n = index;
            this._timers.push(GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
                this._shoot(n, kind);
                return GLib.SOURCE_REMOVE;
            }));
        }
        append(`atr-shot 已启用，stage ${global.stage.width}x${global.stage.height}\n`);
    }

    disable() {
        for (const id of this._timers)
            GLib.source_remove(id);
        this._timers = [];
    }

    _report(tag) {
        const monitor = Main.layoutManager.primaryMonitor;
        const lines = [`=== ${tag} @ ${new Date().toISOString()} ===`,
            `monitor ${monitor.width}x${monitor.height}  panelBox.h=${Main.layoutManager.panelBox.height}`];
        const boxes = [['left', Main.panel._leftBox], ['center', Main.panel._centerBox],
            ['right', Main.panel._rightBox]];
        for (const [name, box] of boxes) {
            const [x, y] = box.get_transformed_position();
            const [w, h] = box.get_size();
            lines.push(`  ${name}Box @(${x.toFixed(0)},${y.toFixed(0)}) ${w.toFixed(0)}x${h.toFixed(0)}`);
            for (const child of box.get_children())
                lines.push(describe(child));
        }
        for (const name of ['netease-viz', 'nv-lyric-view']) {
            const actor = findByName(Main.panel, name);
            lines.push(`  [${name}] ${actor ? '' : '未找到'}`);
            if (actor) {
                lines.push(describe(actor));
                for (const child of actor.get_children()) {
                    const text = child.get_text?.();
                    if (text === undefined)
                        continue;
                    lines.push(`      词="${text}" trans=${JSON.stringify(child.get_translation())}` +
                        ` op=${child.opacity} vis=${child.visible}`);
                }
            }
        }
        append(`${lines.join('\n')}\n`);
        return lines;
    }

    _shoot(index, kind) {
        const monitor = Main.layoutManager.primaryMonitor;
        this._report(`${kind}-${index}`);
        const width = monitor.width;
        const height = kind === 'full' ? Math.min(monitor.height, 240) : 44;
        const path = `${OUT_DIR}/${kind}-${index}.png`;
        let stream;
        try {
            stream = Gio.File.new_for_path(path)
                .replace(null, false, Gio.FileCreateFlags.REPLACE_DESTINATION, null);
        } catch (e) {
            append(`  !! 无法写 ${path}: ${e.message}\n`);
            return;
        }
        const shot = new Shell.Screenshot();
        shot.screenshot_area(0, 0, width, height, stream, null, (source, res) => {
            try {
                source.screenshot_area_finish(res);
                append(`  截图 -> ${path}\n`);
            } catch (e) {
                append(`  !! 截图失败 ${path}: ${e.message}\n`);
            }
        });
    }
}
