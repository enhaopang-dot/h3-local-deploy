#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把模板里的模型路径分隔符改成当前系统的形式。

ComfyUI 的模型下拉枚举值是 os.path.join 出来的：Windows 是
`MiniMax-H3\\xxx.safetensors`，Linux 是 `MiniMax-H3/xxx.safetensors`。
模板跨平台搬运时跑一次这个脚本即可。
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS = ("unet_name", "clip_name", "vae_name", "lora_name")
changed = 0

for path in sorted(glob.glob(os.path.join(HERE, "template_*.json"))):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    n = 0
    for node in data.values():
        ins = node.get("inputs", {})
        for k in KEYS:
            v = ins.get(k)
            if isinstance(v, str):
                new = v.replace("\\", "/") if os.sep == "/" else v.replace("/", "\\")
                if new != v:
                    ins[k] = new
                    n += 1
    if n:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  {os.path.basename(path)}: 改了 {n} 处")
        changed += n

print(f"完成，共 {changed} 处。当前系统分隔符: {os.sep!r}")
sys.exit(0)
