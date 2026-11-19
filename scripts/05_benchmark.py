#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""05_benchmark.py - 跑三档标准测试，拿到本机真实耗时

默认三档（都用 8 步 turbo）：
  A. 2s / 0.2MP   冒烟
  B. 5s / 0.4MP   日常
  C. 10s / 0.9MP  720p 成品
加 --with-base 再跑 D: 10s / 0.9MP / 20 步基模，用来看画质上限的代价。

用法:
  python3 scripts/05_benchmark.py
  python3 scripts/05_benchmark.py --with-base --repeat 2
结果落 scripts/benchmark-result.json
"""
import argparse
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TPL = os.path.normpath(os.path.join(HERE, "..", "panel", "template_t2v.json"))
OUT_JSON = os.path.join(HERE, "benchmark-result.json")
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(base, path, data=None, timeout=60):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"})
    return _opener.open(req, timeout=timeout)


def normalize(wf):
    if os.sep == "/":
        for n in wf.values():
            ins = n.get("inputs", {})
            for k in ("unet_name", "clip_name", "vae_name", "lora_name"):
                if isinstance(ins.get(k), str):
                    ins[k] = ins[k].replace("\\", "/")
    return wf


def load_tpl(path):
    with open(path, encoding="utf-8") as f:
        return normalize(json.load(f))


def run_one(base, tpl, seconds, mp, steps, turbo, prompt, timeout):
    wf = json.loads(json.dumps(tpl))
    wf["140:131"]["inputs"]["prompt"] = prompt
    wf["140:133"]["inputs"]["value"] = float(seconds)
    wf["115"]["inputs"]["megapixels"] = float(mp)
    wf["140:129"]["inputs"]["noise_seed"] = random.randint(1, 2**31)
    wf["140:139"]["inputs"]["value"] = bool(turbo)
    wf["140:137"]["inputs"]["value"] = 20
    wf["140:138"]["inputs"]["value"] = int(steps)
    wf["92"]["inputs"]["filename_prefix"] = "bench/H3_bench"

    t0 = time.time()
    with api(base, "/prompt", {"prompt": wf, "client_id": uuid.uuid4().hex}) as r:
        pid = json.load(r)["prompt_id"]
    while time.time() - t0 < timeout:
        time.sleep(5)
        try:
            with api(base, f"/history/{pid}", timeout=30) as r:
                hist = json.load(r).get(pid)
        except Exception:
            continue
        if not hist:
            continue
        if hist.get("status", {}).get("status_str") == "error":
            raise RuntimeError(json.dumps(hist["status"], ensure_ascii=False)[:1500])
        out = hist.get("outputs", {}).get("92", {})
        for key in ("images", "gifs", "videos"):
            if out.get(key):
                it = out[key][0]
                return time.time() - t0, it.get("subfolder", ""), it.get("filename", "")
    raise TimeoutError(f"超过 {timeout}s 没出片")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("H3_COMFY_URL", "http://127.0.0.1:8188"))
    ap.add_argument("--template", default=DEFAULT_TPL)
    ap.add_argument("--repeat", type=int, default=1, help="每档重复次数，取中位数")
    ap.add_argument("--with-base", action="store_true")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--prompt", default="雨后的城市天台，人物缓慢转身看向镜头，写实电影感，镜头平稳推进")
    args = ap.parse_args()

    tpl = load_tpl(args.template)
    cases = [
        ("A 冒烟 2s 0.2MP 8步", 2, 0.2, 8, True),
        ("B 日常 5s 0.4MP 8步", 5, 0.4, 8, True),
        ("C 成品 10s 0.9MP 8步", 10, 0.9, 8, True),
    ]
    if args.with_base:
        cases.append(("D 画质 10s 0.9MP 20步", 10, 0.9, 20, False))

    results = []
    for label, sec, mp, steps, turbo in cases:
        times = []
        print(f"\n== {label} ==")
        for i in range(args.repeat):
            try:
                dt, sub, fn = run_one(args.url, tpl, sec, mp, steps, turbo,
                                      args.prompt, args.timeout)
            except urllib.error.HTTPError as e:
                print("  提交失败:", e.read().decode("utf-8", "replace")[:1000])
                return 3
            except Exception as e:
                print("  失败:", e)
                return 3
            times.append(dt)
            print(f"  第 {i+1} 次: {dt:.1f}s  {sub}/{fn}")
        med = statistics.median(times)
        results.append({"case": label, "seconds": sec, "megapixels": mp,
                        "steps": steps, "turbo": turbo,
                        "runs_sec": [round(x, 1) for x in times],
                        "median_sec": round(med, 1)})
        print(f"  中位: {med:.1f}s ({med/60:.1f} 分钟)")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "url": args.url,
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
