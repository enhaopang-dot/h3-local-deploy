#!/usr/bin/env bash
# 00_check_env.sh - 安装前环境体检（不改任何东西，只读）
# 用法: bash scripts/00_check_env.sh
set -uo pipefail

FAIL=0
ok()   { printf '  [OK]   %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; FAIL=1; }
sec()  { printf '\n== %s ==\n' "$*"; }

sec "系统与架构"
ARCH="$(uname -m)"
OS_NAME="$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
printf '  系统: %s\n  架构: %s\n' "$OS_NAME" "$ARCH"
if [[ "$ARCH" == "aarch64" ]]; then
  ok "aarch64，符合 DGX Spark 主线要求"
else
  warn "非 aarch64。本包主线按 DGX Spark 写；x86_64 请改用 Windows/Linux 分档路线，见 01-部署路线选择.md"
fi

sec "GPU"
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
  DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
  CUDA_SHOW="$(nvidia-smi 2>/dev/null | awk -F'CUDA Version: ' '/CUDA Version/{print $2}' | awk '{print $1}')"
  printf '  GPU: %s\n  驱动: %s\n  nvidia-smi 报 CUDA: %s\n' "${GPU_NAME:-?}" "${DRIVER:-?}" "${CUDA_SHOW:-?}"
  [[ -n "$GPU_NAME" ]] && ok "GPU 可见" || bad "nvidia-smi 没返回 GPU 名"
else
  bad "找不到 nvidia-smi，GPU 驱动未就绪"
fi

sec "统一内存"
MEM_GB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 / 1024 ))
printf '  MemTotal: %s GB\n' "$MEM_GB"
if (( MEM_GB >= 100 )); then
  ok "内存 ≥100GB，可整模常驻，不需要 lowvram/offload"
elif (( MEM_GB >= 60 )); then
  warn "内存 60-99GB：可跑，但文本编码器和主模型可能来回换入换出，速度会掉"
else
  bad "内存 <60GB：不建议跑 33B INT8 全量，改走小显存路线或云端"
fi

sec "磁盘"
FREE_GB=$(df -Pk "${HOME}" | awk 'NR==2{print int($4/1024/1024)}')
printf '  家目录可用: %s GB\n' "$FREE_GB"
if (( FREE_GB >= 120 )); then
  ok "≥120GB，模型 66GB + ComfyUI + 成片够用"
elif (( FREE_GB >= 80 )); then
  warn "80-119GB：只装基础视频链路（不装可选模型）刚好，建议清盘"
else
  bad "<80GB：装不下，先清盘"
fi

sec "基础命令"
for c in git curl ffmpeg python3.12; do
  if command -v "$c" >/dev/null 2>&1; then
    ok "$c -> $(command -v "$c")"
  else
    bad "缺 $c（01_install.sh 会装）"
  fi
done

sec "端口"
for p in 8188 8199; do
  if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$p" 2>/dev/null | grep -q LISTEN; then
    warn "$p 已被占用：$(ss -ltnp 2>/dev/null | grep ":$p " | head -1)"
  else
    ok "$p 空闲"
  fi
done

sec "结论"
if (( FAIL == 0 )); then
  printf '  环境检查通过，可以执行 scripts/01_install.sh\n'
  exit 0
else
  printf '  有 FAIL 项，先按上面提示处理，处理完重跑本脚本\n'
  exit 1
fi
