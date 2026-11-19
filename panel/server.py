# server.py - H3 极简面板:静态页 + 代理转发到 ComfyUI(8188)
# 用 pythonw 后台运行,监听 8199
import http.server
import urllib.request
import urllib.error
import urllib.parse
import os
from pathlib import Path
import sys
import re
import json
import time
import shutil
import tempfile
import threading
import datetime
import subprocess
import base64

DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- 路径配置（跨机器/跨平台） ----------
# 优先级：环境变量 > panel_config.json > 默认值（同级 ../ComfyUI）
CONF = {}
_cf = os.path.join(DIR, 'panel_config.json')
if os.path.isfile(_cf):
    try:
        with open(_cf, encoding='utf-8') as _f:
            CONF = json.load(_f)
    except Exception:
        CONF = {}


def _cfg(env_name, key, default):
    return os.environ.get(env_name) or CONF.get(key) or default


COMFY_ROOT = _cfg('H3_COMFY_ROOT', 'comfy_root',
                  os.path.normpath(os.path.join(DIR, '..', 'ComfyUI')))
UPSTREAM = _cfg('H3_COMFY_URL', 'comfy_url', 'http://127.0.0.1:8188')
PORT = int(_cfg('H3_PANEL_PORT', 'panel_port', 8199))
BIND = _cfg('H3_PANEL_BIND', 'panel_bind', '127.0.0.1')
SERVER_LOG = _cfg('H3_SERVER_LOG', 'server_log',
                  os.path.join(COMFY_ROOT, '_deploy', 'server.log'))

# 本机回环地址强制直连：绕过系统代理(Clash/VPN 关开后 urllib 走死代理会 10061)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

os.chdir(DIR)

OUT_ROOT = _cfg('H3_OUTPUT_DIR', 'output_dir', os.path.join(COMFY_ROOT, 'output'))
IN_ROOT = _cfg('H3_INPUT_DIR', 'input_dir', os.path.join(COMFY_ROOT, 'input'))
VENV_PY = _cfg('H3_PYTHON', 'python', sys.executable)
FACE_RESTORE = os.path.join(DIR, 'face_restore.py')
TRASH_DIR = os.path.join(OUT_ROOT, '_trash')  # 删除先进回收站, 7天后自动清(防误触✕/清理全部丢片)
TRASH_KEEP_DAYS = 7

import upscale720  # 720p 超分合成(0.9MP直出必崩, 改0.4MP生成+Real-ESRGAN超分)
UPSCALE_STATUS = {'running': False, 'phase': '', 'frame': 0, 'total': 0, 'name': '', 'msg': '', 'ts': 0}
_upscale_lock = threading.Lock()

# ---------- 视频混剪（可选：MoneyPrinterTurbo + 本地生图网关） ----------
# 不配置就只显示“未安装”，不影响 H3 出片主链路
MPT_DIR = _cfg('H3_MPT_DIR', 'mpt_dir', '')
MPT_PORT = int(_cfg('H3_MPT_PORT', 'mpt_port', 8501))
MPT_URL = f'http://127.0.0.1:{MPT_PORT}'
GATEWAY_SCRIPT = _cfg('H3_GATEWAY_SCRIPT', 'gateway_script', '')
GATEWAY_PORT = int(_cfg('H3_GATEWAY_PORT', 'gateway_port', 8089))
CANVAS_DIR = _cfg('H3_CANVAS_DIR', 'canvas_dir', '')


def _port_open(port, host='127.0.0.1'):
    import socket
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def mixcut_status():
    return {
        'mixcut': _port_open(MPT_PORT),
        'gateway': _port_open(GATEWAY_PORT),
        'url': MPT_URL,
    }


def start_mixcut():
    """缺什么补什么：先拉生图网关(8089)，再拉混剪服务(8501)。已跑的不重复拉。"""
    out = {'ok': True, 'started': []}
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    if not _port_open(GATEWAY_PORT):
        if not os.path.isfile(GATEWAY_SCRIPT):
            out['ok'] = False
            out['error'] = f'找不到生图网关: {GATEWAY_SCRIPT}'
            return out
        # 用 ComfyUI 的 venv 解释器：面板自身跑在 Store 版 pythonw 下，
        # 该别名进程拉起子进程会静默失败（2026-11-04 实测 8089 起不来）
        py = VENV_PY if os.path.isfile(VENV_PY) else sys.executable
        gw_log = open(os.path.join(DIR, 'mixcut_gateway.log'), 'a', encoding='utf-8', errors='replace')
        subprocess.Popen([py, GATEWAY_SCRIPT, '--port', str(GATEWAY_PORT)],
                         cwd=os.path.dirname(GATEWAY_SCRIPT) or DIR,
                         creationflags=flags, stdout=gw_log, stderr=subprocess.STDOUT)
        out['started'].append('gateway')
    if not _port_open(MPT_PORT):
        exe = os.path.join(MPT_DIR, '.venv', 'Scripts', 'streamlit.exe')
        if not os.path.isfile(exe):
            out['ok'] = False
            out['error'] = f'找不到 {exe}'
            return out
        logf = open(os.path.join(MPT_DIR, 'mixcut_panel.log'), 'a', encoding='utf-8', errors='replace')
        subprocess.Popen(
            [exe, 'run', 'webui/Main.py', '--server.port', str(MPT_PORT),
             '--server.address', '127.0.0.1', '--server.headless', 'true'],
            cwd=MPT_DIR, creationflags=flags, stdout=logf, stderr=subprocess.STDOUT)
        out['started'].append('mixcut')
    out['status'] = mixcut_status()
    return out



def move_to_trash(p):
    os.makedirs(TRASH_DIR, exist_ok=True)
    dst = os.path.join(TRASH_DIR, os.path.basename(p))
    if os.path.exists(dst):
        base, ext = os.path.splitext(os.path.basename(p))
        dst = os.path.join(TRASH_DIR, f'{base}_{int(time.time()*1000)}{ext}')
    os.replace(p, dst)


def purge_trash():
    try:
        cutoff = time.time() - TRASH_KEEP_DAYS * 86400
        for fn in os.listdir(TRASH_DIR):
            p = os.path.join(TRASH_DIR, fn)
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
    except Exception:
        pass


def comfy_queue_empty():
    """ComfyUI 队列是否空闲(没开=没占显存, 也算空闲)"""
    try:
        q = json.load(_opener.open(urllib.request.Request(UPSTREAM + '/api/queue'), timeout=5))
        return len(q.get('queue_running', [])) + len(q.get('queue_pending', [])) == 0
    except Exception:
        return True


