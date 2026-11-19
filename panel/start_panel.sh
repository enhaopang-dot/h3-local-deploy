#!/usr/bin/env bash
# start_panel.sh - 启动网页面板（8199）
# 前置：ComfyUI 已在 8188 跑；本脚本只管面板
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${H3_ROOT:-$(dirname "$HERE")}"
export H3_COMFY_ROOT="${H3_COMFY_ROOT:-$ROOT/ComfyUI}"
export H3_PYTHON="${H3_PYTHON:-$ROOT/.venv/bin/python}"
export H3_COMFY_URL="${H3_COMFY_URL:-http://127.0.0.1:8188}"
export H3_SERVER_LOG="${H3_SERVER_LOG:-$ROOT/comfyui.log}"
export H3_PANEL_PORT="${H3_PANEL_PORT:-8199}"
export H3_PANEL_BIND="${H3_PANEL_BIND:-127.0.0.1}"

PY="$H3_PYTHON"
[[ -x "$PY" ]] || PY="$(command -v python3)"

cd "$HERE"
echo "面板: http://$H3_PANEL_BIND:$H3_PANEL_PORT/   (ComfyUI: $H3_COMFY_URL)"
exec "$PY" server.py
