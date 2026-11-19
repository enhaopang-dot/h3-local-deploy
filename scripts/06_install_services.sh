#!/usr/bin/env bash
# 06_install_services.sh - 注册成 systemd 用户服务（开机自启、崩了自动拉）
# 用法: bash scripts/06_install_services.sh [--uninstall]
set -euo pipefail

ROOT="${H3_ROOT:-$HOME/AI/minimax-h3}"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl --user disable --now comfyui-h3.service h3-panel.service 2>/dev/null || true
  rm -f "$UNIT_DIR/comfyui-h3.service" "$UNIT_DIR/h3-panel.service"
  systemctl --user daemon-reload
  echo "已卸载 comfyui-h3 / h3-panel 用户服务"
  exit 0
fi

cat > "$UNIT_DIR/comfyui-h3.service" <<EOF
[Unit]
Description=ComfyUI - MiniMax H3
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=H3_ROOT=$ROOT
Environment=IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg
ExecStart=/bin/bash $ROOT/scripts/03_start_comfyui.sh --log
Restart=on-failure
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/h3-panel.service" <<EOF
[Unit]
Description=H3 Video Panel (8199)
After=comfyui-h3.service

[Service]
Type=simple
WorkingDirectory=$ROOT/panel
Environment=H3_COMFY_ROOT=$ROOT/ComfyUI
Environment=H3_COMFY_URL=http://127.0.0.1:8188
Environment=H3_SERVER_LOG=$ROOT/comfyui.log
Environment=H3_PYTHON=$ROOT/.venv/bin/python
ExecStart=$ROOT/.venv/bin/python $ROOT/panel/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now comfyui-h3.service
sleep 2
systemctl --user enable --now h3-panel.service

# 没有 linger 的话，SSH 退出后用户服务会被回收
if command -v loginctl >/dev/null 2>&1; then
  sudo loginctl enable-linger "$USER" || echo "  [WARN] enable-linger 失败，重启后服务可能不自启"
fi

echo
echo "已注册："
systemctl --user --no-pager --lines=0 status comfyui-h3.service | head -5 || true
systemctl --user --no-pager --lines=0 status h3-panel.service | head -5 || true
echo
echo "常用命令："
echo "  systemctl --user restart comfyui-h3   # 重启出片服务"
echo "  systemctl --user stop h3-panel        # 停面板"
echo "  journalctl --user -u comfyui-h3 -f    # 看日志"
echo "  bash scripts/06_install_services.sh --uninstall  # 卸载"
