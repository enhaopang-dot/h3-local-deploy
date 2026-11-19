# -*- coding: utf-8 -*-
"""1080p 超分合成: ffmpeg抽帧 -> ncnn-vulkan 目录批量x4 -> lanczos收放(长边1920保源比例) -> 贴原音轨
供 server.py 在 高清(0.9MP) 任务生成完成后自动调用(0.9MP直出必崩,这是替代链路);也可命令行独立用:
  python upscale720.py <输入.mp4> [输出.mp4]
"""
import json, os, shutil, subprocess, tempfile, time, urllib.request

BASE = os.environ.get('COMFYUI_BASE', 'http://127.0.0.1:8188')
MODEL = 'RealESRGAN_x4plus.pth'
# 目标分辨率: 长边1920, 严格保源片比例(10-13修——原写死1920x1080把竖屏拉成横屏); x4放大后lanczos收放
NCNN = os.environ.get('UPSCALER_NCNN', '')
# 10-12 实测: 6900HX+独显 0.12s/帧(目录批量), 比 ComfyUI 逐帧提交链路(6.15s/帧)快约 50 倍
# 链路: ffmpeg抽帧 -> ncnn-vulkan 目录批量x4(模型只载一次,零HTTP/队列开销) -> lanczos收放 -> 贴原音轨


def _api(path, data=None, timeout=120, retries=6, log=print):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(data).encode('utf-8') if data else None,
                                 headers={'Content-Type': 'application/json'})
    last = None
    for i in range(retries):
        try:
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except Exception as e:
            last = e
            log('  [warn] %s 第%d次失败: %s' % (path, i + 1, e))
            time.sleep(10)
    raise RuntimeError('ComfyUI 不可达: %s' % last)


def _upload(path, folder):
    """上传到 ComfyUI input 的指定子文件夹(10-12 起: 每轮超分独立文件夹, 帧不再混堆 input 根目录)"""
    boundary = '----up720'
    name = os.path.basename(path)
    body = ((f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
             f'Content-Type: application/octet-stream\r\n\r\n').encode() + open(path, 'rb').read()
            + (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\n{folder}'
               f'\r\n--{boundary}--\r\n').encode())
    req = urllib.request.Request(BASE + '/upload/image', data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    json.load(urllib.request.urlopen(req, timeout=60))
    return folder + '/' + name   # LoadImage 按 子目录/文件名 格式读取


def _probe_fps(src):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=r_frame_rate', '-of', 'csv=p=0', src],
                       capture_output=True, text=True)
    num, den = r.stdout.strip().split('/')
    return float(num) / float(den)


def _probe_wh(src):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=width,height', '-of', 'csv=p=0', src],
                       capture_output=True, text=True)
    w, h = (int(x) for x in r.stdout.strip().split(','))
    return w, h


def _target_for(w, h):
    """按源片比例定目标: 长边1920, 严格保源比例(10-13修: 原来写死1920x1080, 竖屏被拉成横屏), 取偶防x264报错"""
    if w >= h:
        tw, th = 1920, max(2, round(1920 * h / w / 2) * 2)
    else:
        th, tw = 1920, max(2, round(1920 * w / h / 2) * 2)
    return tw, th


def _upscale_frame(img_name, tag, out_root, log):
    wf = {
        "1": {"inputs": {"model_name": MODEL}, "class_type": "UpscaleModelLoader"},
        "2": {"inputs": {"image": img_name}, "class_type": "LoadImage"},
        "3": {"inputs": {"upscale_model": ["1", 0], "image": ["2", 0]}, "class_type": "ImageUpscaleWithModel"},
        "4": {"inputs": {"images": ["3", 0], "filename_prefix": "upscaled/" + tag}, "class_type": "SaveImage"},
    }
    pid = _api('/api/prompt', {'prompt': wf}, log=log)['prompt_id']
    while True:
        time.sleep(3)
        h = _api('/api/history/' + pid, log=log)
        if pid not in h:
            continue
        st = h[pid].get('status', {})
        if st.get('status_str') == 'error':
            raise RuntimeError('超分失败 %s: %s' % (tag, json.dumps(st.get('messages', []), ensure_ascii=False)[:400]))
        if st.get('completed'):
            for v in h[pid].get('outputs', {}).values():
                for fn in v.get('images') or []:
                    return os.path.join(out_root, fn.get('subfolder', ''), fn['filename'])
            raise RuntimeError('超分完成但没有输出: ' + tag)


