#!/usr/bin/env bash
# 01_install.sh - 装 ComfyUI + PyTorch(cu130) 基础环境
# 目标机器: DGX Spark GB10 / Ubuntu 24.04 / aarch64 / Python 3.12
# 用法: bash scripts/01_install.sh [安装目录，默认 $HOME/AI/minimax-h3]
# 特性: 可重复执行；已存在的 venv / 仓库不会被清掉
set -euo pipefail

ROOT="${1:-$HOME/AI/minimax-h3}"
COMFY="$ROOT/ComfyUI"
VENV="$ROOT/.venv"
COMFY_COMMIT="${COMFY_COMMIT:-e06965c4d6e07a546b91a70b4c6a0ec54bbdceb8}"
TORCH_VER="2.10.0"
TV_VER="0.25.0"
TA_VER="2.10.0"
CUDA_INDEX="https://download.pytorch.org/whl/cu130"

say() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

say "1/6 安装系统依赖"
sudo apt-get update -qq
sudo apt-get install -y -qq \
  git curl ca-certificates wget \
  python3.12-venv python3.12-dev \
  build-essential pkg-config ninja-build \
  ffmpeg libgl1 libglib2.0-0

say "2/6 建立目录与虚拟环境"
mkdir -p "$ROOT"
[[ -d "$VENV" ]] || python3.12 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install -q --upgrade pip setuptools wheel packaging ninja

say "3/6 安装 PyTorch ${TORCH_VER} (cu130)"
# 先写成 constraints，后面所有 pip 都带上，防止自定义节点把 torch 换掉
cat > "$ROOT/torch-constraints.txt" <<EOF
torch==${TORCH_VER}+cu130
torchvision==${TV_VER}+cu130
torchaudio==${TA_VER}+cu130
EOF
if python - <<'PY' >/dev/null 2>&1
import torch
assert torch.__version__.startswith("2.10.0")
assert torch.version.cuda and torch.version.cuda.startswith("13.")
assert torch.cuda.is_available()
PY
then
  echo "  torch 已就绪，跳过安装"
else
  python -m pip install \
    "torch==${TORCH_VER}" "torchvision==${TV_VER}" "torchaudio==${TA_VER}" \
    --index-url "$CUDA_INDEX"
fi

say "4/6 克隆 ComfyUI 并锁定版本"
if [[ ! -d "$COMFY/.git" ]]; then
  git clone https://github.com/comfyanonymous/ComfyUI.git "$COMFY"
fi
git -C "$COMFY" fetch --all --tags --quiet || true
git -C "$COMFY" checkout --quiet "$COMFY_COMMIT"
echo "  ComfyUI @ $(git -C "$COMFY" rev-parse --short HEAD)"

say "5/6 安装 ComfyUI 依赖（约束 torch 不被替换）"
python -m pip install -q -r "$COMFY/requirements.txt" -c "$ROOT/torch-constraints.txt"
python -m pip install -q huggingface_hub "huggingface_hub[cli]" websocket-client \
  -c "$ROOT/torch-constraints.txt"

say "6/6 自检"
python - <<'PY'
import torch, sys
print("  python :", sys.version.split()[0])
print("  torch  :", torch.__version__, "cuda:", torch.version.cuda)
print("  gpu    :", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "NOT AVAILABLE")
if not torch.cuda.is_available():
    raise SystemExit("  [FAIL] torch 看不到 CUDA，先查 nvidia-smi 与驱动")
x = torch.randn(1024, 1024, device="cuda", dtype=torch.bfloat16)
y = x @ x
torch.cuda.synchronize()
print("  bf16 matmul: OK")
PY

printf '\n安装完成。\n  目录: %s\n  下一步: bash scripts/02_download_models.sh\n' "$ROOT"