def comfy_free():
    """点优化前调用: 让 ComfyUI 把模型清出显存, Ollama 就能吃满整块显卡(优化 40s 级).
    生成中绝不调用(comfy_queue_empty 兜底)——代价是优化后下一次生成要重装模型(约 1~2 分钟)"""
    if not comfy_queue_empty():
        return
    try:
        req = urllib.request.Request(UPSTREAM + '/free',
                                     data=json.dumps({'unload_models': True, 'free_memory': True}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        _opener.open(req, timeout=60).read()
        log('comfy free: 模型已清出显存, 优化优先')
    except Exception as e:
        log(f'comfy free err: {e}')


def wait_vram_free(threshold_gb=10, timeout=90):
    """ comfy_free 后等显存真正落地再让 Ollama 加载。
    WDDM 下显存回收异步: /free 返回≠显存可分配, 立刻加载会让 qwen 只抢到零点几G(半吊子实例10分钟级) """
    t0 = time.time()
    first = True
    while time.time() - t0 < timeout:
        try:
            st = json.load(_opener.open(urllib.request.Request(UPSTREAM + '/api/system_stats', timeout=5)))
            first = False
            dev = (st.get('devices') or [{}])[0]
            free = (dev.get('vram_free') or 0) / 2**30
            if free >= threshold_gb:
                log(f'vram ready: free={free:.1f}G')
                return True
        except Exception:
            if first:   # ComfyUI 根本没开(非生成中): 显存天然空闲, 别空等90s
                log('vram wait: ComfyUI 不可达, 跳过等待')
                return True
        first = False
        time.sleep(2)
    log(f'vram wait timeout({timeout}s), 继续执行')
    return False


def extract_lastframe_b64(video_name):
    """对 output/video 下的成片提真尾帧(纯CPU秒级), 返回 base64 供反馈重写时给 VLM 看生成结果"""
    if '/' in video_name or '\\' in video_name or '..' in video_name:
        raise ValueError('bad video name')
    src = os.path.join(OUT_ROOT, 'video', video_name)
    if not os.path.isfile(src):
        raise ValueError('成片不存在：' + video_name)
    os.makedirs(os.path.join(OUT_ROOT, '_frame_tmp'), exist_ok=True)
    out = os.path.join(OUT_ROOT, '_frame_tmp', f'fb_{int(time.time()*1000)}.jpg')
    r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-sseof', '-1', '-i', src, '-update', '1', '-q:v', '1', out],
                       capture_output=True, timeout=120)
    if r.returncode != 0 or not os.path.isfile(out):
        raise ValueError('尾帧提取失败：' + r.stderr.decode('utf-8', 'replace')[-200:])
    with open(out, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    try:
        os.remove(out)
    except OSError:
        pass
    return b64


def ollama_unload():
    """点生成前调用: 把 Ollama 模型秒级请出显存, 生成吃满 16GB 满血跑"""
    for m in ('qwen3-vl:8b', 'qwen2.5vl:7b', 'qwen3.5:9b'):
        try:
            req = urllib.request.Request('http://127.0.0.1:11434/api/generate',
                                         data=json.dumps({'model': m, 'keep_alive': 0}).encode(),
                                         headers={'Content-Type': 'application/json'}, method='POST')
            _opener.open(req, timeout=10).read()
        except Exception:
            pass


def _history_video(h, prompt_id):
    """成片定位: VHS VideoCombine 落在 outputs.gifs, 原生 SaveVideo(PreviewVideo) 落在 outputs.images——两边都扫"""
    for v in h[prompt_id].get('outputs', {}).values():
        for key in ('gifs', 'images'):
            for g in v.get(key) or []:
                fn = g.get('filename') or ''
                if fn.lower().endswith(('.mp4', '.webm', '.mkv')):
                    return os.path.join(OUT_ROOT, g.get('subfolder') or 'video', fn)
    return None


def upscale_watcher(prompt_id):
    """高清(0.9MP)请求: H3按0.4MP生成完成后, 自动Real-ESRGAN逐帧超分合成1080p, 产物 <原名>_1080p.mp4"""
    with _upscale_lock:
        UPSCALE_STATUS.update(running=True, phase='wait_gen', frame=0, total=0, name='', msg='', ts=time.time())
        try:
            src = None
            t0 = time.time()
            while time.time() - t0 < 3 * 3600:
                time.sleep(15)
                try:
                    h = json.load(_opener.open(urllib.request.Request(UPSTREAM + '/api/history/' + prompt_id), timeout=30))
                except Exception:
                    continue
                if prompt_id not in h:
                    continue
                st = h[prompt_id].get('status', {})
                if st.get('status_str') == 'error':
                    log(f'upscale skip: 生成失败 pid={prompt_id[:8]}')
                    UPSCALE_STATUS.update(phase='error', msg='生成失败', running=False, ts=time.time())
                    return
                if st.get('completed'):
                    src = _history_video(h, prompt_id)
                    if not src:
                        keys = {k: list(v.keys()) for k, v in h[prompt_id].get('outputs', {}).items()}
                        log(f'upscale: _completed但定位不到mp4, outputs键={json.dumps(keys, ensure_ascii=False)[:300]}')
                    break
            if not src or not os.path.isfile(src):
                log(f'upscale skip: 没找到成片 pid={prompt_id[:8]}')
                UPSCALE_STATUS.update(phase='error', msg='没找到成片', running=False, ts=time.time())
                return
            base = os.path.splitext(os.path.basename(src))[0]
            dst = os.path.join(OUT_ROOT, 'video', base + '_1080p.mp4')
            UPSCALE_STATUS['name'] = os.path.basename(dst)
            if os.path.exists(dst):
                move_to_trash(dst)  # 防旧文件造成"秒完成"错觉
            log(f'upscale start: {os.path.basename(src)} -> {os.path.basename(dst)}')
            upscale720.upscale(src, dst, OUT_ROOT, log=log, status=UPSCALE_STATUS)
        except Exception as e:
            log(f'upscale err: {e}')
        finally:
            UPSCALE_STATUS.update(running=False)


def recent_outputs():
    """断点找回: ComfyUI 是否在线 + 正在生成的任务数 + output 下按时间排序的最近成片"""
    purge_trash()
    out = {'busy': 0, 'items': [], 'up': False, 'total_mb': 0}
    try:
        q = json.load(_opener.open(urllib.request.Request(UPSTREAM + '/api/queue'), timeout=5))
        out['busy'] = len(q.get('queue_running', [])) + len(q.get('queue_pending', []))
        out['up'] = True
    except Exception:
        pass
    items = []
    for sub in ('video', 'image'):
        d = os.path.join(OUT_ROOT, sub)
        try:
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if os.path.isfile(p) and not fn.endswith(('.tmp', '.merging')):
                    items.append({'name': fn, 'sub': sub, 't': os.path.getmtime(p),
                                  'sz': os.path.getsize(p)})
        except Exception:
            pass
    out['total_mb'] = round(sum(i['sz'] for i in items) / 1048576, 1)
    items.sort(key=lambda x: -x['t'])
    out['items'] = [{'name': i['name'], 'sub': i['sub'],
                     'size_mb': round(i['sz'] / 1048576, 1)} for i in items[:8]]
    return out


def delete_all_outputs():
    """清理 output 下全部成片(移入回收站), 返回数量与体积"""
    n, freed = 0, 0
    for sub in ('video', 'image'):
        d = os.path.join(OUT_ROOT, sub)
        try:
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if os.path.isfile(p) and not fn.endswith(('.tmp', '.merging')):
                    freed += os.path.getsize(p)
                    move_to_trash(p)
                    n += 1
        except Exception:
            pass
    return n, round(freed / 1048576, 1)

# --- WebSocket 实时进度（直连 ComfyUI /ws，与启动方式无关） ---
LATEST = {}
# --- 进度归属跟踪（2026-10-12 修复：并行短任务(步数AB测试等)的 progress 曾污染面板, 全程误显"收尾中"）---
CURRENT_PID = None   # 面板 /api/prompt 代理提交、正在跟踪的生成任务 prompt_id
TASK_T0 = 0.0        # 该任务提交时刻：日志兜底用 elapsed 校验 tqdm 行归属
PROGRESS_AT = 0.0    # 最后一个 progress 时刻≈采样完成：ws 缓存新鲜度校验 + 收尾耗时起点


def track_prompt(proxy_data):
    """记录当前生成任务的 prompt_id: ws 进度按它过滤, 日志兜底按 TASK_T0 校验(10-12 修复)"""
    try:
        pid = json.loads(proxy_data).get('prompt_id')
    except Exception:
        return
    if not pid:
        return
    global CURRENT_PID, TASK_T0
    CURRENT_PID, TASK_T0 = pid, time.time()
    LATEST.clear()

OPT_SYSTEM = """你是 MiniMax H3 视频生成模型的提示词优化器。用户给一段大白话创意,你输出优化结果,严格 JSON:
{"duration": 数字(秒,1-15), "clarity": "0.2"或"0.4", "ratio": "auto"|"16:9"|"4:3"|"1:1"|"3:4"|"9:16", "has_person": true或false(画面是否含人物), "prompt": "成片提示词(固定段落: 场景/动作节拍/镜头/风格色调/声音等,段间空一行,见排版铁律)"}

优化规则:
- 【最高铁律】以用户原文的最终意思为最高准绳: 只优化表达与细节, 必须完整保留原文全部内容、核心想法与最终意图, 严禁抛弃、篡改、弱化或替换成别的内容
- 【动作保真·与最高铁律同级】用户点名的每个动作与事件(特别是穿脱衣物/换装/摘帽/换装展示等穿搭变化)都是核心情节,必须原样写进 prompt 相应节拍,严禁因用词敏感、觉得不合适或任何理由省略、弱化、改写或跳过
- 【动作清单零丢失机制·执行顺序第一步】写 prompt 之前,先把用户原文拆成动作/事件清单,逐条保留用户原词(如"穿上一件外套"就是一条独立条目,不许并入别的拍);清单上每一条必须原样落进【动作节拍】的某一拍——用用户原词或更具体的近义动作,严禁抽象成"整理衣物/收拾一下/换了身衣服"这类抹掉具体动作的说法;【动作节拍】拍数必须≥清单条数;duration 必须装得下全部拍
- 【方向铁律·最高优先级】用户说的内容方向(谁/在哪/做什么/情节发展/结局走向)不可更改:只允许优化表达方式(补画面细节/镜头/光线/声音/质感),严禁新增用户没说的情节、严禁删减用户说的关键情节、严禁改变动作顺序或结局;任何发挥必须服从用户原意,用户没说的戏一律不许编
- 叙述节奏铁律: 【动作节拍】段严格按用户情节的动作发生顺序组织,用时间过渡词(开始/随后/紧接着/最后)标出节拍;【声音】段的每条声音与对应节拍呼应;严禁打乱顺序,严禁自行加戏
- 完整性铁律: 输出前在脑内逐条点名用户描述里的每个动作和结局(如用户说了3步就必须有3拍),每一拍都要在 prompt 里出现,一个都不许漏、不许合并省略;宁可少写修饰词也不能丢用户的情节
- 【固定段落铁律·2026-10-13】prompt 必须按以下固定段落输出,每段以【】段名开头,全部段落缺一不可,用户没提的段落按画面合理补全,严禁省略任何一段:
  【主体】含人物时: 外貌特征/发型发色/服装/身形(文生视频按设定编定并固定;图生视频从首帧图读出来照写);一致性约束不许写进本段,只写外貌本身
  【场景】时间(时段+光线方向与色温)+地点+环境陈设(写出3~5个看得见的具体物件)+天气或季节+整体氛围——至少两句,严禁一句带过
  【动作节拍】每1.5~3秒一拍,拍数=动作清单条数(只许多不许少),每拍写清: 动作主体部位+路径方向+速度力度+幅度+伴随的衣物摆动/发丝飘动/表情反应;用时间过渡词标节拍
  【分镜头】按你对情节节奏的理解把全片切成2~4个镜头: 每镜写"镜头N(起~止秒): 景别+本镜画面要点";时间区间从0秒无缝铺到duration,各镜秒数由你按节奏分配(慢铺垫给长、激烈动作给短),全部镜头秒数相加必须等于duration
  【镜头】整体景别基调+最多一种主运镜
  【风格色调】2~3个质感词+整体色调氛围
  【声音】每篇必须出现(铁律,用户没提也必须写),分三层写具体: ①底层环境音(整场持续,写明具体声源)②动作伴随音(逐条与动作节拍对应)③配乐(写明有无,有则写曲风/乐器/情绪);严禁只写"伴有音效/环境音"这类空话
  含人物时再加【人脸一致性】【人体结构】两段约束(见下方硬性要求)
- 【审美锚点·画面含人物时默认生效,2026-10-12二次强化】清纯邻家风东亚审美: 人物为东亚中国年轻面孔,18-22岁少女感;皮肤白皙如牛奶(冷白/透亮/哑光),暖光或户外场景也保持白皙;身材纤细单薄,手臂与腿部线条柔和圆润如少女,肩背薄;整体色调清新淡雅低饱和,自然暖白光线,日系居家写真质感。硬性禁止(任何情况下不得出现): 严禁欧美化五官与深轮廓、严禁小麦色/古铜色肌肤、严禁成熟性感风、严禁明显肌肉线条与健身感
- 时长: 若用户在消息里指定了时长,duration 必须原样返回该值; 用户没指定时: 按动作清单估时长(1个动作=3秒, 2拍=5秒, 3拍=8-10秒, 拍更多按比例再加); 【铁律】duration与拍数必须服从动作清单——装不下就加拍到每拍1.5秒或延长duration(上限15秒),严禁删减/合并用户动作来凑时长
- 用户原文已带时间轴标记(如"0-3s"/"前2秒"/分条描述)时,保留该结构与标记,只做润色不重组
- 低分辨率画面避免: 多人同框、快速运动、精细文字、复杂手部特写
- prompt 写法: 按固定段落顺序输出,不再主体前置/风格收尾; 每段写具体能看见或听见的细节(材质/纹理/声源),方便用户逐段直接修改
- 【排版铁律】prompt 每行一个语义块(每个【】段一行、【动作节拍】段每拍一行),行与行之间必须空一行(\\n\\n连接)——包括每拍动作之间;任何两行不许紧挨着,用户要看着清爽、能逐段直接改;行内仍是连贯中文
- clarity 默认"0.2"; 用户提到高清/成品/细节丰富时用"0.4"; 用户明确要720p/最高清/大片级时用"0.9"(0.9MP直出必崩,服务端自动按0.4MP生成+Real-ESRGAN超分合成1080p,总耗时约生成16分+超分5分,用户没提不要主动给)
- 图生视频: 主体外观/服装/场景以首帧图为准,但"做什么动作/发生什么情节"以用户文字为准——图里没有而用户要求的动作必须照写,图里有的而用户没提的不要乱加戏; prompt 侧重什么在动、怎么动
- 【硬性要求·最高优先级】人物人脸统一性: 画面含人物时, prompt 字段本身必须包含一段**详细**的面部一致性约束(不是一句话带过), 包含三层:
  ① 总约束: "同一个人物,全片面部特征保持完全一致,不变脸"
  ② 特征锁定清单: 逐一写明 4-6 个具体面部特征——脸型、眼型、鼻型、唇形、眉形、肤色、发型发色、明显标记(痣/疤痕/眼镜等), 文生视频由你按人物设定编定并固定下来, 图生视频必须从首帧图里读出来照着写
  ③ 动态约束: "每个镜头下五官形状、相对位置、面部比例、年龄感保持一致; 表情和头部角度可以变化, 但面部结构不变; 不出现面部漂移、换脸、五官变形、特征增减; 非老年人设定时面部不出现法令纹等显老纹路, 若人物本身有法令纹, 其深浅和位置全片保持一致"
  图生视频的总约束改为: "严格保持与首帧图同一人物、同一面部特征和发型, 五官不漂移不变形"
  该段约束不可省略、不可精简成一句、不可只写进 storyboard
- 【硬性要求·最高优先级】人体结构一致性: 画面含人物时, prompt 字段本身必须包含一段**详细**的人体解剖结构校准约束(不是一句话带过), 包含三层:
  ① 总约束: "人体解剖结构自然准确, 全片身体比例与器官位置保持正常, 不出现身体畸形"
  ② 部位锁定清单: 逐一写明——头颈肩连接自然、躯干与四肢比例正常、胸部位置与形态符合正常人体解剖且随动作自然变化、四肢数量正确关节位置正常、手部五指完整指节自然、腰臀曲线自然不扭曲
  ③ 动态约束: "运动全程结构稳定: 无器官漂移、无肢体数量增减、无关节反折、无躯干扭曲, 胸部不位移、不畸形、不忽大忽小"
  图生视频的总约束改为: "严格保持与首帧图同一身体结构, 躯干四肢比例、胸部位置与形态、手部完整性全片与首帧一致"
  该段约束不可省略、不可精简成一句、不可只写进 storyboard
- 只输出 JSON, 不要任何解释
- 【输出前自检·最后一步】先把动作清单逐条与prompt比对,每条必须能指出它落在【动作节拍】哪一行(或【分镜头】哪一镜),找不到的立即加拍补写,禁止"觉得已经写了"式默认通过;再核对:固定段落一段不缺、【声音】段三层都写具体、【分镜头】2~4个且时间铺满总和==duration、每行之间都空了一行(\\n\\n),没做到的先改完再输出"""

OPT_SYSTEM_T2I = """你是 Z-Image 文生图模型的提示词优化器。用户给一句大白话画面想法,你输出优化结果,严格 JSON:
{"prompt": "成片提示词"}

优化规则:
- 【最高铁律】以用户原文的最终意思为最高准绳: 只优化表达与细节, 必须完整保留原文全部内容、核心想法与最终意图, 严禁抛弃、篡改、弱化或替换成别的内容
- 【动作保真·与最高铁律同级】用户点名的画面元素与动作(特别是穿脱衣物/换装/摘帽等穿搭变化)必须原样保留进 prompt,严禁因用词敏感或任何理由省略、弱化、改写
- 【方向铁律】画面主体与场景以用户描述为准:只补细节和质感,严禁新增用户没说的主体、道具或场景元素,严禁改变用户指定的姿态/构图核心
- 【固定段落铁律·2026-10-13】prompt 必须按以下固定段落输出,每段以【】段名开头,缺一不可,用户没提的段落按画面合理补全:
  【主体】外观/姿态/数量(含人物时: 发型发色/服装/身形按审美锚点编定)
  【场景】时间(时段+光线)+地点+环境陈设(3~5个看得见的具体物件)+天气或季节——至少两句,严禁一句带过
  【构图】景别+视角(特写/全身/俯视等)
  【风格色调】风格(写实/插画/水彩/电影感等2~3个词)+色调氛围
- 【审美锚点·画面含人物时默认生效,2026-10-12二次强化】清纯邻家风东亚审美: 东亚中国年轻面孔,18-22岁少女感,面部轮廓圆润饱满有胶原蛋白感;皮肤必须白皙(冷白/牛奶肌/透亮但哑光),绝不出现小麦色/古铜/蜜糖/晒黑肤色,暖光与户外场景同样保持白皙;身材纤细单薄少女感,手臂与腿部线条柔和圆润,无肌肉隆起、无马甲线、无健身训练感,肩背薄;整体色调清新淡雅低饱和,自然暖白光线,日系居家写真质感。明确排除欧美默认审美: 健美肌肉线条/小麦色古铜肤色/油光厚修容/高对比浓郁调色。硬性禁止(任何情况下不得出现): 严禁欧美化五官与深轮廓、严禁小麦色/古铜色肌肤、严禁成熟性感风、严禁明显肌肉线条与健身感
- 按固定段落顺序输出,不再主体前置/风格收尾; 细节词落在能看见的东西上(材质/纹理/光影), 不堆砌空泛形容词
- 静态画面: 只写"定格的一瞬", 不写动作发展过程
- 低质量陷阱规避: 多人同框逐个写清位置关系; 避免精细文字/复杂手指除非用户点名
- prompt 用中文输出, 50~120 字为宜
- 人体结构准确: 含人物时写清头颈肩/躯干/四肢/手部结构自然、比例正确, 胸部等器官位置形态符合正常人体解剖, 无多余肢体、无畸形
- 【排版铁律】prompt 每行一个【】段,行与行之间必须空一行(\\n\\n连接),不许紧挨着,用户要看着清爽、能逐段直接改
- 【输出前自检·最后一步】输出前逐条点名用户原文里的每个元素与动作(特别是换装/穿脱衣物类),发现漏写立即补齐;再核对固定段落一段不缺、每行之间空了一行(\\n\\n)再输出
- 只输出 JSON, 不要任何解释"""

OPT_SYSTEM_R2V = """你是 MiniMax H3 参考生视频模型的提示词优化器。用户上传了真人照片锁定人物长相与穿着,并给一句大白话创意,你输出优化结果,严格 JSON:
{"duration": 数字(秒,1-15), "clarity": "0.2"或"0.4", "ratio": "auto"|"16:9"|"4:3"|"1:1"|"3:4"|"9:16", "has_person": true或false, "prompt": "成片提示词(一段连贯中文,含画面/动作/声音)"}

优化规则:
- 【最高铁律】以用户原文的最终意思为最高准绳: 只优化表达与细节, 必须完整保留原文全部内容、核心想法与最终意图, 严禁抛弃、篡改、弱化或替换成别的内容
- 【动作保真·与最高铁律同级】用户点名的每个动作(特别是穿脱衣物/换装/摘帽等穿搭变化)都是核心情节,必须写进 prompt 相应节拍,严禁因用词敏感或任何理由省略弱化;本条与"不描述外貌"不冲突:动作必须写,外貌仍不描述
- 【动作清单零丢失机制·执行顺序第一步】写 prompt 之前,先把用户原文拆成动作/事件清单,逐条保留用户原词(如"穿上一件外套"就是一条独立条目);清单上每一条必须原样落进【动作节拍】的某一拍——用用户原词或更具体的近义动作,严禁抽象成"整理衣物/换了身衣服"这类抹掉具体动作的说法;【动作节拍】拍数必须≥清单条数;duration 必须装得下全部拍
- 【方向铁律·最高优先级】用户说的内容方向(做什么/情节发展/结局走向/换不换场景)不可更改:只允许优化表达方式,严禁新增用户没说的情节、严禁删减用户说的关键情节、严禁改变动作顺序或结局;任何发挥必须服从用户原意
- 叙述节奏铁律: 【动作节拍】段严格按用户情节的动作发生顺序组织,用时间过渡词标出节拍;【声音】段的每条声音与对应节拍呼应
- 完整性铁律: 输出前逐条点名用户描述的每个动作和结局,每一拍都要出现在 prompt 里,一个不许漏、不许合并省略
- 用户原文已带时间轴标记(如"0-3s"/"前2秒"/分条描述)时,保留该结构与标记,只做润色不重组
- 【固定段落铁律·2026-10-13】prompt 必须按以下固定段落输出,每段以【】段名开头,缺一不可,用户没提的段落按画面合理补全:
  【场景】默认沿用参考照片里的环境,用户明确要求换场景时才写新场景;写清时间(时段+光线)+环境陈设(3~5个看得见的具体物件)+整体氛围——至少两句,严禁一句带过
  【动作节拍】每1.5~3秒一拍,拍数=动作清单条数(只许多不许少),每拍写清: 动作主体部位+路径方向+速度力度+幅度+伴随的衣物摆动/发丝飘动/表情反应;用时间过渡词标节拍
  【分镜头】按你对情节节奏的理解把全片切成2~4个镜头: 每镜写"镜头N(起~止秒): 景别+本镜画面要点";时间区间从0秒无缝铺到duration,各镜秒数由你按节奏分配(慢铺垫给长、激烈动作给短),全部镜头秒数相加必须等于duration
  【镜头】整体景别基调+最多一种主运镜
  【风格色调】2~3个质感词+渲染色调(默认清纯邻家低饱和)
  【声音】每篇必须出现(铁律,用户没提也必须写),分三层写具体: ①底层环境音(写明具体声源)②动作伴随音(逐条与动作节拍对应)③配乐(写明有无,有则写曲风/乐器/情绪);严禁只写"伴有音效/环境音"这类空话
- 【审美锚点·渲染风格默认生效,2026-10-12二次强化】人物长相穿着以参考照片为准(仍不许文字描述),但渲染审美默认清纯邻家风: 皮肤白皙如牛奶(冷白/哑光),面部与身体轮廓圆润柔和,身材纤细单薄,手臂与腿部线条如少女般柔和圆润;整体色调清新淡雅低饱和,自然暖白光线,日系居家写真质感。硬性禁止(任何情况下不得出现): 严禁欧美化五官与深轮廓、严禁小麦色/古铜色肌肤、严禁成熟性感风、严禁明显肌肉线条与健身感
- 【硬性要求·最高优先级】prompt 里严禁描述人物的长相、五官、发型、服装、身材——这些由参考照片锁定,文字描述反而干扰; 只写"她/他/人物"指代即可
- 【硬性要求】人体结构校准(只防畸形, 不属于描述身材外貌, 与上条不冲突): prompt 必须含"人体解剖结构自然准确, 头颈肩/躯干/四肢/手部结构正常, 胸部等器官位置形态符合正常人体解剖, 运动全程无器官漂移、无肢体增减、无关节反折、无躯干扭曲"
- 场景默认沿用参考照片里的环境; 用户明确要求换场景时才写新场景
- 时长: 若用户在消息里指定了时长,duration 必须原样返回该值; 用户没指定时: 按动作清单估时长(1个动作=3秒, 2拍=5秒, 3拍=8-10秒, 拍更多按比例再加); 【铁律】duration与拍数必须服从动作清单——装不下就加拍到每拍1.5秒或延长duration(上限15秒),严禁删减/合并用户动作来凑时长
- clarity 默认"0.2"; 用户提到高清/成品/细节丰富时用"0.4"; 用户明确要720p/最高清/大片级时用"0.9"(0.9MP直出必崩,服务端自动按0.4MP生成+Real-ESRGAN超分合成1080p,总耗时约生成16分+超分5分,用户没提不要主动给)
- prompt 写法: 按固定段落顺序输出; 每段写具体能看见或听见的细节,方便用户逐段直接修改
- 【排版铁律】prompt 每行一个语义块(每个【】段一行、【动作节拍】段每拍一行、结构校准约束一行),行与行之间必须空一行(\\n\\n连接),不许紧挨着,用户要看着清爽、能逐段直接修改;行内仍是连贯中文
- 【输出前自检·最后一步】先把动作清单逐条与prompt比对,每条必须能指出它落在【动作节拍】哪一行(或【分镜头】哪一镜),找不到的立即加拍补写,禁止"觉得已经写了"式默认通过;再核对固定段落一段不缺、【声音】段三层齐全、【分镜头】2~4个且时间铺满总和==duration、每行之间空了一行(\\n\\n)再输出
- 只输出 JSON, 不要任何解释"""

OPT_SYSTEM_I2E = """你是 Qwen-Image-Edit 2511 图改图模型的指令优化器。用户上传一张原图,并说一句想怎么改。你输出严格 JSON:
{"prompt": "改图指令", "prompt_zh": "这段指令的中文回述"}

优化规则:
- 【最高铁律】以用户原文的最终意思为最高准绳: 只优化表达, 必须完整保留用户想改的内容与核心想法, 严禁抛弃、篡改、弱化或替换成别的修改要求
- 【修改保真·与最高铁律同级】用户点名的修改内容(特别是换装/脱外套/摘帽/换发型等穿搭变化)必须原样出现在改图指令里,严禁因用词敏感或任何理由省略、弱化、改写
- 【修改清单零丢失机制·执行顺序第一步】写指令前先把用户原文拆成"要改的清单",逐条保留用户原词(如"把外套换成黑色皮夹克"就是一条独立条目);清单每一条必须原样落进【修改内容】段的某一行,严禁抽象成"调整一下服装/换身衣服"这类抹掉具体改法的说法;【修改内容】的行数必须≥清单条数
- 【方向铁律·最高优先级】用户说的修改方向不可更改: 只允许优化表达(补充颜色/材质/长度/位置等可执行细节),严禁新增用户没提的修改项、严禁把用户要改的改成别的东西、严禁顺手"美颜/优化/提升质感"式加戏
- 【保持不变铁律·同等优先级】除【修改内容】里点名的项目外,其余一切必须保持原图原样: 脸部、五官、脸型、肤色、表情、年龄感、发型发色(未被点名时)、姿势与身体角度、未点名的服装与配饰、背景场景、构图与画面比例、光线方向与色温、整体色调。用户没点名的地方一律不许动
- 【固定段落铁律】prompt 必须按以下四段输出,每段以【】段名开头,四段缺一不可,按此顺序:
  【修改内容】逐条写清改什么+改成什么样,一行一条,用用户原词或更具体的近义说法;每条都必须是模型能直接执行的视觉描述(颜色/材质/版型/长度/位置),不写"更好看""更自然"这类空话
  【保持不变】明确列出不许改的部分(脸/五官/表情/姿势/未点名服装配饰/背景/构图/光线/色调),一行写全
  【人体结构】修改后的人体结构约束(见下方硬性要求)
  【风格质感】原图的质感与色调延续说明(保留原图的颗粒感/光比/色温,不换风格)
- 【排版铁律】prompt 每行一个语义块,行与行之间必须空一行(\\n\\n连接),用户要能逐行直接改;【修改内容】里每条修改单独一行,严禁把多条揉成一句
- 【审美锚点·默认生效,2026-10-23】画面含人物时,皮肤质感按东亚审美处理: 白皙透亮、细腻光滑、无明显瑕疵与粗糙颗粒,但保留真实皮肤纹理(不塑料感、不磨成假人);整体色调清新淡雅低饱和,自然暖光。硬性禁止(任何情况下不得出现): 严禁欧美化五官与深轮廓、严禁小麦色/古铜色肌肤、严禁成熟性感风、严禁明显肌肉线条与健身感。边界: 本锚点只写进【风格质感】段、只提升肤质与色调,严禁借它改变脸型五官、年龄感、身材比例或原图构图——那些属于【保持不变】,用户没点名就不许动
- 【硬性要求·最高优先级】人脸保持: 只要修改涉及人物,【保持不变】段必须包含具体到可执行的人脸锁定约束,不能一句话带过,写清——①总约束"同一人物,脸部与五官与原图完全一致,不重新生成脸" ②锁定项逐一写明: 脸型、眼型、鼻型、唇形、眉形、肤色、年龄感、表情、未点名时的发型发色 ③禁止项"不换脸、不变性别年龄、不改变五官比例与相对位置、不新增或减少面部特征、不把原图人物改成另一个人"
- 【硬性要求·最高优先级】人体结构一致性: 修改涉及人物或衣物时,【人体结构】段必须包含可执行的结构约束,写清——①"保持原图同一身体结构,躯干四肢比例、肩宽、腰臀曲线与原图一致" ②部位清单: 头颈肩连接自然、四肢数量正确关节位置正常、手部五指完整指节自然、胸部位置与形态符合原图 ③禁止项"不出现新增或缺失的肢体、无关节反折、无躯干扭曲、衣物遮挡处不臆造裸露结构"
- 用户附图就是要改的原图,涉及画面内容以图为准,不要编与图不符的描述;原图里没有的元素不许凭空添加
- 修改不得破坏画面一致性: 改动区域的透视、光影方向、色温必须与原图匹配,拼接痕迹自然,不做风格化重绘
- prompt_zh 是给用户看的对照, 用中文如实复述 prompt 里写了什么(不新增英文/指令里没有的内容), 一句话概括修改项与保持项
- 只输出 JSON, 不要任何解释、不要步骤分解
- 【输出前自检·最后一步】输出前把"要改的清单"逐条与 prompt 比对,每条必须能指出它落在【修改内容】的哪一行,找不到的立即补写,禁止"觉得已经写了"式默认通过;再核对: 四段一段不缺、【保持不变】已把人脸与人体锁定写具体、每条修改单独成行且行间空行、没有用户没提的新增修改项;没做到的先改完再输出"""

OPT_SYSTEM_IID = """你是 SDXL + InstantID 保脸生图模型的英文提示词翻译器。用户上传一张人脸照片,并中文描述想要的新画面。你输出严格 JSON:
{"prompt": "英文提示词", "prompt_zh": "这段英文的中文回述"}

优化规则:
- 【最高铁律】以用户原文的最终意思为最高准绳: 只优化表达, 必须完整保留用户想表达的服装、发型、场景、动作、氛围, 严禁抛弃、篡改、弱化或替换成别的内容
- 【要素保真·与最高铁律同级】用户点名的每一项(特别是换装/穿脱衣物/发型改变等穿搭描述)必须全部翻进英文提示词,严禁因用词敏感或任何理由漏译、弱化;用户没说的一律不许加
- 【要素清单零丢失机制·执行顺序第一步】写提示词前先把用户原文拆成要素清单(服装/发型/场景/光线/动作/氛围各一条),逐条保留用户原词;清单每一条必须落进对应段落,严禁抽象成"nice outfit/lovely scene"这类抹掉细节的说法
- 【方向铁律·最高优先级】用户说的人物身份、场景、动作、氛围不可更改: 只允许补充可执行的视觉细节(材质/颜色/版型/光线方向/镜头景别),严禁新增用户没说的情节或元素,严禁把用户要的场景换成别的
- 【人脸不描述铁律】长相由 InstantID 从照片提取,提示词里严禁写任何五官细节(脸型/眼睛/鼻子/嘴唇/肤色/年龄),一律用 "a person" 指代;写了五官词会和人脸锁冲突,导致脸跑偏
- 【固定段落铁律】prompt 必须按以下五段输出,每段单独一行,缺一不可,按此顺序:
  【主体】a person + 身形/姿态/朝向 + 景别(全身/半身/特写),不写长相
  【发型服装】逐项写清发型发色发长、每件服装的版型/材质/颜色/长短、配饰;用户点名的穿搭变化必须逐条落地
  【场景光线】地点+环境陈设(3~5个看得见的具体物件)+时间与光线方向色温+天气氛围
  【动作表情】人物在做什么、身体朝向、手部位置、表情基调;动作要符合人体可行范围
  【风格质感】摄影风格+镜头(如 85mm portrait)+景深+画质词(photorealistic, detailed, film grain),控制总词数在 60 词内
- 【排版铁律】五段各占一行,行与行之间空一行(\\n\\n连接),用户要能逐段直接改;每段内用英文逗号分隔短语,不要写完整句子
- 【审美锚点·默认生效,2026-10-23】一律东亚审美: 东亚中国年轻面孔,皮肤白皙如牛奶(冷白/透亮/哑光,细腻光滑无粗糙颗粒与明显瑕疵),身材纤细单薄、肩背薄、手臂与腿部线条柔和,整体色调清新淡雅低饱和,自然暖白光线,日系居家写真质感。硬性禁止(任何情况下不得出现): 严禁欧美化五官与深眼窝高鼻梁轮廓、严禁小麦色/古铜色/日光浴肤色、严禁成熟性感风、严禁明显肌肉线条与健身感、严禁欧美浓妆网红感。注意: 长相与年龄感由 InstantID 从照片提取,【审美锚点】只写进【发型服装】【风格质感】两段,只影响肤质/身材/色调词,严禁写成五官描述(否则违反人脸不描述铁律)。对应英文词优先用: porcelain fair skin, smooth flawless skin, slim slender figure, soft natural warm light, fresh pastel tones, japanese home portrait photography
- 【硬性要求·最高优先级】人体结构: 【主体】或【动作表情】段必须包含可执行的结构约束,写清——"natural human anatomy, correct limb count and joint positions, complete five fingers on each hand, natural neck-shoulder connection, normal torso-to-limb proportion";禁止项 "no extra limbs, no fused fingers, no distorted torso"
- 身体结构准确: 四肢与手部完整, 躯干比例与器官位置自然符合人体解剖, 无畸形
- 场景与光线必须支撑人物: 光源方向明确(如 window light from the left),人物受光与背景光源方向一致,不出现多光源打架
- prompt_zh 是给用户看的对照, 用一句中文如实复述 prompt 里写了什么(不新增英文里没有的内容), 不要重复原始需求
- 只输出 JSON, 不要任何解释、不要步骤分解
- 【输出前自检·最后一步】输出前把要素清单逐条与 prompt 比对,每条必须能指出它落在哪一段哪一处,找不到的立即补写;再核对: 五段一段不缺、没有任何五官描述词、人体结构约束已写具体、每段单独成行且行间空行、没有用户没提的新增元素;没做到的先改完再输出"""

OPT_SYSTEM_V2E = """你是 MiniMax H3 视频模型的「改图」提示词优化器。用户上传一张原图,说要改成什么样;H3 以这张图为首帧生成 2 秒视频,再逐帧抽图当改图成品。你输出严格 JSON:
{"duration": 数字(秒,5-8,按内容定), "clarity": "0.2"或"0.4", "ratio": "auto"|"16:9"|"4:3"|"1:1"|"3:4"|"9:16", "has_person": true或false, "prompt": "改图提示词(固定段落,段间空一行)"}

【任务本质】原图=改之前的起点,不是终点。prompt 的唯一任务:让画面从原图状态变成用户要的最终状态,变化完成后稳定停住。用户点名的改动必须在最终画面里看得见。

【时长·按内容定】duration 不是固定值:只换一件衣服/发型/背景=5 秒;换装+一个动作=5~6 秒;换装+两三个动作或走位=6~8 秒。最短 5 秒——H3 训练下限 124 帧(约 5.2 秒),更短会让替换和动作不稳。用户已指定时长时以用户为准。

【最高铁律·改动必须落地】
- 用户说改什么,最终画面就必须变成什么:写清目标态的可执行细节(颜色/材质/版型/长度/位置/数量/穿法)。
- 服装类改动必须写三件事:①原服装现在是什么(一句,说明从哪改) ②替换成什么(款式+颜色+材质+长短+穿着状态) ③脱掉/穿上/换上的具体动作与顺序。
- 严禁只写"换了一件衣服/整理衣物/换了身搭配/调整服装"这类没有目标态的空话;严禁只写动作、不写变换后的样子。
- 用户点了动作(转身/坐下/抬手等)时动作照写,但服装目标态不许丢;先完成换装再做动作,或严格按用户给的先后顺序。

【首帧只锁人不锁衣】
- 锁:脸型五官、肤色、年龄感、身材比例、姿势(用户没让动时)、机位构图、场景陈设、光线方向色温。
- 不锁:服装(用户点名就必须换掉)、发型发色(用户点名就必须变)。
- 除用户点名的项目外其余全部与首帧一致,严禁新增用户没说的元素/道具/文字/情节。

【变化过程必须写】
- H3 从首帧开始演,不写过程模型就不会变。必须写:开头约 1 秒内(约占全片前 1/5)完成替换(按部位写顺序,连贯过渡,不闪切不叠影);之后有动作就按用户要求演,没动作就保持最终状态静止到结尾。
- 严禁写"全片每一帧都已是成品状态/绝不表现变化过程"这类让模型无法从首帧跳变的话。

【固定段落·缺一不可,每段一行,行与行之间空一行;段名必须带【】,每段写实际内容,严禁把本规则的说明文字或示例原样抄进 prompt】
【原图】一句具体的话:人物/姿势/场景 + 当前穿的什么(只作对照,例:一名年轻女性站在客厅,穿白色长袖T恤和牛仔裤,双手垂在身前)
【目标画面】逐条写改动项与目标态细节,一行一条,优先沿用用户原词
【变化过程】开头约 1 秒怎么从原样变成目标态(按部位与顺序),之后按用户要求做动作或保持静止
【保持项】脸/五官/肤色/身材/姿势/机位/场景/光线/未被点名的配饰与原图一致
【风格色调】沿用原图质感、色调、光线方向,不换风格
【声音】一句与改动匹配的声音(布料摩擦/脚步声/环境底噪),不写大段配乐
含人物时再加两段:
【人脸一致性】严格保持与首帧图同一人物:脸型、五官形状与相对位置、肤色、年龄感一致,不换脸、不漂移、不增减面部特征;发型发色只在用户点名改动时改变,未点名时与首帧一致。
【人体结构】保持首帧同一身体结构:躯干四肢比例正常,四肢数量与关节位置正确,手部五指完整;运动全程无肢体增减、无关节反折、无躯干扭曲。

【输出前自检·最后一步】
1. 用户要改的每一项,在【目标画面】里都能指出对应一行,指不出的立即补。
2. 【目标画面】与【原图】必须明显不同:被点名的项目不许写成原样。
3. 【变化过程】写的是替换,不是只写姿态。
4. 固定段落一段不缺,每行之间空了一行(\\n\\n)。
没做到就先改完再输出。只输出 JSON,不要任何解释。"""

# ---- 人脸/人体/审美硬规则常量(模块级): optimize_one 注入用, test_steps_ab.py 复用 ----
FACE_T2V = ('同一个人物,全片面部特征保持完全一致,不变脸;'
            '每个镜头下五官形状、相对位置、面部比例、年龄感保持一致;'
            '表情和头部角度可以变化,但面部结构不变;'
            '不出现面部漂移、换脸、五官变形、特征增减;'
            '非老年人设定时面部不出现法令纹等显老纹路,若人物本身有法令纹,其深浅和位置全片保持一致。')
FACE_I2V = ('严格保持与首帧图同一人物、同一面部特征和发型,五官不漂移不变形;'
            '面部轮廓、五官形状与相对位置、肤色、发型发色、年龄感与首帧完全一致;'
            '全片任何角度、任何表情下都不漂移、不换脸、不增减面部细节(痣、皱纹、眼镜等保持原样);'
            '法令纹状态与首帧一致:首帧没有就全片不出现,首帧有就深浅位置保持一致。')
DYN = ('每个镜头下五官形状、相对位置、面部比例、年龄感保持一致;'
       '表情和头部角度可以变化,但面部结构不变;'
       '不出现面部漂移、换脸、五官变形、特征增减;'
       '非老年人设定时面部不出现法令纹等显老纹路,若人物本身有法令纹,其深浅和位置全片保持一致。')
BODY_T2V = ('人体解剖结构自然准确,全片身体比例与器官位置保持正常;'
            '头颈肩连接自然,躯干与四肢比例正常,胸部位置与形态符合正常人体解剖且随动作自然变化,'
            '四肢数量正确关节位置正常,手部五指完整指节自然,腰臀曲线自然不扭曲;'
            '运动全程结构稳定:无器官漂移、无肢体数量增减、无关节反折、无躯干扭曲,胸部不位移不畸形不忽大忽小。')
BODY_I2V = ('严格保持与首帧图同一身体结构:躯干四肢比例、胸部位置与形态、手部完整性、腰臀曲线全片与首帧一致;'
            '运动全程无器官错位、无肢体增减、无关节反折、无躯干扭曲。')
# 改图专用: 脸锁首帧但发型允许用户点名改动(原 FACE_I2V 把发型也钉死, 用户要换发型会被自己人驳回)
V2E_FACE = ('严格保持与首帧图同一人物:脸型、五官形状与相对位置、肤色、年龄感与首帧一致,'
            '不换脸、不漂移、不增减面部特征;发型发色只在用户点名改动时改变,未点名时与首帧一致。')
V2E_BODY = ('保持首帧同一身体结构:躯干四肢比例正常,肩宽腰臀曲线自然,四肢数量与关节位置正确,手部五指完整;'
            '运动全程无肢体增减、无关节反折、无躯干扭曲。')
# 审美锚点 V2(2026-10-12二次强化): 措辞全正面零否定——H3节点没有negative_prompt输入,
# 否定句写进正文会反向强化(白熊效应); 若实测仍残留欧美感, 下一步走首帧锁外观(A方案)
ANCHOR_V2 = ('审美锚点: 东亚中国年轻少女,约20岁,面部轮廓圆润饱满;'
             '皮肤白皙如牛奶般冷白细腻,呈少女感的清透哑光肤感;'
             '身材纤细单薄,手臂与腿部线条柔和圆润如少女,肩背薄;'
             '整体清新淡雅低饱和色调,自然暖白光线,如日系居家杂志写真。')
ANCHOR_R2V = ('审美锚点: 长相穿着以参考照片为准;渲染上皮肤白皙如牛奶(冷白/哑光),'
              '面部与身体轮廓圆润柔和,身材纤细单薄,手臂与腿部线条如少女般柔和圆润;'
              '整体清新淡雅低饱和色调,自然暖白光线,如日系居家杂志写真。')

# ---- 长视频分镜拆解器(2026-10-13): 任意总时长 -> N段(每段<=15s, 尾帧续接+拼接) ----
SB_SYSTEM = """你是 MiniMax H3 视频生成模型的长视频分镜拆解器。用户给一个完整视频创意和总时长,你把它拆成若干个连续分段,用于"逐段生成 + 尾帧续接 + 拼接"流程。严格 JSON 数组输出,每项:
{"duration": 秒数, "has_person": true或false, "prompt": "本段H3成片提示词", "note": "给用户看的衔接说明"}

铁律:
1. 每段 5~15 秒,按动作完整性切分(一个完整动作/情节节拍一段,不在动作中途切断);所有段 duration 之和必须恰好等于总时长
2. 全片共用一个风格锚:以用户描述里指定的风格为准;用户没指定时按内容自己挑一个最合适的,并在第1段 prompt 里定死风格词,之后每段原样复用同一组词,一字不改。不要默认套用某一种风格(不写死黏土/动漫/写实)
3. 第1段 prompt 写完整首帧构图(主角外观/服装/场景定格,图生视频则以图为准);第2段起 prompt 第一句写"承接上段结尾画面:",之后只写本段动作/镜头/光线/声音——禁止重复描述人物长相与服装(实际首帧就是上段真实尾帧,外观已被图锁死),禁止擅自换场景除非用户原文有换场
4. 每段 prompt 是独立完整的 H3 提示词,五段式分行(\\n分隔): ①风格与场景锚(复用同组词) ②承接/首帧构图 ③本段核心动作(一条主线,按时间顺序,用开始/随后/紧接着/最后标节拍) ④显性禁区(本段涉及的禁止项,逐条明写) ⑤声音(环境音+动作音,H3自带音效必须写声音)
5. 画面含人物时 has_person=true,且该段 prompt 必须带详细人脸一致性约束(总约束+特征锁定清单4-6项+动态约束)与人体结构约束,内容与措辞全片各段保持一致,不可一段有一段没有
6. 人物外观按用户描述与参考图来:年龄、肤色、身材、穿着照用户写的写;用户完全没写时才按内容合理补全,补全后全片保持一致。不要擅自把人改成固定年龄/固定肤色/固定体型,也不要默认套日系美颜
7. 【方向铁律】用户说的内容方向(谁/在哪/做什么/情节/结局)不可更改: 只优化表达,严禁加戏、严禁改结局、严禁打乱动作顺序;用户点名的每个动作必须落在某一具体段里,一段装不下就拆两段,一个都不许漏
8. note 字段一句话写清本段从哪接到哪停(给用户看衔接关系)
9. 只输出 JSON 数组本身,不要 markdown 围栏,不要任何解释
【输出前自检】逐段核对: duration之和==总时长;风格词各段一致;第2段起无人物外观复述;用户每个动作都已点名到段。"""


def _shrink_b64(b64, max_side=640, quality=88):
    """给视觉模型的图先压到长边 ≤768: 原图动辄 2~4MP, 直接喂会把上下文撑爆
    (qwen2.5vl 在超长图文上下文下会报 ggml 断言), 压到 768 不影响看穿着/场景/颜色"""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(b64))).convert('RGB')
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return b64


