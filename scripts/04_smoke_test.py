#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""04_smoke_test.py - 最小出片验收

做三件事：
1. 等 ComfyUI 起来，确认 MiniMaxH3ImageToVideo 节点在；
2. 用 panel/template_t2v.json 提交一条 2 秒 0.2MP 的短片；
3. 轮询到出片，检查 mp4 文件真实存在且非空。

用法:
  python3 scripts/04_smoke_test.py
  python3 scripts/04_smoke_test.py --seconds 3 --megapixels 0.4 --timeout 1800
退出码: 0 通过 / 2 环境没起来 / 3 提交或出片失败
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TPL = os.path.normpath(os.path.join(HERE, "..", "panel", "template_t2v.json"))
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(base, path, data=None, timeout=60):
    url = base.rstrip("/") + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    return _opener.open(req, timeout=timeout)


def wait_up(base, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with api(base, "/system_stats", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def normalize_model_paths(wf):
    """Windows 导出的模板用反斜杠；Linux 的枚举值是正斜杠。"""
    if os.sep == "/":
        for node in wf.values():
            ins = node.get("inputs", {})
            for k in ("unet_name", "clip_name", "vae_name", "lora_name"):
                if isinstance(ins.get(k), str):
                    ins[k] = ins[k].replace("\\", "/")
    return wf


def available_unets(base):
    with api(base, "/object_info/UNETLoader") as r:
        info = json.load(r)
    try:
        return set(info["UNETLoader"]["input"]["required"]["unet_name"][0])
    except Exception:
        return set()


def pick_output(hist, node_id):
    out = hist.get("outputs", {}).get(node_id, {})
    for key in ("images", "gifs", "videos"):
        if out.get(key):
            return out[key][0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("H3_COMFY_URL", "http://127.0.0.1:8188"))
    ap.add_argument("--template", default=DEFAULT_TPL)
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--megapixels", type=float, default=0.2)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--prompt", default="一只橘猫趴在窗台上晒太阳，阳光缓慢移动，镜头轻微推进，写实电影感")
    args = ap.parse_args()

    base = args.url
    print(f"[1/4] 等 ComfyUI: {base}（最多 300 秒）")
    if not wait_up(base, 300):
        print("[FAIL] ComfyUI 没起来。看 comfyui.log 或 systemctl --user status comfyui-h3")
        return 2
    print("      已就绪")

    print("[2/4] 检查原生 H3 节点")
    with api(base, "/object_info") as r:
        nodes = json.load(r)
    if "MiniMaxH3ImageToVideo" not in nodes:
        print("[FAIL] 没有 MiniMaxH3ImageToVideo 节点。ComfyUI 版本太旧，重跑 01_install.sh 锁到 e06965c")
        return 2
    print("      MiniMaxH3ImageToVideo 存在")

    with open(args.template, encoding="utf-8") as f:
        wf = json.load(f)
    wf = normalize_model_paths(wf)

    required = ["92", "115", "140:129", "140:131", "140:133", "140:138", "140:139"]
    missing = [k for k in required if k not in wf]
    if missing:
        print(f"[FAIL] 模板缺节点 {missing}，模板文件被改过，换回交付包里的原版")
        return 3

    unets = available_unets(base)
    turbo = "MiniMax-H3/minimax_h3_fl2va_pruned_turbo_int8_convrot.safetensors"
    base_model = "MiniMax-H3/minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    if turbo in unets:
        use_turbo, steps = True, args.steps
        print(f"      模型: 8 步 turbo，{steps} 步")
    elif base_model in unets:
        use_turbo, steps = False, 20
        print("      模型: 基模（没下 turbo），改 20 步，出片会慢约 2.5 倍")
    else:
        print(f"[FAIL] UNETLoader 里找不到 H3 模型。当前可见: {sorted(unets)[:10]}")
        return 3

    wf["140:131"]["inputs"]["prompt"] = args.prompt
    wf["140:133"]["inputs"]["value"] = float(args.seconds)
    wf["115"]["inputs"]["megapixels"] = float(args.megapixels)
    wf["140:129"]["inputs"]["noise_seed"] = random.randint(1, 2**31)
    wf["140:139"]["inputs"]["value"] = use_turbo
    wf["140:138"]["inputs"]["value"] = int(steps)
    wf["92"]["inputs"]["filename_prefix"] = "smoke/H3_smoke"

    print(f"[3/4] 提交任务: {args.seconds}s / {args.megapixels}MP / {steps} 步")
    payload = {"prompt": wf, "client_id": uuid.uuid4().hex}
    t0 = time.time()
    try:
        with api(base, "/prompt", payload, timeout=60) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        print("[FAIL] 提交被拒:")
        print(e.read().decode("utf-8", "replace")[:4000])
        return 3
    pid = res.get("prompt_id")
    if not pid:
        print(f"[FAIL] 没拿到 prompt_id: {res}")
        return 3
    print(f"      prompt_id={pid}")

    print("[4/4] 等出片（首次含模型加载，可能 2-5 分钟）")
    last = 0
    while time.time() - t0 < args.timeout:
        time.sleep(5)
        try:
            with api(base, f"/history/{pid}", timeout=30) as r:
                hist_all = json.load(r)
        except Exception:
            continue
        hist = hist_all.get(pid)
        if not hist:
            continue
        status = hist.get("status", {})
        if status.get("status_str") == "error":
            print("[FAIL] 任务报错:")
            print(json.dumps(status, ensure_ascii=False)[:4000])
            return 3
        item = pick_output(hist, "92")
        if item:
            elapsed = time.time() - t0
            fname = item.get("filename", "")
            sub = item.get("subfolder", "")
            typ = item.get("type", "output")
            q = urllib.parse.urlencode({"filename": fname, "subfolder": sub, "type": typ})
            with api(base, "/view?" + q, timeout=120) as rr:
                data = rr.read()
            if len(data) < 10000:
                print(f"[FAIL] 成片过小（{len(data)} 字节），可能只是封面")
                return 3
            print()
            print(f"[PASS] 出片成功，耗时 {elapsed:.1f} 秒")
            print(f"       文件: {sub}/{fname}  {len(data)/1024/1024:.2f} MB")
            return 0
        if time.time() - last > 30:
            last = time.time()
            print(f"      还在跑… 已等 {int(time.time()-t0)}s")
    print(f"[FAIL] 超过 {args.timeout}s 没出片，看 comfyui.log")
    return 3


if __name__ == "__main__":
    sys.exit(main())