def free_comfy(log):
    """队列空闲时把模型请出显存让超分独占; 生成任务不受影响(非空则跳过)"""
    try:
        q = _api('/api/queue', timeout=10, log=log)
        if q.get('queue_running') or q.get('queue_pending'):
            log('队列非空, 跳过 /free')
            return
        req = urllib.request.Request(BASE + '/free',
                                     data=json.dumps({'unload_models': True, 'free_memory': True}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        urllib.request.urlopen(req, timeout=60).read()
        log('comfy /free 已发, 等显存落地')
        time.sleep(8)
    except Exception as e:
        log('comfy free 失败(继续): %s' % e)


def _try_ncnn(src, dst, fps, tw, th, log, status):
    """ncnn-vulkan 目录批量x4 -> lanczos 收放到 tw x th(保源比例) -> 贴原音轨。成功返回 True, 不可用/失败返回 False 走旧链路"""
    if not os.path.exists(NCNN):
        return False
    work = tempfile.mkdtemp(prefix='up720nc_')
    try:
        bak = os.path.join(work, 'src' + os.path.splitext(src)[1])
        shutil.copy(src, bak)   # 踩坑2: 源片开跑即备份, 用户中途删片不致整段白跑
        frames = os.path.join(work, 'f')
        ups = os.path.join(work, 'up')
        os.makedirs(frames)
        os.makedirs(ups)
        # 抽帧命名用纯序号: ncnn 目录模式输出帧名=输入帧名, 合帧 glob 才能对上(10-12 e2e 踩坑)
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
                        '-q:v', '1', os.path.join(frames, '%05d.jpg')], check=True)
        total = len(os.listdir(frames))
        if status is not None:
            status.update(phase='upscale', frame=0, total=total)
        log('ncnn-vulkan x4 批量超分 %d 帧（绕开 ComfyUI 队列, 模型只载一次）…' % total)
        t0 = time.time()
        r = subprocess.run([NCNN, '-i', frames, '-o', ups, '-s', '4', '-f', 'jpg'],
                           capture_output=True, timeout=7200)
        done = len(os.listdir(ups)) if os.path.isdir(ups) else 0
        if r.returncode != 0 or done < total:
            log('ncnn-vulkan 中断(%d/%d帧, 回退ComfyUI逐帧链路): %s'
                % (done, total, r.stderr.decode('utf-8', 'replace')[-150:]))
            return False
        log('  x4 超分完成 %d 帧 | %.0fs' % (total, time.time() - t0))
        log('lanczos 收放 %dx%d + 贴原音轨封装中…' % (tw, th))
        # 音轨一律转 aac(踩坑2); 输出帧名是纯序号 %05d.jpg(目录模式无前缀, 10-12 实测)
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-framerate', str(fps), '-i', os.path.join(ups, '%05d.jpg'),
                        '-i', bak, '-map', '0:v', '-map', '1:a?',
                        '-vf', 'scale=%d:%d:flags=lanczos' % (tw, th),
                        '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-b:a', '192k', '-shortest', dst], check=True)
        log('超分完成 -> %s (%.1fMB)' % (dst, os.path.getsize(dst) / 1048576))
        return True
    except Exception as e:
        log('ncnn-vulkan 路径异常(回退ComfyUI逐帧链路): %s' % e)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def upscale(src, dst, out_root, log=print, status=None):
    """src mp4 -> dst 超分mp4(长边1920, 保源比例)。status 为 dict 时回写 phase/frame/total 供 /upscale_progress 查询"""
    fps = _probe_fps(src)
    sw, sh = _probe_wh(src)
    tw, th = _target_for(sw, sh)
    log('超分开始: %s fps=%.3f 源=%dx%d -> 目标=%dx%d -> %s' % (os.path.basename(src), fps, sw, sh, tw, th, dst))
    if status is not None:
        status.update(phase='upscale', frame=0, total=0)
    if _try_ncnn(src, dst, fps, tw, th, log, status):
        if status is not None:
            status.update(phase='done', running=False)
        return dst
    # ---- 回退: ComfyUI RealESRGAN_x4plus 逐帧链路(ncnn 不可用/失败时, 亦供无独显环境) ----
    work = tempfile.mkdtemp(prefix='up720_')
    try:
        frames = os.path.join(work, 'f')
        up = os.path.join(work, 'up')
        os.makedirs(frames)
        os.makedirs(up)
        # 先把源片备份进临时目录: 出片后用户可能立刻删掉"小成片", 最后贴音轨还要读源片(10-12实测踩坑)
        bak = os.path.join(work, 'src' + os.path.splitext(src)[1])
        shutil.copy(src, bak)
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
                        '-q:v', '1', os.path.join(frames, 'f_%05d.jpg')], check=True)
        names = sorted(os.listdir(frames))
        total = len(names)
        if status is not None:
            status.update(phase='upscale', frame=0, total=total)
        log('抽帧 %d 帧, 逐帧 x4 超分中...' % total)
        free_comfy(log)
        t0 = time.time()
        _tag = os.path.splitext(os.path.basename(src))[0].strip('_').replace(' ', '_')
        folder = 'up720_%s_%s' % (_tag, time.strftime('%m%d_%H%M%S'))  # 一轮一文件夹, 同轮 158/192 帧全部归位
        for i, fn in enumerate(names):
            cname = _upload(os.path.join(frames, fn), folder)
            opath = _upscale_frame(cname, 'f%05d' % i, out_root, log)
            shutil.copy(opath, os.path.join(up, 'f_%05d.png' % i))
            try:
                os.remove(opath)   # 防 output/upscaled 堆几百MB PNG
            except OSError:
                pass
            if status is not None:
                status['frame'] = i + 1
            if (i + 1) % 48 == 0 or i + 1 == total:
                el = time.time() - t0
                log('  超分 %d/%d | %.0fs | ETA %.0fs' % (i + 1, total, el, el / (i + 1) * (total - i - 1)))
        if status is not None:
            status['phase'] = 'mux'
        log('lanczos 收放 %dx%d + 贴原音轨封装中...' % (tw, th))
        # 音轨一律转 aac: -c:a copy 遇到源片怪编码会在封装步整段崩(10-11 ffmpeg exit 4294967294 实测)
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-framerate', str(fps), '-i', os.path.join(up, 'f_%05d.png'),
                        '-i', bak, '-map', '0:v', '-map', '1:a?',
                        '-vf', 'scale=%d:%d:flags=lanczos' % (tw, th),
                        '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-b:a', '192k', '-shortest', dst], check=True)
        log('超分完成 -> %s (%.1fMB)' % (dst, os.path.getsize(dst) / 1048576))
        if status is not None:
            status.update(phase='done', running=False)
        return dst
    except Exception:
        if status is not None:
            status.update(phase='error', running=False)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    import sys
    _src = sys.argv[1]
    _dst = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(_src)[0] + '_1080p.mp4'
    upscale(_src, _dst, os.environ.get('H3_OUTPUT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ComfyUI', 'output')))