def optimize_one(req):
    text = (req.get('text') or '').strip()
    if not text:
        raise ValueError('empty text')
    if len(text) > 4000:   # 防超长粘贴把 7B 拖死(2026-10-26 15:23 超时排查)
        text = text[:4000]
    mode = req.get('mode', 't2v')
    is_v2e = mode == 'v2e'   # AI改图(视频截帧法): 复用 i2v 生成链路, 提示词/锚点全部走 v2e 专用分支
    if is_v2e:
        mode = 'i2v'
    is_i2e = mode == 'i2e'
    is_iid = mode == 'iid'
    kind = ('改图' if is_v2e else ('保脸改图' if is_iid else ('图改图' if is_i2e else ('图生视频' if mode == 'i2v'
            else ('参考生视频' if mode == 'r2v' else ('文生图' if mode == 't2i' else '文生视频'))))))
    sb = req.get('storyboard')
    img = req.get('image') if not is_iid else None  # i2v 首帧图 / i2e 原图 base64,走视觉模型;iid 纯文本翻译
    feedback = (req.get('feedback') or '').strip()   # 反馈重写: 用户吐槽 + 生成结果截图
    prev = (req.get('prev') or '').strip()           # 上一版实际用于生成的 prompt
    history = req.get('history') or []               # 前几轮反馈重写记录 [{feedback, prev, next}], 多轮沟通上下文
    result_img = (req.get('result_image') or '').strip()  # 图片成片的反馈图(t2i/i2e/iid), base64
    if feedback and req.get('video'):
        result_img = extract_lastframe_b64(str(req.get('video')))  # 视频成片: 服务端自动提尾帧
    if sb:
        sb_text = '\n'.join(f"{s.get('sec', '')}: 画面={s.get('visual', '')} 声音={s.get('sound', '')}" for s in sb)
        user_msg = (f'{kind}需求：{text}\n\n用户已手动调整好分镜,必须严格按以下分镜重写 prompt 和参数'
                    f'(duration 取分镜总时长,storyboard 字段原样返回用户给的版本,不得改动):\n{sb_text}')
    else:
        user_msg = f'{kind}需求：{text}'
    if mode == 'r2v':
        user_msg += ('\n\n（这是参考生视频：用户已上传真人照片锁定人物长相与穿着，prompt 只写动作/镜头/光线/声音，'
                     '严禁描述人物长相/发型/服装；场景默认沿用照片里的环境，除非用户明确要求换场景）')
    # 用户已定的时长/比例是铁律: AI 只负责编排, 不许擅自改（否则 15 秒会被顶成 8 秒）
    dur = req.get('dur')
    if dur:
        try:
            dsec = int(float(dur))
            user_msg += (f'\n\n（用户已把成片时长定为 {dsec} 秒：duration 必须返回 {dsec}；storyboard 时间轴从 0 到 {dsec} 秒'
                         f'连续铺满，每格 2~4 秒按内容节奏自定，最后一格的结束时间必须等于 {dsec} 秒）')
        except (TypeError, ValueError):
            pass
    rk = req.get('ratio')
    if rk and rk != 'auto':
        try:
            short = str(rk).split(' ')[0]
            user_msg += f'\n\n（用户已选定画面比例：ratio 必须返回 "{short}"）'
        except Exception:
            pass
    img, result_img = (_shrink_b64(img) if img else img), (_shrink_b64(result_img) if result_img else result_img)
    imgs = [i for i in (img, result_img) if i]  # 首帧/原图 + 生成结果尾帧, 双图对照找 drift
    if img and result_img:
        user_msg += '\n\n(共两张图: 第1张是首帧/原图=目标画面, 第2张是上一版生成结果的尾帧=实际出片; 请对照两张图找出用户反馈的问题所在)'
    elif result_img:
        user_msg += '\n\n(附图是上一版 prompt 生成出的成片画面, 请对照它找出用户反馈的问题所在)'
    elif img:
        if is_i2e:
            user_msg += '\n\n(附图就是用户要修改的原图,请看清图中人物长相/服装/场景,改写时以图为准)'
        elif is_v2e:
            user_msg += ('\n\n(附图=要改的原图,它是本次改动的起点不是终点:人物长相、未点名的发型、场景陈设以图为准;'
                         '但用户点名的改动必须在最终画面里生效——尤其是服装,必须换成用户说的新样式,原服装不得留在最终画面。'
                         '服装类改动写清三件事:原来穿的什么、换成什么(款式/颜色/材质/长短)、怎么换的)')
        else:
            user_msg += '\n\n(附上的就是用户上传的首帧图,画面描述以图里实际的人物长相/服装/场景为准,不要瞎编与图不符的内容)'
    if is_v2e:
        user_msg += ('\n\n【本次是改图用途】:H3 从原图首帧开始演一段视频,出片后抽帧当改图成品。'
                     'prompt 必须让画面从原样变成目标态:开头约 1 秒内完成替换(连贯过渡),之后按用户要求做动作或保持最终状态静止。'
                     '镜头固定不动、主体基本静止,但"把衣服换成X"这类改动必须真的发生在画面里——'
                     '只保持首帧原样、不做替换的一版视为失败。')
    base_sys = (OPT_SYSTEM_V2E if is_v2e else OPT_SYSTEM_T2I if mode == 't2i'
                else (OPT_SYSTEM_R2V if mode == 'r2v' else (OPT_SYSTEM_IID if is_iid
                else (OPT_SYSTEM_I2E if is_i2e else OPT_SYSTEM))))
    if feedback:
        hist_txt = ''
        if history:
            lines = [f"第{i}轮 用户反馈:{str(h.get('feedback', ''))[:150]} | 该轮改后prompt:{str(h.get('next', ''))[:180]}"
                     for i, h in enumerate(history[-4:], 1)]  # 最多带最近4轮(2026-10-26: 6轮+完整prev 会把上下文顶满)
            hist_txt = ('\n【前几轮迭代记录(你与用户的对话史)】' + '\n'.join(lines) +
                        '\n(记录里已改过的内容视为已定稿; 已否决的方向绝对不要改回去; 同一问题不要重复改)')
        base_sys += (f'\n\n【本次是反馈重写】用户已按上一版 prompt 生成过一轮,附图是生成结果,用户看完不满意。'
                     f'\n用户反馈:{feedback}'
                     f'\n上一版 prompt:{(prev or "(无)")[:1200]}'
                     f'\n重写规则:用户原始的内容方向(谁/在哪/做什么/情节/结局)永远不变,只针对反馈修问题;'
                     f'没问题的部分尽量保持原样;上一版里用户手动改过的内容必须保留(用户改动优先);'
                     f'duration/ratio 铁律与人脸一致性硬约束照常全部遵守;输出仍是完整全套字段,不许只给改动片段'
                     f'\n【把反馈当想法理解】用户这句话是意见/方向/感受,不是要你照抄进 prompt 的指令:'
                     f'先判断他真正在意的是什么(是漏了内容?是效果没达成?是风格不对?),再改对应段落;'
                     f'严禁把用户原话原样粘进 prompt,也严禁只加一句"用户要求…"了事。'
                     f'\n【漏项必补】如果用户指出漏了内容(某段动作/服装细节/声音/场景元素/道具),'
                     f'必须把漏掉的东西补回它该在的段落,并在对应节拍里体现;不许用"调整/优化/更自然"这类话糊弄过去。' + hist_txt)
    # 描述模型二选一(2026-10-26 用户要求): 快速=现用的 7B/9B, 精细=qwen3-vl:8b(会思考, 慢 2~4 倍)
    want_deep = (req.get('opt_model') or 'fast') == 'deep'
    body = {
        # 快速档: 看图走 qwen2.5vl:7b(非思考型, 2026-10-23 换), 纯文本走 qwen3.5:9b
        # 精细档: qwen3-vl:8b, thinking 关不掉, 每次 92~240s 且可能空返回, 超时放宽到 420s
        'model': ('qwen3-vl:8b' if want_deep else ('qwen2.5vl:7b' if imgs else 'qwen3.5:9b')),
        'stream': False, 'keep_alive': '5m',
        'format': 'json',
        'options': {'num_ctx': 6144},  # 显式给上下文防自动缩水; 8192 在 16G 卡上会把推理拖慢(11:32 实测超时)
        'messages': [
            {'role': 'system', 'content': base_sys},
            {'role': 'user', 'content': user_msg, **({'images': imgs} if imgs else {})},
        ],
    }
    if not imgs and body['model'] == 'qwen3.5:9b':
        body['think'] = False   # qwen2.5vl / qwen3-vl 不支持该参数, 只在 qwen3.5 上带
    log(f'optimize req: text={len(text)} user={len(user_msg)} imgs={len(imgs)} hist={len(history)} deep={want_deep} model={body["model"]}')

    def _chat(messages):
        b = dict(body, messages=messages)
        r = urllib.request.Request('http://127.0.0.1:11434/api/chat',
                                   data=json.dumps(b, ensure_ascii=False).encode('utf-8'),
                                   headers={'Content-Type': 'application/json'})
        resp = json.load(_opener.open(r, timeout=420 if want_deep else 240))  # 精细档 thinking 慢, 单独放宽
        c = (resp['message']['content'] or '').strip()
        if c.startswith('```'):
            c = re.sub(r'^```[a-zA-Z]*\s*', '', c)
            c = re.sub(r'\s*```\s*$', '', c)
        return c

    def _chat_heal(messages):
        """挂死自愈(2026-10-15): Ollama 实例长跑劣化会 GPU 空转永不返回, 卸载重载模型后重试一次"""
        try:
            return _chat(messages)
        except Exception as e1:
            # 超时不再自动重载(2026-10-26): 一次超时已等 240s, 再重载重跑又是 4 分钟, 用户只会觉得彻底卡死
            if isinstance(e1, TimeoutError) or 'timed out' in str(e1).lower():
                raise ValueError('这次模型太慢超时了（不是坏了）。直接再点一次；连续两次都超时就重启 Ollama。')
            log(f'optimize {mode} 首次失败({type(e1).__name__}), 自动卸载重载模型重试…')
            ollama_unload()
            time.sleep(3)
            try:
                r = _chat(messages)
                log(f'optimize {mode} 重试成功')
                return r
            except Exception as e2:
                raise ValueError(f'AI 服务两次尝试都失败（{type(e2).__name__}: {str(e2)[:60]}）。'
                                 f'模型已自动重载, 请再点一次优化; 仍失败就双击一键启动 bat 重启 Ollama')

    content = _chat_heal(body['messages'])
    try:
        out = json.loads(content)
    except ValueError:
        log(f'optimize {mode} parse fail, auto retry once')
        content = _chat_heal(body['messages'] + [
            {'role': 'assistant', 'content': content[:800]},
            {'role': 'user', 'content': '你的输出无法被解析为 JSON。重新完整输出一次,只输出 JSON 对象本身,不要用 markdown 围栏,不要任何解释。'}])
        try:
            out = json.loads(content)
        except ValueError:
            raise ValueError(f'AI 返回了无法解析的内容（{content[:60] or "空响应"}），再点一次通常就好')
    # 空壳检测(2026-10-12): 冷启动/显存抖动后 9B 实例会退化成"只有标题行"的骨架, 自动重试一次
    if len((out.get('prompt') or '')) < 120:
        log(f'optimize {mode} skeleton detected (len={len(out.get("prompt") or "")}), auto retry once')
        content = _chat_heal(body['messages'] + [
            {'role': 'assistant', 'content': content[:800]},
            {'role': 'user', 'content': '你的输出是空壳模板(只有段落标题、没有实质内容)。重新完整输出一次:prompt 必须填满头面部外观、场景、动作节拍、声音、镜头、风格等全部段落的实质内容,只输出 JSON 对象本身,不要解释。'}])
        out = json.loads(content)
    log(f'optimize {mode} img={bool(img)} done')
    if not out.get('prompt'):
        raise ValueError('no prompt in llm reply')
    # clarity 兜底(2026-10-13): 用户已选 0.9(高清超分)时, LLM 按提示词默认返回 0.2/0.4 会覆盖掉用户选择
    # 规则: 用户当前值是 0.9 → 保留 0.9(最高清不可逆降); 否则以 LLM 返回值为准
    try:
        user_mp = str(req.get('mp') or '')
    except Exception:
        user_mp = ''
    if user_mp == '0.9':
        out['clarity'] = '0.9'
    if sb:
        out['storyboard'] = sb  # 用户手改的分镜原样返回
    # 人脸/人体/审美常量已在模块级定义(FACE_T2V/FACE_I2V/DYN/BODY_T2V/BODY_I2V/ANCHOR_V2/ANCHOR_R2V)
    p = out['prompt']
    if out.get('has_person') and not is_i2e and not is_iid:
        if is_v2e:
            # 改图专用(2026-10-26): 不注入审美锚点——原锚点把人物强行改成"20岁东亚少女/牛奶冷白皮",
            # 与被改的原图打架(年龄/肤色/人种都被拽走), 改图只该锁人不该换人。
            if '与首帧图同一人物' not in p and '同一人物' not in p:
                out['prompt'] = V2E_FACE + '\n\n' + p
            if '同一身体结构' not in p and '解剖结构自然准确' not in p:
                out['prompt'] = out['prompt'].rstrip('。') + '。\n\n' + V2E_BODY
            p = out['prompt']
        else:
            # 审美锚点服务端兜底(2026-10-12二次强化): 9B优化器会丢锚导致成片仍偏欧美(小麦色/肌肉感),
            # 与FACE/BODY同机制——不依赖LLM自觉, 含人物直接在最终prompt追加锚点正文。
            if '审美锚点' not in p:
                out['prompt'] = p.rstrip('。') + '。\n\n' + (ANCHOR_R2V if mode == 'r2v' else ANCHOR_V2)
                p = out['prompt']
            if mode != 'r2v':  # r2v 长相由照片锚定,不塞文本脸规则
                if mode == 'i2v':
                    if '五官不漂移' not in p:
                        out['prompt'] = FACE_I2V + '\n\n' + p
                    elif '不漂移' not in p.split('五官不漂移', 1)[-1] and '面部结构不变' not in p:
                        out['prompt'] = p.rstrip('。') + '。\n\n' + DYN
                else:
                    if '不变脸' not in p and '保持完全一致' not in p:
                        out['prompt'] = p.rstrip('。') + '。\n\n' + FACE_T2V
                    elif '面部结构不变' not in p:
                        out['prompt'] = p.rstrip('。') + '。\n\n' + DYN
                p = out['prompt']
            # 人体结构: t2v/i2v/r2v 全量兜底
            if mode == 'i2v':
                if '同一身体结构' not in p and '解剖结构自然准确' not in p:
                    out['prompt'] = p.rstrip('。') + '。\n\n' + BODY_I2V
            elif '解剖结构自然准确' not in p and '身体比例与器官位置' not in p:
                out['prompt'] = p.rstrip('。') + '。\n\n' + BODY_T2V
    if is_v2e:
        # 强制改动块(2026-10-26 用户要求): 优化后的 prompt 末尾再钉一遍用户原话,
        # 防止 LLM 把"换衣服"重写成"整理衣物"或只写动作不写换装; 这段是给 H3 读的最后一句, 权重最高。
        # 只钉用户最初的改图诉求; 反馈是"想法", 由 LLM 理解后改写进 prompt, 不能原话粘进来
        core = re.sub(r'\s+', ' ', text).strip()
        out['prompt'] = out['prompt'].rstrip() + (
            '\n\n【必做改动·最终画面必须呈现】\n' + core[:240] +
            '\n（以上改动按"改完之后的样子"呈现,原状不得保留;开头约1秒内完成替换,其后按需做动作或保持静止。'
            '只保持首帧原样、没发生替换的一版视为失败。）')
    return out


