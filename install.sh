#!/usr/bin/env bash
# 安装 / 更新 网易云顶栏可视化（幂等，可反复跑）。
#
#   ./install.sh                 部署扩展 + 装服务 + 重启后台 + 预热启用列表
#   ./install.sh --no-autostart  同上，但不设成随图形会话自启
#
# 只动 ~/.local 和 ~/.config 下的用户文件，不碰系统目录、不下载任何东西。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UUID="netease-viz@maus.local"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
UNIT_DST="$HOME/.config/systemd/user/netease-viz.service"

echo "==> 部署扩展 → $EXT_DIR"
mkdir -p "$EXT_DIR"
rsync -a --delete "$HERE/extension/" "$EXT_DIR/"
chmod +x "$HERE/vizd.py" "$HERE"/tools/*.py "$HERE"/tools/smoke/smoke.sh 2>/dev/null || true

echo "==> 安装 systemd --user 服务"
mkdir -p "$(dirname "$UNIT_DST")"
cp "$HERE/systemd/netease-viz.service" "$UNIT_DST"
systemctl --user daemon-reload
systemctl --user restart netease-viz.service
if [[ "${1:-}" != "--no-autostart" ]]; then
    systemctl --user enable netease-viz.service >/dev/null
    echo "    已设为随图形会话自启（想取消：systemctl --user disable netease-viz）"
fi

echo "==> 预热 GNOME 扩展启用列表"
python3 - "$UUID" <<'PY'
import subprocess, sys
uuid = sys.argv[1]
current = subprocess.run(['gsettings', 'get', 'org.gnome.shell', 'enabled-extensions'],
                         capture_output=True, text=True).stdout.strip()
items = [x.strip().strip("'") for x in current.strip('[]').split(',') if x.strip()]
if uuid in items:
    print(f'    {uuid} 已在启用列表')
else:
    items.append(uuid)
    subprocess.run(['gsettings', 'set', 'org.gnome.shell', 'enabled-extensions',
                    '[' + ', '.join(f"'{i}'" for i in items) + ']'], check=True)
    print(f'    {uuid} 已加入启用列表')
PY

if gnome-extensions list 2>/dev/null | grep -qx "$UUID"; then
    gnome-extensions disable "$UUID" 2>/dev/null || true
    gnome-extensions enable "$UUID" 2>/dev/null || true
    echo "==> 已重载扩展"
else
    echo "==> 本会话还没扫到这个扩展目录：注销重登一次即可生效"
    echo "    （GNOME 只在登录时扫描扩展，Wayland 下没法热重启 shell）"
fi

echo
systemctl --user --no-pager status netease-viz.service | head -5
