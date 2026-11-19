# -*- coding: utf-8 -*-
"""视频复刻（换商品 / 换装）：保运动、只改指定区域。

思路：
1. 用 ffmpeg 把原片抽帧；
2. 只在关键帧上让 Qwen-Image-Edit 重绘框内区域（喂原框截图 + 商品图）；
3. 相邻帧用光流把关键帧结果跟着画面运动挪过去，保证帧间不跳；
4. 回贴原帧、重新编码，音轨沿用原片。

它是「视频修复 + 参考图重绘」的等价实现，不是文生视频：
原片的镜头运动、人物动作、背景都保留，只换框里的东西。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

COMFY = os.environ.get("H3_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
HERE = Path(__file__).parent
TPL = HERE / "template_i2e.json"
OUT_ROOT = Path(os.environ.get(
    "H3_OUTPUT_DIR",
    (HERE / ".." / "ComfyUI" / "output").resolve()))
OUT_VIDEO = OUT_ROOT / "video"
OUT_VIDEO.mkdir(parents=True, exist_ok=True)

# 进度给前端轮询
STATE: dict = {"running": False, "phase": "", "done": 0, "total": 0,
               "msg": "", "result": "", "error": "", "ts": 0.0}


def _log(msg: str) -> None:
    STATE["msg"] = msg
    STATE["ts"] = time.time()
    try:
        with open(HERE / "replicate.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------- ComfyUI 客户端 ----------------

def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----aiNative" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fname, data, ctype) in files.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n".encode()
        body += f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def comfy_upload(path: Path, name: str | None = None) -> str:
    """上传图片到 ComfyUI input，返回它认的文件名。"""
    name = name or path.name
    ctype = mimetypes.guess_type(name)[0] or "image/png"
    body, ct = _multipart({"overwrite": "true", "type": "input"},
                          {"image": (name, path.read_bytes(), ctype)})
    req = urllib.request.Request(f"{COMFY}/upload/image", data=body,
                                 headers={"Content-Type": ct}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    sub = d.get("subfolder") or ""
    fn = d.get("name") or name
    return f"{sub}/{fn}" if sub else fn


def comfy_run(wf: dict, timeout: float = 600) -> Path:
    """提交工作流并等结果，返回第一张输出图。"""
    cid = uuid.uuid4().hex
    body = json.dumps({"prompt": wf, "client_id": cid}).encode()
    req = urllib.request.Request(f"{COMFY}/api/prompt", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        pid = json.load(r)["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(2)
        try:
            with urllib.request.urlopen(f"{COMFY}/history/{pid}", timeout=20) as r:
                h = json.load(r)
        except Exception:  # noqa: BLE001
            continue
        job = h.get(pid)
        if not job:
            continue
        outs = (job.get("outputs") or {})
        for node in outs.values():
            for img in (node.get("images") or []):
                q = urllib.parse.urlencode({"filename": img.get("filename"),
                                            "subfolder": img.get("subfolder", ""),
                                            "type": img.get("type", "output")})
                with urllib.request.urlopen(f"{COMFY}/view?{q}", timeout=60) as r:
                    data = r.read()
                tmp = Path(tempfile.gettempdir()) / f"rep_{uuid.uuid4().hex}.png"
                tmp.write_bytes(data)
                return tmp
        if job.get("status", {}).get("status_str") == "error":
            raise RuntimeError("ComfyUI 执行失败")
    raise TimeoutError("ComfyUI 超时")


EDIT_STEPS = 8          # Qwen-Image-Edit 在 cfg=1 下 8 步够用，20 步是三四倍的耗时
EDIT_MAX = 768          # 关键帧送编辑时的长边；1024 换成 768，算力降到 56%


def _build_wf(frame_img: str, prompt: str, seed: int) -> dict:
    """单图编辑工作流：面板「图改图」用的同一份模板，已验证可用。"""
    wf = json.loads(TPL.read_text(encoding="utf-8"))
    for node in wf.values():
        ct = node.get("class_type")
        ins = node.setdefault("inputs", {})
        if ct == "LoadImage":
            ins["image"] = frame_img
        if ct == "KSampler":
            ins["seed"] = seed
            ins["steps"] = EDIT_STEPS
        if ct in ("TextEncodeQwenImageEditPlus", "TextEncodeQwenImageEdit"):
            ins["prompt"] = prompt
    return wf


def _compose_crop(crop_path: Path, product_path: Path, out_path: Path) -> Path:
    """把商品图按比例贴进裁剪区，交给单图编辑去融合光照与边缘。

    直接双图参考这张卡不吃（会糊成一片），所以先合成再让模型「融进去」。
    """
    from PIL import Image, ImageDraw, ImageFilter
    base = Image.open(crop_path).convert("RGBA")
    prod = Image.open(product_path).convert("RGBA")
    W, H = base.size
    m = 0.10
    tw, th = int(W * (1 - 2 * m)), int(H * (1 - 2 * m))
    r = min(tw / prod.width, th / prod.height)
    prod = prod.resize((max(1, int(prod.width * r)), max(1, int(prod.height * r))), Image.LANCZOS)

    # 产品图没有 alpha 时，用圆角矩形做软边，别留一张硬边方块
    alpha = prod.getchannel("A")
    if alpha.getextrema()[0] >= 250:
        mask = Image.new("L", prod.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, prod.width - 1, prod.height - 1],
                                               radius=max(4, min(prod.size) // 12), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(max(1, min(prod.size) // 40)))
        prod.putalpha(mask)
    px = (W - prod.width) // 2
    py = (H - prod.height) // 2
    base.alpha_composite(prod, (px, py))
    base.convert("RGB").save(out_path)
    return out_path


# ---------------- 视频处理 ----------------

def _ffmpeg(*args: str) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("找不到 ffmpeg，先 sudo apt install ffmpeg")
    r = subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {r.stderr[-300:]}")


def _probe(path: Path) -> dict:
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("找不到 ffprobe，先 sudo apt install ffmpeg")
    r = subprocess.run([exe, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate,duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    s = json.loads(r.stdout)["streams"][0]
    num, den = (s.get("r_frame_rate") or "25/1").split("/")
    return {"w": int(s["width"]), "h": int(s["height"]),
            "fps": float(s.get("duration") and 1 or 1) and (float(num) / float(den or 1)),
            "dur": float(s.get("duration") or 0)}


def _warp_region(prev_gray, cur_gray, region_img, box):
    """用相邻两帧的光流，把上一帧的替换区域挪到当前帧位置。"""
    import numpy as np
    import cv2
    x, y, w, h = box
    flow = cv2.calcOpticalFlowFarneback(prev_gray, cur_gray, None,
                                        0.5, 3, 21, 3, 5, 1.2, 0)
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    sx = (gx + x + flow[y:y + h, x:x + w, 0]).astype(np.float32)
    sy = (gy + y + flow[y:y + h, x:x + w, 1]).astype(np.float32)
    return cv2.remap(region_img, sx, sy, interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)


def replicate(video_path: Path, product_path: Path, box: tuple[int, int, int, int],
              prompt: str = "", key_step: int = 12, fps: float | None = None,
              out_name: str | None = None) -> Path:
    """主流程。box = (x, y, w, h)，按原片像素坐标。"""
    import cv2
    import numpy as np

    STATE.update(running=True, error="", result="", done=0, total=0, phase="准备")
    info = _probe(video_path)
    fps = fps or min(info["fps"], 15)
    work = Path(tempfile.mkdtemp(prefix="replicate_"))
    try:
        frames_dir = work / "frames"
        frames_dir.mkdir()
        _log(f"抽帧 {fps}fps")
        _ffmpeg("-i", str(video_path), "-vf", f"fps={fps}", str(frames_dir / "f_%05d.png"))
        frames = sorted(frames_dir.glob("f_*.png"))
        if not frames:
            raise RuntimeError("没有抽到帧")
        STATE["total"] = len(frames)

        _ = product_path  # 商品图在合成阶段用，编辑阶段只传合成结果
        x, y, w, h = box
        # 对齐到 8 的倍数，避免尺寸抖动
        w8, h8 = w - w % 8, h - h % 8
        w, h = max(64, w8), max(64, h8)

        key_idx = list(range(0, len(frames), max(2, key_step)))
        edited: dict[int, np.ndarray] = {}
        first = cv2.imread(str(frames[0]))
        H, W = first.shape[:2]
        x = max(0, min(x, W - w))
        y = max(0, min(y, H - h))

        for n, i in enumerate(key_idx, 1):
            STATE["phase"] = "重绘关键帧"
            STATE["done"] = n
            img = cv2.imread(str(frames[i]))
            crop = img[y:y + h, x:x + w]
            # 放大到 1024 再送编辑，细节更好
            scale = EDIT_MAX / max(w, h)
            big = cv2.resize(crop, (int(w * scale) // 8 * 8, int(h * scale) // 8 * 8),
                             interpolation=cv2.INTER_CUBIC)
            fp = work / f"crop_{i:05d}.png"
            cv2.imwrite(str(fp), big)
            comp = _compose_crop(fp, product_path, work / f"comp_{i:05d}.png")
            up = comfy_upload(comp)
            wf = _build_wf(up, prompt, seed=1000 + i)
            res = comfy_run(wf)
            out = cv2.imread(str(res))
            if out is None:
                raise RuntimeError("编辑结果读不出来")
            edited[i] = cv2.resize(out, (w, h), interpolation=cv2.INTER_AREA)
            _log(f"关键帧 {i} 完成 ({n}/{len(key_idx)})")

        STATE["phase"] = "回贴与补帧"
        out_dir = work / "out"
        out_dir.mkdir()
        prev_gray = cv2.cvtColor(cv2.imread(str(frames[0])), cv2.COLOR_BGR2GRAY)
        keys = sorted(edited)
        for i, f in enumerate(frames):
            cur = cv2.imread(str(f))
            cur_gray = cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)
            k = max([q for q in keys if q <= i], default=keys[0])
            reg = edited[k]
            if i != k:
                try:
                    base_gray = cv2.cvtColor(cv2.imread(str(frames[k])), cv2.COLOR_BGR2GRAY)
                    reg = _warp_region(base_gray, cur_gray, edited[k], (x, y, w, h))
                except Exception:  # noqa: BLE001
                    reg = edited[k]
            # 羽化边缘，别露硬边
            mask = np.zeros((h, w), np.float32)
            cv2.rectangle(mask, (0, 0), (w - 1, h - 1), 1.0, -1)
            mask = cv2.GaussianBlur(mask, (0, 0), max(3, min(w, h) / 40))[..., None]
            cur[y:y + h, x:x + w] = (cur[y:y + h, x:x + w] * (1 - mask) + reg * mask).astype(np.uint8)
            cv2.imwrite(str(out_dir / f.name), cur)
            prev_gray = cur_gray
            STATE["done"] = i + 1

        STATE["phase"] = "编码"
        OUT_VIDEO.mkdir(parents=True, exist_ok=True)
        name = out_name or f"replicate_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        dst = OUT_VIDEO / name
        _ffmpeg("-framerate", str(fps), "-i", str(out_dir / "f_%05d.png"),
                "-i", str(video_path), "-map", "0:v:0", "-map", "1:a?",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                "-c:a", "aac", "-shortest", str(dst))
        STATE.update(running=False, phase="完成", result=str(dst))
        _log(f"完成 -> {dst}")
        return dst
    except Exception as e:  # noqa: BLE001
        STATE.update(running=False, error=str(e)[:300], phase="失败")
        _log(f"失败 {type(e).__name__}: {e}")
        raise
    finally:
        STATE["running"] = False


def spawn(video_path: Path, product_path: Path, box, prompt,
          key_step: int = 12, fps: float | None = None, out_name: str | None = None) -> None:
    threading.Thread(target=lambda: _safe(video_path, product_path, box, prompt, key_step, fps, out_name),
                     daemon=True).start()


def _safe(*a, **k) -> None:
    try:
        replicate(*a, **k)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(STATE, ensure_ascii=False))