_opt_lock = threading.Lock()


def optimize(req):
    """优化单飞锁: Ollama 一次只能跑一条, 连点会排队卡死(2026-09-27 实战), 直接拒并提示"""
    if not _opt_lock.acquire(blocking=False):
        raise ValueError('上一条优化还在跑,请等它出结果再点——连续多点只会让队伍更卡')
    try:
        return optimize_one(req)
    finally:
        _opt_lock.release()


def _fmt_eta(sec):
    sec = max(0, int(sec))
    return f'{sec // 60:02d}:{sec % 60:02d}'


def ws_listener():
    import websocket
    global PROGRESS_AT, FIN_T0, FIN_HIST
    while True:
        ws = None
        try:
            ws = websocket.create_connection(UPSTREAM.replace('http://','ws://').rstrip('/') + '/ws', timeout=30)
            while True:
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue  # 30秒无消息属正常,连接还活着,不许重连(旧代码每30s重连丢一个死socket,ComfyUI刷屏 send error)
                t = msg.get('type')
                d = msg.get('data', {})
                pid = d.get('prompt_id')
                if t == 'execution_start':
                    if pid == CURRENT_PID:
                        LATEST.clear()
                elif t == 'progress':
                    if pid != CURRENT_PID:
                        continue  # 并行短任务(AB测试/其他脚本直连ComfyUI)的进度与本任务无关, 丢弃防污染
                    v, mx = d.get('value'), d.get('max')
                    if not v or not mx:
                        continue
                    now = time.time()
                    PROGRESS_AT = now
                    if 't0' not in LATEST:
                        # 首个事件只建基准,速率未知
                        LATEST.update({'t0': now, 'v0': v, 'value': v, 'max': mx, 'eta': '--:--'})
                        continue
                    if v <= LATEST.get('value', 0):
                        continue  # 重复事件:保留已有 eta,不重置基准
                    dv = v - LATEST['v0']
                    rate = (now - LATEST['t0']) / dv if dv > 0 else 0
                    eta = _fmt_eta(rate * (mx - v)) if rate > 0 else LATEST.get('eta', '--:--')
                    LATEST.update({'value': v, 'max': mx, 'eta': eta})
                elif t == 'execution_success' and pid == CURRENT_PID:
                    # 采样→落盘的总收尾耗时入库, 滚动均值给前端做收尾 ETA
                    if PROGRESS_AT:
                        FIN_HIST = (FIN_HIST + [max(1, int(time.time() - PROGRESS_AT))])[-10:]
        except Exception:
            LATEST.clear()
            time.sleep(5)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass


