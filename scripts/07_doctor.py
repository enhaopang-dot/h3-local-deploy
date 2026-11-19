#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""07_doctor.py - 出问题先跑这个，按顺序体检并给下一步动作

用法:
  python3 scripts/07_doctor.py
  python3 scripts/07_doctor.py --url http://<SPARK_IP>:8188
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.environ.get("H3_ROOT", os.path.expanduser("~/AI/minimax-h3"))
COMFY = os.path.join(ROOT, "ComfyUI")
MODELS = os.path.join(COMFY, "models")
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

OK, BAD, WARN = "OK  ", "FAIL", "WARN"
problems = []


def line(tag, msg, fix=""):
    print(f"  [{tag}] {msg}")
    if tag != OK and fix:
        print(f"         下一步: {fix}")
    if tag == BAD:
        problems.append(msg)


def get(base, path, timeout=10):
    with _opener.open(base.rstrip("/") + path, timeout=timeout) as r:
        return json.load(r)


def check_ports():
    print("\n== 端口与进程 ==")
    for port, name in ((8188, "ComfyUI"), (8199, "面板")):
        r = subprocess.run(["bash", "-lc", f"ss -ltn 2>/dev/null | grep -q ':{port} '"],
                           capture_output=True)
        if r.returncode == 0:
            line(OK, f"{name} 端口 {port} 在监听")
        else:
            line(WARN, f"{name} 端口 {port} 没监听", "systemctl --user start comfyui-h3")


def check_api(base):
    print("\n== ComfyUI 接口 ==")
    try:
        st = get(base, "/system_stats", 8)
        sysinfo = st.get("system", {})
        line(OK, f"/system_stats 正常，python {sysinfo.get('python_version', '?').split()[0]}")
    except Exception as e:
        line(BAD, f"/system_stats 不通: {e}", "看 journalctl --user -u comfyui-h3 -n 80")
        return
    try:
        nodes = get(base, "/object_info", 30)
        if "MiniMaxH3ImageToVideo" in nodes:
            line(OK, "原生 H3 节点存在")
        else:
            line(BAD, "缺 MiniMaxH3ImageToVideo 节点", "ComfyUI 版本不对，重跑 01_install.sh")
        if "MiniMaxH3ReferenceToVideo" in nodes:
            line(OK, "RefMod 节点存在（R2V 可用）")
        else:
            line(WARN, "没有 RefMod 节点（只影响参考生视频）", "装 ComfyUI-MiniMaxH3Mod，见文档第 7 节")
    except Exception as e:
        line(WARN, f"/object_info 读不到: {e}")


def check_models():
    print("\n== 模型文件 ==")
    need = [
        ("diffusion_models/MiniMax-H3/minimax_h3_fl2va_pruned_int8_convrot.safetensors", 19e9, "FL2VA 基模"),
        ("diffusion_models/MiniMax-H3/minimax_h3_fl2va_pruned_turbo_int8_convrot.safetensors", 19e9, "8步 turbo"),
        ("text_encoders/MiniMax-H3/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", 14e9, "文本编码器"),
        ("vae/MiniMax-H3/minimax_h3_video_vae_fp16.safetensors", 5e9, "视频 VAE"),
        ("vae/MiniMax-H3/minimax_h3_audio_vae_fp32.safetensors", 0.5e9, "音频 VAE"),
    ]
    for rel, min_sz, label in need:
        p = os.path.join(MODELS, rel)
        if not os.path.isfile(p):
            line(BAD, f"缺 {label}: {rel}", "bash scripts/02_download_models.sh")
        elif os.path.getsize(p) < min_sz:
            line(BAD, f"{label} 大小异常（{os.path.getsize(p)/1e9:.1f}GB）", f"删了重下: {p}")
        else:
            line(OK, f"{label} {os.path.getsize(p)/1e9:.1f}GB")


def check_disk():
    print("\n== 磁盘 ==")
    total, used, free = shutil.disk_usage(ROOT if os.path.isdir(ROOT) else os.path.expanduser("~"))
    line(OK if free > 20e9 else WARN,
         f"安装盘剩余 {free/1e9:.1f}GB",
         "清 output/_trash 或旧成片")


def check_log():
    print("\n== 最近日志 ==")
    log = os.path.join(ROOT, "comfyui.log")
    if not os.path.isfile(log):
        line(WARN, "没有 comfyui.log（可能不是用 03_start_comfyui.sh 起的）")
        return
    with open(log, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 20000))
        tail = f.read().decode("utf-8", "replace")
    keys = ["Traceback", "Error", "error", "OutOfMemory", "CUDA", "not found", "No module"]
    hits = [ln for ln in tail.splitlines() if any(k in ln for k in keys)][-8:]
    if hits:
        for h in hits:
            print("    " + h[:180])
        line(WARN, "日志里有报错关键字，按上面几行走")
    else:
        line(OK, "日志末尾没看到明显报错")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("H3_COMFY_URL", "http://127.0.0.1:8188"))
    args = ap.parse_args()

    print(f"体检: {ROOT}")
    check_ports()
    check_api(args.url)
    check_models()
    check_disk()
    check_log()

    print("\n== 结论 ==")
    if not problems:
        print("  全部通过。出片慢是正常现象，对照 02-性能预期.md")
        return 0
    print(f"  有 {len(problems)} 个问题，按上面的「下一步」处理")
    return 1


if __name__ == "__main__":
    sys.exit(main())
