#!/usr/bin/env bash
# 03_start_comfyui.sh - 前台启动 ComfyUI（Ctrl+C 退出）
# 用法: bash scripts/03_start_comfyui.sh [--log]
# 常驻请用: bash scripts/06_install_services.sh
set -euo pipefail

ROOT="${H3_ROOT:-$HOME/AI/minimax-h3}"
COMFY="$ROOT/ComfyUI"
VENV="$ROOT/.venv"
PORT="${H3_PORT:-8188}"
BIND="${H3_BIND:-127.0.0.1}"

[[ -d "$COMFY" ]] || { echo "找不到 $COMFY，先跑 01_install.sh"; exit 1; }
# shellcheck disable=SC1091
source "$VENV/bin/activate"
export IMAGEIO_FFMPEG_EXE="${IMAGEIO_FFMPEG_EXE:-/usr/bin/ffmpeg}"
# 统一内存机器不要开 lowvram：整模常驻，offload 反而把带宽优势打没
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"

cd "$COMFY"
ARGS=(main.py --listen "$BIND" --port "$PORT" --use-pytorch-cross-attention)
if [[ "${1:-}" == "--log" ]]; then
  python "${ARGS[@]}" 2>&1 | tee -a "$ROOT/comfyui.log"
else
  exec python "${ARGS[@]}"
fi