def _hms(s):
    p = [int(x) for x in s.split(':')]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


def _log_progress(now):
    """日志兜底: 取 tqdm 行做归属校验——elapsed 须落在本任务已跑时长 ± 容差内,
    太短是并行短任务(AB测试)的行, 太长是旧任务残留(10-12 实测两类污染都存在)"""
    try:
        with open(SERVER_LOG, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            data = f.read().decode('utf-8', 'replace')
    except Exception:
        return None
    data = re.sub(r'\x1b\[[0-9;]*m', '', data)
    marker = data.rfind('got prompt')
    if marker >= 0:
        data = data[marker:]
    ms = re.findall(r'(\d+)/(\d+)\s*\[\s*([\d:]+)\s*<\s*([\d:]+)', data)
    run = now - TASK_T0
    for v, mx, el, eta in reversed(ms):
        e = _hms(el)
        if e < 60 or e < run - 420 or e > run + 90:
            continue
        return {'value': int(v), 'max': int(mx), 'eta': eta}
    return None


def read_progress():
    """WS 缓存(带 120s 新鲜度校验)优先; 失效回退日志 tqdm 行(带归属校验)"""
    now = time.time()
    if LATEST.get('value') is not None and now - PROGRESS_AT < 120:
        r = {'value': LATEST['value'], 'max': LATEST['max'], 'eta': LATEST.get('eta', '')}
    else:
        r = _log_progress(now)
    if r and r.get('max') and r['value'] >= r['max']:
        r['fin_eta'] = int(sum(FIN_HIST) / len(FIN_HIST)) if FIN_HIST else 360  # 冷启动按 6 分钟(本次实测 8 分钟)
    return r


def log(msg):
    with open(os.path.join(DIR, 'panel.log'), 'a', encoding='utf-8') as f:
        f.write(f'{datetime.datetime.now():%H:%M:%S} {msg}\n')


_last_unet = [None]   # 上一次提交用的 UNET，用于切换模型前卸载显存


def vram_preflight(wf):
    """切模型前先让 ComfyUI 卸掉旧模型：16GB 卡硬塞 20GB 权重会卡死在 Model Initializing（2026-10-27 实测）。"""
    unet = None
    for n in (wf.get('prompt') or {}).values():
        if n.get('class_type') == 'UNETLoader':
            unet = (n.get('inputs') or {}).get('unet_name')
            break
    prev = _last_unet[0]
    if unet and prev and unet != prev:
        try:
            _req = urllib.request.Request(UPSTREAM + '/free', method='POST',
                                         data=b'{"unload_models": true, "free_memory": true}',
                                         headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(_req, timeout=30).read()
            log('VRAM preflight: unload %s -> %s' % (os.path.basename(prev), os.path.basename(unet)))
            time.sleep(2)
        except Exception as e:
            log('VRAM preflight failed: %s' % e)
    if unet:
        _last_unet[0] = unet


def _up_json(path, data=None, timeout=120):
    req = urllib.request.Request(UPSTREAM + path,
                                 data=json.dumps(data).encode('utf-8') if data else None,
                                 headers={'Content-Type': 'application/json'})
    return json.load(_opener.open(req, timeout=timeout))


def _upload_to_comfy(path):
    """multipart 上传图片到 ComfyUI input/, 返回服务端文件名"""
    boundary = '----enhpanel'
    name = os.path.basename(path)
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n').encode() \
        + open(path, 'rb').read() + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(UPSTREAM + '/upload/image', data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    return json.load(_opener.open(req, timeout=120))['name']


def enhance_photo(name, face, scale, log=log):
    """照片清晰化: 可选GFPGAN人脸修复 -> Real-ESRGAN x4 -> lanczos收放到scale倍 -> output/image/
    face: 'off'/False=不修 | 'natural'=自然保脸(权重0.25,保真不变假脸,推荐) | 'strong'/True=强力(0.5,糊脸专用)"""
    src = os.path.join(IN_ROOT, name)
    if not os.path.isfile(src):
        raise ValueError('图片不存在：' + name)
    pr = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                         '-show_entries', 'stream=width,height', '-of', 'csv=p=0', src],
                        capture_output=True, timeout=60)
    W, H = (int(x) for x in pr.stdout.decode().strip().split(','))
    cur, cname = src, name
    tmp = None
    if face and face != 'off':
        fw = 0.25 if face == 'natural' else 0.5
        os.makedirs(os.path.join(OUT_ROOT, '_frame_tmp'), exist_ok=True)
        tmp = os.path.join(OUT_ROOT, '_frame_tmp', f'face_{int(time.time() * 1000)}.jpg')
        log(f'enhance: GFPGAN 人脸修复中(权重{fw}{",自然保脸" if fw==0.25 else ",强力"},约10~30秒)...')
        r = subprocess.run([VENV_PY, FACE_RESTORE, cur, tmp, str(fw)],
                           capture_output=True, timeout=600)
        if not os.path.isfile(tmp):
            raise ValueError('人脸修复失败: ' + r.stderr.decode('utf-8', 'replace')[-300:])
        cur, cname = tmp, None
    if cur != src:
        cname = _upload_to_comfy(cur)
    tw, th = W * scale, H * scale
    log(f'enhance: Real-ESRGAN x4 -> {tw}x{th}')
    wf = {
        "1": {"inputs": {"model_name": "RealESRGAN_x4plus.pth"}, "class_type": "UpscaleModelLoader"},
        "2": {"inputs": {"image": cname}, "class_type": "LoadImage"},
        "3": {"inputs": {"upscale_model": ["1", 0], "image": ["2", 0]}, "class_type": "ImageUpscaleWithModel"},
        "4": {"inputs": {"image": ["3", 0], "upscale_method": "lanczos", "width": tw, "height": th,
                         "crop": "disabled"}, "class_type": "ImageScale"},
        "5": {"inputs": {"images": ["4", 0], "filename_prefix": "enhanced/p"}, "class_type": "SaveImage"},
    }
    pid = _up_json('/api/prompt', {'prompt': wf})['prompt_id']
    png = None
    t0 = time.time()
    while time.time() - t0 < 600:
        time.sleep(3)
        h = _up_json('/api/history/' + pid)
        if pid not in h:
            continue
        st = h[pid].get('status', {})
        if st.get('status_str') == 'error':
            raise ValueError('超分失败: ' + json.dumps(st.get('messages', []), ensure_ascii=False)[:300])
        if st.get('completed'):
            for v in h[pid].get('outputs', {}).values():
                for fn in v.get('images') or []:
                    png = os.path.join(OUT_ROOT, fn.get('subfolder', ''), fn['filename'])
            break
    if not png or not os.path.isfile(png):
        raise ValueError('超分超时或没有输出')
    os.makedirs(os.path.join(OUT_ROOT, 'image'), exist_ok=True)
    outname = f'enh_{int(time.time() * 1000)}.jpg'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', png,
                    '-q:v', '1', os.path.join(OUT_ROOT, 'image', outname)], check=True, timeout=120)
    try:
        os.remove(png)
    except OSError:
        pass
    if tmp:
        try:
            os.remove(tmp)
        except OSError:
            pass
    log(f'enhance: OK -> image/{outname}')
    return outname


