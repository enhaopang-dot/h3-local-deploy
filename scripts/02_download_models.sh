#!/usr/bin/env bash
# 02_download_models.sh - 下载 MiniMax H3 模型（默认约 66GB）
# 用法:
#   bash scripts/02_download_models.sh              # 基础视频链路（FL2VA 基模 + 预焊 turbo）
#   bash scripts/02_download_models.sh --with-ref2v # 加参考生视频（+21GB）
#   bash scripts/02_download_models.sh --with-image # 加生图/改图（+12GB）
#   bash scripts/02_download_models.sh --all
# 网络: 默认先试 huggingface.co，5 秒不通自动切 hf-mirror；也可手动 HF_ENDPOINT=... 指定
set -euo pipefail

ROOT="${H3_ROOT:-$HOME/AI/minimax-h3}"
MODELS="$ROOT/ComfyUI/models"
TMP="$ROOT/_hf_tmp"
mkdir -p "$MODELS" "$TMP"

WITH_REF2V=0
WITH_IMAGE=0
for a in "$@"; do
  case "$a" in
    --with-ref2v) WITH_REF2V=1 ;;
    --with-image) WITH_IMAGE=1 ;;
    --all) WITH_REF2V=1; WITH_IMAGE=1 ;;
    *) echo "未知参数: $a"; exit 2 ;;
  esac
done

if [[ -z "${HF_ENDPOINT:-}" ]]; then
  if curl -fsS --max-time 5 -o /dev/null https://huggingface.co 2>/dev/null; then
    echo "网络: 直连 huggingface.co"
  else
    export HF_ENDPOINT="https://hf-mirror.com"
    echo "网络: 直连不通，切镜像 $HF_ENDPOINT"
  fi
fi
export HF_HUB_DISABLE_XET=1   # hf-mirror 不代理 xet CAS，必须走普通 HTTP

H3_REV="e5eb578a89295337b8ff433a035929ce0279e0b6"
TURBO_REV="30f485ed874aa44c8183348ad361c5eabad318da"

# get <repo> <repo内路径> <目标目录> [revision]
get() {
  local repo="$1" file="$2" dest="$3" rev="${4:-main}"
  local base; base="$(basename "$file")"
  mkdir -p "$dest"
  if [[ -f "$dest/$base" ]]; then
    echo "  已存在，跳过: $base"
    return 0
  fi
  echo "  下载: $repo/$file"
  hf download "$repo" "$file" --revision "$rev" --local-dir "$TMP"
  if [[ ! -f "$TMP/$file" ]]; then
    echo "  [FAIL] 下载后找不到 $TMP/$file"; exit 1
  fi
  mv -f "$TMP/$file" "$dest/$base"
}

need_mb() {  # need_mb <最小MB> <文件> <说明>
  local min="$1" f="$2" label="$3"
  if [[ ! -f "$f" ]]; then echo "  [FAIL] 缺文件: $f"; exit 1; fi
  local sz mb; sz=$(stat -c%s "$f"); mb=$(( sz / 1000000 ))
  if (( mb < min )); then
    echo "  [FAIL] $label 大小异常: ${mb}MB（期望 ≥${min}MB），删掉重下: $f"; exit 1
  fi
  printf '  [OK] %-58s %6.2f GB\n' "$label" "$(awk "BEGIN{print $sz/1000000000}")"
}

echo
echo "== 基础视频链路 =="
get "Comfy-Org/MiniMax-H3" "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors" "$MODELS/diffusion_models/MiniMax-H3" "$H3_REV"
get "Comfy-Org/MiniMax-H3" "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"           "$MODELS/text_encoders/MiniMax-H3"      "$H3_REV"
get "Comfy-Org/MiniMax-H3" "vae/minimax_h3_video_vae_fp16.safetensors"                             "$MODELS/vae/MiniMax-H3"                "$H3_REV"
get "Comfy-Org/MiniMax-H3" "vae/minimax_h3_audio_vae_fp32.safetensors"                             "$MODELS/vae/MiniMax-H3"                "$H3_REV"

echo
echo "== 8 步加速（预焊 turbo 整模，直接换 UNet，不用挂 LoRA）=="
get "PulpCut/MiniMax-H3-Turbo-INT8-ConvRot" "minimax_h3_fl2va_pruned_turbo_int8_convrot.safetensors" "$MODELS/diffusion_models/MiniMax-H3"

if (( WITH_REF2V )); then
  echo
  echo "== 参考生视频（R2V）=="
  get "PulpCut/MiniMax-H3-Ref2VA-Turbo-INT8-ConvRot" "minimax_h3_ref2va_pruned_turbo_int8_convrot.safetensors" "$MODELS/diffusion_models/MiniMax-H3"
fi

if (( WITH_IMAGE )); then
  echo
  echo "== 生图（Z-Image-Turbo）=="
  get "Comfy-Org/z_image_turbo" "split_files/diffusion_models/z_image_turbo_int8_convrot.safetensors" "$MODELS/diffusion_models/Z-Image-Turbo"
  get "Comfy-Org/z_image_turbo" "split_files/text_encoders/qwen_3_4b_fp8_mixed.safetensors"           "$MODELS/text_encoders/Z-Image-Turbo"
  get "Comfy-Org/z_image_turbo" "split_files/vae/ae.safetensors"                                      "$MODELS/vae/Z-Image-Turbo"

  echo
  echo "== 改图（Qwen-Image-Edit 2511）=="
  get "Comfy-Org/Qwen-Image-Edit_ComfyUI" "split_files/diffusion_models/qwen_image_edit_2511_int8_convrot.safetensors" "$MODELS/diffusion_models"
  get "Comfy-Org/Qwen-Image_ComfyUI"      "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors"            "$MODELS/text_encoders"
  get "Comfy-Org/Qwen-Image_ComfyUI"      "split_files/vae/qwen_image_vae.safetensors"                                   "$MODELS/vae"
fi

echo
echo "== 文件校验 =="
need_mb 19000 "$MODELS/diffusion_models/MiniMax-H3/minimax_h3_fl2va_pruned_int8_convrot.safetensors" "FL2VA 基模"
need_mb 19000 "$MODELS/diffusion_models/MiniMax-H3/minimax_h3_fl2va_pruned_turbo_int8_convrot.safetensors" "FL2VA 8步 turbo"
need_mb 14000 "$MODELS/text_encoders/MiniMax-H3/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors" "Qwen3VL-32B 文本编码器"
need_mb 5000  "$MODELS/vae/MiniMax-H3/minimax_h3_video_vae_fp16.safetensors" "视频 VAE"
need_mb 500   "$MODELS/vae/MiniMax-H3/minimax_h3_audio_vae_fp32.safetensors" "音频 VAE"
(( WITH_REF2V )) && need_mb 19000 "$MODELS/diffusion_models/MiniMax-H3/minimax_h3_ref2va_pruned_turbo_int8_convrot.safetensors" "R2V turbo"
(( WITH_IMAGE )) && need_mb 5000 "$MODELS/diffusion_models/Z-Image-Turbo/z_image_turbo_int8_convrot.safetensors" "Z-Image-Turbo"

echo
echo "模型就绪。下一步: bash scripts/03_start_comfyui.sh"
