#!/usr/bin/env bash
# 无头冒烟测试：在**隔离**的 GNOME Shell 里真加载扩展、真画顶栏、真截图。
#
#   bash tools/smoke/smoke.sh              # 跑到完，产出在 /tmp/viz-smoke.<随机>
#   VIZ_SMOKE_KEEP=1 bash tools/smoke/smoke.sh
#
# 隔离手段：独立的 XDG_DATA_HOME（只放我们自己的扩展，不碰真实会话）、
# 内存版 GSettings、独立的 XDG_RUNTIME_DIR（状态文件写这儿，真实会话不受影响）。
# 用的是 `gnome-shell --headless --virtual-monitor`，所以**不会**动 Maus 的真实桌面。
set -u

VIZ_SMOKE_DIR="${VIZ_SMOKE_DIR:-$(mktemp -d /tmp/viz-smoke.XXXXXX)}"
export VIZ_SMOKE_DIR

# 必须跑在**私有** D-Bus 会话里，否则嵌套 shell 会因为 org.gnome.Shell 已被真实会话占用而退出。
if [[ -z "${VIZ_SMOKE_INNER:-}" ]]; then
    export VIZ_SMOKE_INNER=1
    echo "==> 切到私有 D-Bus 会话（真实会话不受影响）"
    exec dbus-run-session -- bash "$0" "$@"
fi

PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
EXT_SRC="${VIZ_EXT_DIR:-$HOME/.local/share/gnome-shell/extensions/netease-viz@maus.local}"
UUID="netease-viz@maus.local"
SHOT_UUID="viz-shot@maus.local"
ROOT="$VIZ_SMOKE_DIR"

DATA="$ROOT/data"
mkdir -p "$DATA/gnome-shell/extensions" "$ROOT/runtime" "$ROOT/cache" "$ROOT/config" "$ROOT/shots"
chmod 700 "$ROOT/runtime"

# 关键：XDG_DATA_HOME 一换，~/.local/share/fonts 里的用户字体（萝莉体）就看不见了，
# 截图会变成默认字体，白测一场。把字体目录软链进隔离环境。
if [[ -d "$HOME/.local/share/fonts" ]]; then
    ln -sfn "$HOME/.local/share/fonts" "$DATA/fonts"
fi

cp -rT "$EXT_SRC" "$DATA/gnome-shell/extensions/$UUID"
cp -rT "$PROJECT/tools/smoke/$SHOT_UUID" "$DATA/gnome-shell/extensions/$SHOT_UUID"

export XDG_DATA_HOME="$DATA"
export XDG_CACHE_HOME="$ROOT/cache"
export XDG_CONFIG_HOME="$ROOT/config"
export VIZ_SMOKE_DIR="$ROOT/shots"
export GSETTINGS_BACKEND=memory
# VIZ_REAL_RUNTIME=1：不隔离运行目录，直接读**真**后台服务写的 state.json，
# 用来验证「真音频 → 真后台 → 顶栏」整条链路（这时不启假数据源）。
if [[ "${VIZ_REAL_RUNTIME:-0}" == "1" ]]; then
    echo "==> 使用真实 state.json（真后台 + 真音频）"
else
    export XDG_RUNTIME_DIR="$ROOT/runtime"
fi
# 让扩展每秒往日志打一行几何信息
mkdir -p "$ROOT/cache/netease-viz"
touch "$ROOT/cache/netease-viz/debug"

echo "==> 冒烟目录：$ROOT"
if [[ "${VIZ_REAL_RUNTIME:-0}" == "1" ]]; then
    PUBLISHER=""
else
    echo "==> 状态源：假数据，每 2 秒换一句"
    python3 "$PROJECT/tools/preview_state.py" --fps 60 --line-seconds 2.0 \
        > "$ROOT/publisher.log" 2>&1 &
    PUBLISHER=$!
    sleep 0.4
fi

echo "==> 起无头 gnome-shell（1920x1080 虚拟显示器）"
gnome-shell --headless --virtual-monitor 1920x1080 > "$ROOT/shell.log" 2>&1 &
SHELL_PID=$!

cleanup() {
    kill "$SHELL_PID" 2>/dev/null
    [[ -n "${PUBLISHER:-}" ]] && kill "$PUBLISHER" 2>/dev/null
    wait "$SHELL_PID" 2>/dev/null
    [[ -n "${PUBLISHER:-}" ]] && wait "$PUBLISHER" 2>/dev/null
}
trap cleanup EXIT

echo "==> 等 Shell 初始化（14s）"
sleep 14

echo "==> 扫描到的扩展"
gnome-extensions list 2>&1 | grep -E "netease-viz|viz-shot" || echo "  (没扫到！)"

echo "==> 启用"
gnome-extensions enable "$UUID" 2>&1 | tail -2
gnome-extensions enable "$SHOT_UUID" 2>&1 | tail -2

echo "==> 等截图与动画采样（8s）"
sleep 8

echo
echo "=== 扩展状态 ==="
gnome-extensions info "$UUID" 2>&1 | grep -iE "State|Enabled|Error" | head -5

echo
echo "=== Shell 日志里的扩展消息 / 报错 ==="
grep -iE "netease-viz|JS ERROR|CRITICAL|Extension .*error" "$ROOT/shell.log" | tail -25

echo
echo "=== 几何报告（最后 60 行）==="
tail -60 "$ROOT/shots/report.txt" 2>/dev/null

echo
echo "=== 产出 ==="
ls -la "$ROOT/shots" 2>/dev/null
echo "冒烟目录：$ROOT"