# ==================== 长视频模式(2026-10-13): 任意总时长 = N段(<=15s) 尾帧续接 + 拼接 ====================

def _fix_segs(segs, total):
    """分镜时长校验修正: 每段夹紧[5,15], 总和归一到 total(从尾段往前吃差值); 返回(leftover)"""
    for s in segs:
        try:
            d = int(round(float(s.get('duration') or 10)))
        except (TypeError, ValueError):
            d = 10
        s['duration'] = min(15, max(5, d))
    # 全15s还不够 → 尾部补"延续段"(沿用末段风格与动作方向)
    for _ in range(80):
        cur = sum(s['duration'] for s in segs)
        room = sum(15 - s['duration'] for s in segs)
        diff = total - cur
        if diff <= room:
            break
        segs.append({'duration': min(15, max(5, diff - room)), 'has_person': segs[-1].get('has_person'),
                     'prompt': segs[-1]['prompt'], 'note': '（自动补足）延续上段画面与动作'})
    for _ in range(200):
        diff = total - sum(s['duration'] for s in segs)
        if diff == 0:
            break
        moved = False
        for i in range(len(segs) - 1, -1, -1):   # 从尾段往前吃差值, 保动作完整性优先动尾段
            if diff > 0 and segs[i]['duration'] < 15:
                d = min(diff, 15 - segs[i]['duration']); segs[i]['duration'] += d; diff -= d; moved = True
            elif diff < 0 and segs[i]['duration'] > 5:
                d = min(-diff, segs[i]['duration'] - 5); segs[i]['duration'] -= d; diff += d; moved = True
        if not moved:
            break
    return segs, total - sum(s['duration'] for s in segs)


def storyboard_plan(req):
    """长视频分镜拆解: LLM 把总需求按动作完整性切成 N 段(每段<=15s), 服务端校验时长和+锚点注入"""
    text = (req.get('text') or '').strip()
    total = int(float(req.get('total') or 0))
    vmode = req.get('mode') or 't2v'   # 首段模式(t2v/i2v/r2v), 决定段1的锚点措辞; 段2+恒为 i2v 续接
    if not text:
        raise ValueError('先写点描述吧')
    if total < 16:
        raise ValueError('总时长至少 16 秒(长视频至少拆 2 段; 16 秒以内直接用普通模式生成)')
    if total > 600:
        raise ValueError('总时长上限 600 秒(600 秒≈40~50 段连跑要好几个小时, 超长请分批做)')
    n_hint = max(2, round(total / 11.5))
    comfy_free()        # 同 /optimize: 先请出 ComfyUI 模型(队列非空自动跳过)
    wait_vram_free()    # 等 WDDM 显存回收落地, 防 Ollama 半吊子实例(踩坑3)
    ollama_unload()
    body = {
        'model': 'qwen3.5:9b', 'stream': False, 'think': False, 'keep_alive': '5m',
        'format': 'json',
        'messages': [
            {'role': 'system', 'content': SB_SYSTEM},
            {'role': 'user', 'content': f'总时长 {total} 秒,拆成约 {n_hint} 段(每段5~15秒,所有段之和必须恰好={total})。视频需求:\n{text}'},
        ],
    }

    def _chat(messages):
        b = dict(body, messages=messages)
        r = urllib.request.Request('http://127.0.0.1:11434/api/chat',
                                   data=json.dumps(b, ensure_ascii=False).encode('utf-8'),
                                   headers={'Content-Type': 'application/json'})
        resp = json.load(_opener.open(r, timeout=300))
        c = (resp['message']['content'] or '').strip()
        if c.startswith('```'):
            c = re.sub(r'^```[a-zA-Z]*\s*', '', c)
            c = re.sub(r'\s*```\s*$', '', c)
        return c

    content = _chat(body['messages'])
    try:
        arr = json.loads(content)
    except ValueError:
        log('storyboard parse fail, auto retry once')
        content = _chat(body['messages'] + [
            {'role': 'assistant', 'content': content[:800]},
            {'role': 'user', 'content': '你的输出无法被解析为 JSON 数组。重新完整输出一次,只输出 JSON 数组本身,不要用 markdown 围栏,不要任何解释。'}])
        arr = json.loads(content)
    if not isinstance(arr, list):
        raise ValueError('AI 没按分镜数组返回,再点一次通常就好')
    segs = []
    for it in arr[:60]:
        if not isinstance(it, dict):
            continue
        p = str(it.get('prompt') or '').strip()
        if not p:
            continue
        segs.append({'duration': it.get('duration', 10), 'has_person': bool(it.get('has_person')),
                     'prompt': p, 'note': str(it.get('note') or '').strip()})
    if not segs:
        raise ValueError('分镜为空,再点一次通常就好')
    segs, leftover = _fix_segs(segs, total)
    # 只注入结构约束, 不再注入审美锚点(2026-10-26 用户要求): 年龄/肤色/体型/风格一律按用户描述与参考图,
    # 拆段底层不预设"18-22岁冷白皮"这类固定审美, 免得多年龄段/多题材的片子被拽成同一种脸
    for i, s in enumerate(segs):
        if not s['has_person']:
            continue
        if i == 0 and vmode == 'r2v':
            continue   # 长相穿着由参考图锁, 不塞文本约束
        elif i == 0 and vmode == 't2v':
            s['prompt'] = s['prompt'].rstrip('。') + '。\n' + FACE_T2V + '\n' + BODY_T2V
        else:   # 首段 i2v 及全部续接段: 外观由首帧锁, 走 I2V 措辞
            s['prompt'] = s['prompt'].rstrip('。') + '。\n' + FACE_I2V + '\n' + BODY_I2V
    log(f'storyboard: total={total}s mode={vmode} -> {len(segs)} segs, leftover={leftover}s')
    return {'segs': segs, 'total': total, 'leftover': leftover}


def extract_lastframe_keep(video_name):
    """长视频续接: 提成片尾帧并存进 input/longvideo/(不被清理, 供下一段 LoadImage 当首帧), 返回input相对路径"""
    if not video_name or '/' in video_name or '\\' in video_name or '..' in video_name:
        raise ValueError('bad video name')
    src = os.path.join(OUT_ROOT, 'video', video_name)
    if not os.path.isfile(src):
        raise ValueError('成片不存在：' + video_name)
    os.makedirs(os.path.join(IN_ROOT, 'longvideo'), exist_ok=True)
    name = f"longvideo/tail_{time.strftime('%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.jpg"
    out = os.path.join(IN_ROOT, name)
    r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-sseof', '-1', '-i', src, '-update', '1', '-q:v', '1', out],
                       capture_output=True, timeout=120)
    if r.returncode != 0 or not os.path.isfile(out):
        raise ValueError('尾帧提取失败：' + r.stderr.decode('utf-8', 'replace')[-200:])
    log(f'tail_frame {video_name} -> input/{name}')
    return name


def concat_videos(req):
    """拼接: 源片(input/v2v/ 带前缀) + 各段成片(output/video/ 裸名) 混排, concat demuxer 无损秒拼; 失败回退 libx264 重编码
    2026-10-13: 视频延续要"源片+延续段"合成完整片交付, files 放开三源解析"""
    files = req.get('files') or []
    if len(files) < 2:
        raise ValueError('至少两段才能拼接')
    paths = []
    for fn in files:
        fn = str(fn)
        if not fn.lower().endswith('.mp4') or '..' in fn or "'" in fn:
            raise ValueError('非法成片名: ' + fn)
        paths.append(_video_real_path(_safe_vid_name(fn)))   # v2v//longvideo/ 前缀 -> input, 裸名 -> output/video, 含存在性校验
    name = str(req.get('name') or '')
    if not re.match(r'^[\w\-一-鿿]+\.mp4$', name):
        name = f"长视频_{time.strftime('%m%d_%H%M%S')}.mp4"
    work = tempfile.mkdtemp(prefix='concat_')
    try:
        # 分辨率一致性检测: 源片(用户上传,1080p等)与生成段(MP档位)分辨率必不同,
        # -c copy 直拼会流内切换分辨率, 部分播放器花屏(2026-10-13 实测 ffprobe 存活但不可交付)。
        # 不一致时逐段重编码统一到首段分辨率(比例锁定机制保证同比例, 纯缩放无变形), 再无损拼。
        try:
            whs = [upscale720._probe_wh(p) for p in paths]
        except Exception:
            whs = []
        if len(whs) == len(paths) and any((w, h) != whs[0] for w, h in whs):
            tw, th = whs[0]
            unified = []
            log(f'concat: 分辨率不一致 {whs} -> 统一 {tw}x{th} 重编码')
            for i, p in enumerate(paths):
                tp = os.path.join(work, f'u{i}.mp4')
                r0 = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', p,
                                     '-vf', f'scale={tw}:{th}', '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p',
                                     '-c:a', 'aac', '-b:a', '192k', tp],
                                    capture_output=True, timeout=3600)
                if r0.returncode != 0:
                    raise ValueError('拼接前统一分辨率失败: ' + r0.stderr.decode('utf-8', 'replace')[-200:])
                unified.append(tp)
            paths = unified
        lst = os.path.join(work, 'list.txt')
        with open(lst, 'w', encoding='utf-8') as f:
            for p in paths:
                f.write("file '%s'\n" % p.replace('\\', '/'))
        out = os.path.join(OUT_ROOT, 'video', name)
        r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat',
                            '-safe', '0', '-i', lst, '-c', 'copy', out],
                           capture_output=True, timeout=600)
        if r.returncode != 0:
            log(f'concat -c copy 失败(回退重编码): {r.stderr.decode("utf-8", "replace")[-150:]}')
            r2 = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat',
                                 '-safe', '0', '-i', lst,
                                 '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p',
                                 '-c:a', 'aac', '-b:a', '192k', out],
                                capture_output=True, timeout=7200)
            if r2.returncode != 0:
                raise ValueError('拼接失败: ' + r2.stderr.decode('utf-8', 'replace')[-200:])
        log(f'concat: {len(files)} segs -> video/{name} ({os.path.getsize(out) / 1048576:.1f}MB)')
        return {'name': name, 'size_mb': round(os.path.getsize(out) / 1048576, 1), 'segs': len(files)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def keep_upload_video(stream, length, orig_name):
    """把上传的视频字节存进 input/v2v/(不被清理, 供视频超分/延续模式读), 返回服务端文件名"""
    if length <= 0:
        raise ValueError('没收到视频数据')
    safe = re.sub(r'[^\w\-.]+', '_', os.path.basename(orig_name or 'upload.mp4'), flags=re.UNICODE)[:60] or 'upload.mp4'
    if not safe.lower().endswith(('.mp4', '.mov', '.webm', '.mkv')):
        safe += '.mp4'
    d = os.path.join(IN_ROOT, 'v2v')
    os.makedirs(d, exist_ok=True)
    name = f'v2v_{int(time.time() * 1000)}_{safe}'
    with open(os.path.join(d, name), 'wb') as f:
        left = length
        while left > 0:
            chunk = stream.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    log(f'keep_video -> input/v2v/{name}')
    return name


def _safe_vid_name(name):
    name = str(name or '')
    if not name or '..' in name:
        log(f'_safe_vid_name REJECT empty/..: {name!r}')
        raise ValueError('bad video name')
    # v2v/ longvideo/ 前缀=本机子目录, 合法; 其他禁路径分隔符防目录穿越
    if not (name.startswith('v2v/') or name.startswith('longvideo/')) and ('/' in name or '\\' in name):
        log(f'_safe_vid_name REJECT path sep: {name!r}')
        raise ValueError('bad video name')
    return name


def _video_real_path(name):
    """三种合法来源: output/video 成片 | input/v2v 上传 | input/longvideo 尾帧源"""
    if name.startswith('v2v/'):
        p = os.path.join(IN_ROOT, name)
    elif name.startswith('longvideo/'):
        p = os.path.join(IN_ROOT, name)
    else:
        p = os.path.join(OUT_ROOT, 'video', name)
    if not os.path.isfile(p):
        raise ValueError('视频不存在：' + name)
    return p


def video_upscale(stream, length, orig_name):
    """视频超分: 上传 mp4 -> ncnn x4 -> lanczos 收放(长边1920保源比例) -> output/video/<原名>_1080p.mp4"""
    name = keep_upload_video(stream, length, orig_name)
    src = _video_real_path('v2v/' + name)
    base = os.path.splitext(name)[0]
    dst = os.path.join(OUT_ROOT, 'video', base + '_1080p.mp4')
    UPSCALE_STATUS.clear()
    UPSCALE_STATUS.update({'running': True, 'phase': 'upscale', 'frame': 0, 'total': 0,
                           'name': base + '_1080p.mp4', 'msg': '', 'ts': int(time.time())})
    def _log(msg):
        log(msg)
        UPSCALE_STATUS['msg'] = str(msg)[-300:]
    upscale720.upscale(src, dst, OUT_ROOT, log=_log, status=UPSCALE_STATUS)
    out = os.path.basename(dst)
    log(f'video_upscale {name} -> {out}')
    return {'name': out, 'size_mb': round(os.path.getsize(dst) / 1048576, 1)}


def video_extend_first(video_name, desc, dur):
    """视频延续第1段: 源片尾帧 -> 存 input/longvideo/ -> 返回 (相对路径, 时长夹紧, 源宽, 源高)
    源宽高原样返回: 前端按它锁生成比例, 防延续段尺寸/横竖与源片不一致(2026-10-13 变形修复)"""
    name = _safe_vid_name(video_name)
    src = _video_real_path(name)
    if not str(desc or '').strip():
        raise ValueError('写一句接下来想发生什么，AI 才知道怎么延续')
    dur = int(dur or 5)
    if not 1 <= dur <= 15:
        raise ValueError('每段时长 1~15 秒')
    os.makedirs(os.path.join(IN_ROOT, 'longvideo'), exist_ok=True)
    rel = f"longvideo/tail_{time.strftime('%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.jpg"
    out = os.path.join(IN_ROOT, rel)
    r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-sseof', '-1', '-i', src, '-update', '1', '-q:v', '1', out],
                       capture_output=True, timeout=120)
    if r.returncode != 0 or not os.path.isfile(out):
        raise ValueError('尾帧提取失败：' + r.stderr.decode('utf-8', 'replace')[-200:])
    try:
        sw, sh = upscale720._probe_wh(src)
    except Exception:
        sw, sh = 0, 0
    log(f'video_extend first tail <- {name} ({sw}x{sh})')
    return rel, dur, sw, sh


# ==================== 真·视频改视频(2026-10-14): Fun-ControlNet-Union-2.0 整片重绘 ====================
V2V_PATCH = 'minimax_h3_fun_controlnet_union_2.0_pruned_int8_convrot.safetensors'
V2V_CHUNK_SEC = 14.0   # 单片上限(15s 的 17n+5 帧栅格安全区)
V2V_RATIOS = [('21:9 (Ultrawide)', 21/9), ('16:9 (Widescreen)', 16/9), ('3:2 (Photo)', 1.5),
              ('4:3 (Standard)', 4/3), ('1:1 (Square)', 1.0), ('3:4 (Portrait Standard)', 0.75),
              ('2:3 (Portrait Photo)', 2/3), ('9:16 (Portrait Widescreen)', 9/16)]


def _probe_duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', path], capture_output=True, timeout=60)
    return float((r.stdout.decode() or '0').strip() or 0)


def _frames_canny(video, out_dir):
    """抽帧(24fps) -> Canny 控制图 -> out_dir/frame_00001.jpg...(ComfyUI venv 的 cv2)"""
    raw = os.path.join(out_dir, '_raw')
    os.makedirs(raw, exist_ok=True)
    r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-i', video, '-vf', 'fps=24', '-q:v', '2',
                        os.path.join(raw, 'frame_%05d.jpg')],
                       capture_output=True, timeout=600)
    if r.returncode != 0:
        raise ValueError('抽帧失败: ' + r.stderr.decode('utf-8', 'replace')[-200:])
    code = ('import cv2,glob,os,sys\n'
            'd,raw=sys.argv[1],sys.argv[2]\n'
            'for p in sorted(glob.glob(os.path.join(raw,"*.jpg"))):\n'
            '    img=cv2.imread(p)\n'
            '    g=cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)\n'
            '    e=cv2.Canny(g,100,200)\n'
            '    cv2.imwrite(os.path.join(d,os.path.basename(p)),cv2.cvtColor(e,cv2.COLOR_GRAY2BGR),[cv2.IMWRITE_JPEG_QUALITY,95])\n')
    r = subprocess.run([VENV_PY, '-c', code, out_dir, raw], capture_output=True, timeout=900)
    if r.returncode != 0:
        raise ValueError('Canny 生成失败: ' + r.stderr.decode('utf-8', 'replace')[-200:])
    shutil.rmtree(raw, ignore_errors=True)


def _v2v_wf(desc, frames_dir, sw, sh, ratio_name, mp, dur, seed, run, idx):
    """template_t2v + 注入 ModelPatchLoader/LoadImagesFromFolderKJ/ApplyFunControlNet"""
    with open(os.path.join(DIR, 'template_t2v.json'), encoding='utf-8') as f:
        wf = json.load(f)
    guider = vvae = None
    for nid, n in wf.items():
        ct = n.get('class_type')
        ins = n.get('inputs', {})
        if ct == 'MiniMaxH3ImageToVideo':
            ins['prompt'] = desc
        elif ct == 'ResolutionSelector':
            ins['aspect_ratio'] = ratio_name
            ins['megapixels'] = mp
        elif ct == 'RandomNoise':
            ins['noise_seed'] = seed
        elif ct == 'SaveVideo':
            ins['filename_prefix'] = f'video/v2v_{run}_c{idx}'
        elif ct == 'PrimitiveFloat':
            ins['value'] = round(dur, 2)
        elif ct == 'BasicGuider':
            guider = nid
        elif ct == 'VAELoader' and 'video' in str(ins.get('vae_name')):
            vvae = nid
    model_src = wf[guider]['inputs']['model']   # 保留出片档位开关链
    wf['900'] = {'class_type': 'LoadImagesFromFolderKJ',
                 'inputs': {'folder': frames_dir, 'width': sw, 'height': sh,
                            'keep_aspect_ratio': 'stretch',
                            'image_load_cap': 0, 'start_index': 0, 'include_subfolders': False}}
    wf['901'] = {'class_type': 'ModelPatchLoader', 'inputs': {'name': V2V_PATCH}}
    wf['902'] = {'class_type': 'MiniMaxH3FunControlNetApply',
                 'inputs': {'model': model_src, 'model_patch': ['901', 0], 'vae': [vvae, 0],
                            'strength': 1.0, 'start_percent': 0.0, 'end_percent': 1.0,
                            'control_video': ['900', 0]}}
    wf[guider]['inputs']['model'] = ['902', 0]
    return wf


def _wait_video(pid, tag, timeout=2400):
    """轮询 ComfyUI history 取 SaveVideo 成片(裸文件名, 位于 output/video/)"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(4)
        h = _up_json('/api/history/' + pid)
        if pid not in h:
            continue
        st = h[pid].get('status', {})
        if st.get('status_str') == 'error':
            raise ValueError(f'{tag}生成失败: ' + json.dumps(st.get('messages', []), ensure_ascii=False)[:400])
        outs = h[pid].get('outputs', {})
        if outs:
            for v in outs.values():
                for fn in (v.get('images') or []):
                    if str(fn.get('filename', '')).lower().endswith('.mp4'):
                        return fn['filename']
    raise ValueError(f'{tag}生成超时')


def v2v_controlnet(req):
    """视频改视频真身: 源片 -> Canny 控制 -> Fun-ControlNet-Union-2.0 逐段重绘 -> 拼接
    动作逐帧复刻源片(替代旧"尾帧续写"近似法); 超14秒自动分段"""
    desc = str(req.get('desc') or '').strip()
    if not desc:
        raise ValueError('先写想把视频改成什么样（新风格/新场景）')
    patch = os.path.join(os.path.dirname(IN_ROOT.rstrip('/\\')), 'models', 'model_patches', V2V_PATCH)
    if not os.path.isfile(patch) or os.path.getsize(patch) < 1e9:
        raise ValueError('Fun-ControlNet-Union-2.0 权重未就绪（需约13.5GB，当前不在或不足1GB）')
    video = req.get('video')
    if isinstance(video, dict) and video.get('data_b64'):
        raw = base64.b64decode(video.get('data_b64') or b'')
        if not raw:
            raise ValueError('没收到视频数据')
        d = os.path.join(IN_ROOT, 'v2v')
        os.makedirs(d, exist_ok=True)
        safe = re.sub(r'[^\w\-.]+', '_', os.path.basename(video.get('name') or 'upload.mp4'), flags=re.UNICODE)[:60] or 'upload.mp4'
        if not safe.lower().endswith(('.mp4', '.mov', '.webm', '.mkv')):
            safe += '.mp4'
        kept = f'v2v_{int(time.time() * 1000)}_{safe}'
        with open(os.path.join(d, kept), 'wb') as f:
            f.write(raw)
        name = 'v2v/' + kept
    elif isinstance(video, dict):
        name = video.get('name')          # 已是服务端落盘名(v2v/ 前缀或成片裸名)
    else:
        name = video
    name = _safe_vid_name(name)
    src = _video_real_path(name)
    sw, sh = upscale720._probe_wh(src)
    total = _probe_duration(src)
    if not total or total < 0.5:
        raise ValueError('读不到源片时长')
    r = sw / sh
    ratio_name = min(V2V_RATIOS, key=lambda t: abs(r - t[1]))[0]
    mp = float(req.get('mp') or 0.4)
    mp = 0.2 if mp <= 0.21 else 0.4   # H3 限 0.4MP
    run = f'{int(time.time() * 1000) % 1000000}'
    tmpd = os.path.join(OUT_ROOT, '_frame_tmp', 'v2v_' + run)
    os.makedirs(tmpd, exist_ok=True)
    # 1) 分段(<=14s, 重编码保证切点帧精确)
    n = int(total // V2V_CHUNK_SEC)
    if total % V2V_CHUNK_SEC >= 1:
        n += 1
    n = max(1, n)
    chunks = []
    for i in range(n):
        cdur = min(V2V_CHUNK_SEC, total - i * V2V_CHUNK_SEC)
        if cdur < 1:
            break
        if n == 1:
            chunks.append((src, total))
            break
        cp = os.path.join(tmpd, f'chunk_{i}.mp4')
        rr = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                             '-ss', f'{i * V2V_CHUNK_SEC:.3f}', '-t', f'{cdur:.3f}', '-i', src,
                             '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p', '-an', cp],
                            capture_output=True, timeout=600)
        if rr.returncode != 0 or not os.path.isfile(cp):
            raise ValueError(f'分段{i + 1}失败: ' + rr.stderr.decode('utf-8', 'replace')[-200:])
        chunks.append((cp, cdur))
    log(f'v2v controlnet: {name} {sw}x{sh} {total:.1f}s -> {len(chunks)}段')
    # 2) 逐段: 抽帧+Canny+ControlNet 重绘
    seed = int(time.time()) % 100000
    outs = []
    try:
        for i, (cp, cdur) in enumerate(chunks):
            fdir = os.path.join(IN_ROOT, 'v2v_ctrl', run, f'c{i}')
            os.makedirs(fdir, exist_ok=True)
            _frames_canny(cp, fdir)
            wf = _v2v_wf(desc, fdir, sw, sh, ratio_name, mp, min(cdur, V2V_CHUNK_SEC), seed + i, run, i)
            pid = _up_json('/api/prompt', {'prompt': wf})['prompt_id']
            log(f'v2v controlnet: 段{i + 1}/{len(chunks)} queued {pid}')
            outs.append(_wait_video(pid, f'改视频段{i + 1}'))
        joined = 'v2v_' + time.strftime('%m%d_%H%M%S') + '.mp4'
        if len(outs) == 1:
            shutil.copy(os.path.join(OUT_ROOT, 'video', outs[0]),
                        os.path.join(OUT_ROOT, 'video', joined))
        else:
            joined = concat_videos({'files': outs, 'name': joined})['name']
        log(f'v2v controlnet: OK -> {joined}')
        return {'ok': True, 'name': joined, 'segs': len(chunks), 'dur': round(total, 1)}
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
        shutil.rmtree(os.path.join(IN_ROOT, 'v2v_ctrl', run), ignore_errors=True)


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == '/canvas_launch':
            # 无限画布(7865) 在跑就返回 already；没在跑就用自身解释器静默拉起（stdlib 服务，无第三方依赖）
            result = {'ok': True, 'already': False, 'url': 'http://127.0.0.1:7865'}
            try:
                urllib.request.urlopen('http://127.0.0.1:7865/api/status', timeout=2)
                result['already'] = True
            except Exception:
                canvas_dir = CANVAS_DIR
                if canvas_dir and os.path.isfile(os.path.join(canvas_dir, 'server.py')):
                    subprocess.Popen([sys.executable, 'server.py'], cwd=canvas_dir,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    result['starting'] = True
                else:
                    result = {'ok': False, 'error': 'canvas server.py not found'}
            data = json.dumps(result, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith('/mixcut_status'):
            data = json.dumps(mixcut_status(), ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith('/mixcut_launch'):
            data = json.dumps(start_mixcut(), ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith('/upscale_progress'):
            data = json.dumps(UPSCALE_STATUS).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith('/progress'):
            data = json.dumps(read_progress() or {}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith('/replicate_progress'):
            import replicate
            body = json.dumps(replicate.STATE, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith('/recent'):
            data = json.dumps(recent_outputs(), ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith('/first_frames'):
            # 首帧库: 放在 input/首帧库/ 的标准形象照会自动出现, 供 i2v/r2v 一键选为主图
            d = os.path.join(IN_ROOT, '首帧库')
            items = []
            try:
                items = sorted(fn for fn in os.listdir(d)
                               if fn.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')))
            except OSError:
                pass
            data = json.dumps({'items': items}, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith('/tail_frame'):
            # 长视频续接: 提成片尾帧存进 input/longvideo/ 供下一段当首帧
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                name = (q.get('file') or [''])[0]
                src = _video_real_path(_safe_vid_name(name))
                # 复用 video_extend_first 的提取逻辑: desc 仅走非空校验; 带源尺寸供前端锁比例
                tail, _, sw, sh = video_extend_first(name, 'tail', 5)
                code = 200
                payload = json.dumps({'ok': True, 'name': tail, 'src_w': sw, 'src_h': sh}, ensure_ascii=False).encode('utf-8')
            except ValueError as e:
                code, payload = 400, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            except Exception as e:
                code, payload = 500, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith(('/api/', '/view')):
            self.proxy('GET')
        else:
            if self.path == '/':
                self.path = '/index.html'
            super().do_GET()

    def do_POST(self):
        if self.path.startswith('/video_replicate'):
            try:
                length = int(self.headers.get('Content-Length') or 0)
                req = json.loads(self.rfile.read(length) or b'{}')
                import base64 as _b64
                import replicate
                d = os.path.join(IN_ROOT, 'replicate')
                os.makedirs(d, exist_ok=True)
                stamp = int(time.time() * 1000)
                vraw = _b64.b64decode(req.get('video_b64') or b'')
                praw = _b64.b64decode(req.get('product_b64') or b'')
                if not vraw or not praw:
                    raise ValueError('视频和商品图都要传')
                vp = os.path.join(d, f'v_{stamp}.mp4')
                pp = os.path.join(d, f'p_{stamp}.png')
                with open(vp, 'wb') as f:
                    f.write(vraw)
                with open(pp, 'wb') as f:
                    f.write(praw)
                box = req.get('box') or [0, 0, 0, 0]
                box = tuple(int(float(v)) for v in box)
                prompt = (req.get('prompt') or
                          '把 <Picture 1> 画面里被替换区域的内容换成 <Picture 2> 中的商品，'
                          '保持同一人物、同一姿态、同一光照和背景，画面风格不变。')
                replicate.spawn(Path(vp), Path(pp), box, prompt,
                                key_step=int(req.get('key_step') or 6),
                                fps=float(req.get('fps') or 0) or None)
                payload = json.dumps({'ok': True}, ensure_ascii=False).encode('utf-8')
                code = 200
            except ValueError as e:
                code, payload = 400, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            except Exception as e:  # noqa: BLE001
                code, payload = 500, json.dumps({'error': str(e)[:200]}, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith(('/video_upscale', '/video_extend')):
            log(f'do_POST hit: {self.path[:80]} ct={self.headers.get("Content-Type")!r} len={self.headers.get("Content-Length")}')
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if length <= 0:
                    raise ValueError('没收到数据')
                ct = self.headers.get('Content-Type') or ''
                try:
                    head = self.rfile.peek(1)[:1]
                except AttributeError:
                    head = b''
                if 'application/json' in ct or (head and head in b'{['):
                    raw_body = self.rfile.read(length) or b'{}'
                    req = json.loads(raw_body)
                    video = req.get('video')
                    if not isinstance(video, dict):
                        tail, dur, sw, sh = video_extend_first(video, req.get('desc'), req.get('dur'))
                        code = 200
                        payload = json.dumps({'ok': True, 'tail': tail, 'dur': dur, 'src_w': sw, 'src_h': sh}, ensure_ascii=False).encode('utf-8')
                    else:
                        import base64 as _b64
                        raw = _b64.b64decode(video.get('data_b64') or b'')
                        if not raw:
                            raise ValueError('没收到视频数据')
                        d = os.path.join(IN_ROOT, 'v2v')
                        os.makedirs(d, exist_ok=True)
                        safe = re.sub(r'[^\w\-.]+', '_', os.path.basename(video.get('name') or 'upload.mp4'), flags=re.UNICODE)[:60] or 'upload.mp4'
                        if not safe.lower().endswith(('.mp4', '.mov', '.webm', '.mkv')):
                            safe += '.mp4'
                        kept = f'v2v_{int(time.time() * 1000)}_{safe}'
                        with open(os.path.join(d, kept), 'wb') as f:
                            f.write(raw)
                        tail, dur, sw, sh = video_extend_first('v2v/' + kept, req.get('desc'), req.get('dur'))
                        code = 200
                        payload = json.dumps({'ok': True, 'tail': tail, 'dur': dur, 'kept': 'v2v/' + kept,
                                              'src_w': sw, 'src_h': sh}, ensure_ascii=False).encode('utf-8')
                else:
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    orig = q.get('f', ['upload.mp4'])[0]
                    if self.path.startswith('/video_upscale'):
                        out = video_upscale(self.rfile, length, orig)
                        code = 200
                        payload = json.dumps(out, ensure_ascii=False).encode('utf-8')
                    else:
                        raise ValueError('video_extend 请用 JSON 提交')
            except ValueError as e:
                code, payload = 400, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            except Exception as e:
                code, payload = 500, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith('/v2v_controlnet'):
            try:
                length = int(self.headers.get('Content-Length') or 0)
                req = json.loads(self.rfile.read(length) or b'{}')
                out = v2v_controlnet(req)
                code, payload = 200, json.dumps(out, ensure_ascii=False).encode('utf-8')
            except ValueError as e:
                code, payload = 400, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            except Exception as e:
                code, payload = 500, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith('/enhance_photo'):
            try:
                length = int(self.headers.get('Content-Length') or 0)
                req = json.loads(self.rfile.read(length) or b'{}')
                name = str(req.get('name') or '')
                if not name or '/' in name or '\\' in name or '..' in name:
                    raise ValueError('bad name')
                face = req.get('face')
                if face is True:
                    face = 'strong'      # 旧前端布尔兼容
                elif face not in ('natural', 'strong', 'off'):
                    face = 'off'
                scale = int(req.get('scale') or 1)
                if scale not in (1, 2, 4):
                    raise ValueError('scale 只支持 1/2/4')
                out = enhance_photo(name, face, scale)
                code = 200
                payload = json.dumps({'ok': True, 'name': out}, ensure_ascii=False).encode('utf-8')
            except ValueError as e:
                code, payload = 400, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            except Exception as e:
                code, payload = 500, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith('/deleteall'):
            n, freed = delete_all_outputs()
            log(f'deleteall {n} files, freed {freed}MB')
            payload = json.dumps({'ok': True, 'n': n, 'freed_mb': freed}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith('/frames'):
            # AI改图: 按每秒 8 张抽帧(2026-10-26 用户定, 24fps 成片=每 3 帧取 1 张)
            try:
                length = int(self.headers.get('Content-Length') or 0)
                req = json.loads(self.rfile.read(length) or b'{}')
                name = str(req.get('name') or '')
                if not name or '/' in name or '\\' in name or '..' in name:
                    raise ValueError('bad name')
                src = os.path.join(OUT_ROOT, 'video', name)
                if not os.path.isfile(src):
                    raise ValueError('成片不存在：' + name)
                pr = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_frames',
                                     '-show_entries', 'stream=nb_read_frames,r_frame_rate',
                                     '-of', 'default=nw=1', src], capture_output=True, timeout=60)
                meta = dict(ln.split('=', 1) for ln in pr.stdout.decode().splitlines() if '=' in ln)
                N = int(meta.get('nb_read_frames') or 0)
                if not N:
                    raise ValueError('读不到视频帧数（ffprobe 失败）')
                # 每秒 8 张: step = 帧率/8(24fps→每 3 帧 1 张); 第 0 帧=原图可作对照
                fps = 24.0
                try:
                    num, _, den = meta.get('r_frame_rate', '').partition('/')
                    fps = float(num) / float(den or 1)
                except (TypeError, ValueError):
                    pass
                step = max(1, int(round(fps / 8.0)))
                picks = list(range(0, N, step))
                if len(picks) > 120:  # 超长成片防爆盘(改图锁 2 秒时约 19 张, 走不到这里)
                    picks = picks[::max(1, len(picks) // 120)][:120]
                sel = '+'.join(f'eq(n\\,{p})' for p in picks)
                safe = re.sub(r'[^\w\-]+', '_', os.path.splitext(name)[0], flags=re.UNICODE)[:30] or 'v'
                os.makedirs(os.path.join(OUT_ROOT, '_frame_tmp'), exist_ok=True)
                outtmpl = os.path.join(OUT_ROOT, '_frame_tmp', f'mu_{int(time.time() * 1000)}_%02d.jpg')
                r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
                                    '-vf', f'select={sel}', '-vsync', '0', '-q:v', '1', outtmpl],
                                   capture_output=True, timeout=120)
                ts = int(time.time() * 1000)
                frames = []
                for i, p in enumerate(picks):
                    tmpf = outtmpl % (i + 1)
                    if not os.path.isfile(tmpf):
                        raise ValueError(f'第{p}帧提取失败：' + r.stderr.decode('utf-8', 'replace')[-200:])
                    fn = f'v2e_{safe}_f{p}_{ts}.jpg'
                    os.replace(tmpf, os.path.join(OUT_ROOT, 'image', fn))
                    frames.append({'frame': p, 'total': N, 'name': fn})
                log(f'frames {name} N={N} -> {[f["name"] for f in frames]}')
                code, payload = 200, json.dumps({'ok': True, 'frames': frames}, ensure_ascii=False).encode('utf-8')
            except ValueError as e:
                code, payload = 400, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            except Exception as e:
                code, payload = 500, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith('/lastframe'):
            tmp = None
            try:
                ct = self.headers.get('Content-Type') or ''
                if ct.startswith('application/json'):
                    # 来源=最近成片: 只接受 output/video 下的文件名
                    length = int(self.headers.get('Content-Length') or 0)
                    req = json.loads(self.rfile.read(length) or b'{}')
                    name = str(req.get('name') or '')
                    if not name or '/' in name or '\\' in name or '..' in name:
                        raise ValueError('bad name')
                    src = os.path.join(OUT_ROOT, 'video', name)
                    if not os.path.isfile(src):
                        raise ValueError('成片不存在：' + name)
                    base = os.path.splitext(name)[0]
                else:
                    # 来源=直接上传的视频字节, 先落临时文件再提
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    orig = os.path.basename(q.get('f', ['upload.mp4'])[0]) or 'upload.mp4'
                    length = int(self.headers.get('Content-Length') or 0)
                    if length <= 0:
                        raise ValueError('没收到视频数据')
                    os.makedirs(os.path.join(OUT_ROOT, '_frame_tmp'), exist_ok=True)
                    tmp = os.path.join(OUT_ROOT, '_frame_tmp', f'in_{int(time.time()*1000)}.mp4')
                    with open(tmp, 'wb') as f:
                        left = length
                        while left > 0:
                            chunk = self.rfile.read(min(1 << 20, left))
                            if not chunk:
                                break
                            f.write(chunk)
                            left -= len(chunk)
                    src = tmp
                    base = os.path.splitext(orig)[0] or 'upload'
                # -sseof -1 跳到倒数1秒 + -update 1 只留最后一帧(真尾帧,不占显卡,秒级)
                safe = re.sub(r'[^\w\-]+', '_', base, flags=re.UNICODE)[:40] or 'video'
                outname = f'lastframe_{safe}_{int(time.time()*1000)}.jpg'
                out = os.path.join(OUT_ROOT, 'image', outname)
                r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                                    '-sseof', '-1', '-i', src, '-update', '1', '-q:v', '1', out],
                                   capture_output=True, timeout=120)
                if r.returncode != 0 or not os.path.isfile(out):
                    raise ValueError('ffmpeg 提取失败：' + r.stderr.decode('utf-8', 'replace')[-300:])
                log(f'lastframe {base} -> {outname}')
                code, payload = 200, json.dumps({'ok': True, 'name': outname}, ensure_ascii=False).encode('utf-8')
            except ValueError as e:
                code, payload = 400, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            except Exception as e:
                code, payload = 500, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            finally:
                if tmp:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith('/delete'):
            length = int(self.headers.get('Content-Length') or 0)
            try:
                req = json.loads(self.rfile.read(length) or b'{}')
                name, sub = str(req.get('name') or ''), str(req.get('sub') or '')
                if sub not in ('video', 'image') or not name or '/' in name or '\\' in name or '..' in name:
                    raise ValueError('bad name')
                p = os.path.join(OUT_ROOT, sub, name)
                if not os.path.isfile(p):
                    raise ValueError('文件不存在')
                move_to_trash(p)
                log(f'delete {sub}/{name}')
                code, payload = 200, json.dumps({'ok': True}).encode('utf-8')
            except Exception as e:
                code, payload = 400, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith('/optimize'):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length)
            try:
                req = json.loads(body)
                comfy_free()   # 优化优先: 先把 ComfyUI 模型请出显存(队列非空时自动跳过)
                wait_vram_free()  # 等 WDDM 显存回收落地, 防 Ollama 加载成半吊子实例
                ollama_unload()  # 再重置 Ollama 模型: 上次的半吊子实例(显存没吃满)不许复用
                out = optimize(req)
                code, payload = 200, json.dumps(out, ensure_ascii=False).encode('utf-8')
            except urllib.error.URLError as e:
                code, payload = 503, json.dumps(
                    {'error': f'本地优化模型连不上（Ollama 未启动？）: {e}'}, ensure_ascii=False).encode('utf-8')
            except Exception as e:
                code, payload = 500, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == '/storyboard':
            # 长视频分镜拆解: 任意总时长 -> N段(每段<=15s, 智能按动作完整性切)
            length = int(self.headers.get('Content-Length') or 0)
            try:
                req = json.loads(self.rfile.read(length) or b'{}')
                out = storyboard_plan(req)
                code, payload = 200, json.dumps(out, ensure_ascii=False).encode('utf-8')
            except urllib.error.URLError as e:
                code, payload = 503, json.dumps(
                    {'error': f'本地优化模型连不上（Ollama 未启动？）: {e}'}, ensure_ascii=False).encode('utf-8')
            except ValueError as e:
                code, payload = 400, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            except Exception as e:
                code, payload = 500, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == '/concat':
            # 长视频拼接: 各段成片 -> concat demuxer 无损秒拼
            length = int(self.headers.get('Content-Length') or 0)
            try:
                req = json.loads(self.rfile.read(length) or b'{}')
                out = concat_videos(req)
                code, payload = 200, json.dumps(out, ensure_ascii=False).encode('utf-8')
            except ValueError as e:
                code, payload = 400, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            except Exception as e:
                code, payload = 500, json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == '/api/prompt':
            ollama_unload()  # 生成优先: 先把 Ollama 模型秒级请出显存, 生成满血
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else None
            want_upscale = False
            if body:
                try:
                    wf = json.loads(body)
                    for n in (wf.get('prompt') or {}).values():
                        inp = n.get('inputs') or {}
                        if n.get('class_type') == 'ResolutionSelector':
                            mp = float(inp.get('megapixels') or 0)
                            if mp >= 0.9:
                                # 0.9MP直出必崩(10-12实测): 改0.6MP直出(10-17 A/B-2实测稳定), 完成后超分合成1080p
                                inp['megapixels'] = 0.6
                                want_upscale = True
                            elif abs(mp - 0.6) < 1e-6:
                                want_upscale = True  # 0.6MP直出档: 完成后轻超分合成1080p(放大仅2.1x,比0.4链的3.3x更真)
                    body = json.dumps(wf).encode('utf-8')
                except Exception as e:
                    log(f'720p intercept skip: {e}')
            try:
                vram_preflight(wf)
            except Exception as e:
                log('VRAM preflight skip: %s' % e)
            data = self.proxy('POST', body=body)
            track_prompt(data)   # 记录本任务 pid: ws 进度过滤 + 日志兜底归属校验的基准
            if want_upscale and data:
                try:
                    pid = json.loads(data).get('prompt_id')
                    if pid:
                        threading.Thread(target=upscale_watcher, args=(pid,), daemon=True).start()
                        log(f'1080p upscale queued pid={pid[:8]}')
                except Exception:
                    pass
            return
        self.proxy('POST')

    def proxy(self, method, body=None):
        url = UPSTREAM + self.path
        if body is None:
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=body, method=method)
        for h in ('Content-Type',):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            with _opener.open(req, timeout=300) as r:
                data = r.read()
                self.send_response(r.status)
                ct = r.headers.get('Content-Type')
                if ct:
                    self.send_header('Content-Type', ct)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return data
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return data
        except Exception as e:
            log(f'proxy err {self.path}: {e}')
            data = str(e).encode('utf-8')
            self.send_response(502)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None


if __name__ == '__main__':
    log(f'panel up :{BIND}:{PORT}  (ComfyUI: {UPSTREAM})')
    threading.Thread(target=ws_listener, daemon=True).start()
    http.server.ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
