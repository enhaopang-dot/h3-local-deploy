# MiniMax H3 本地视频站 · 安装总纲（给 Agent 执行）

> 读这份文档的是 AI Agent。按顺序执行，每节末尾有验收命令和通过标准。
> 文档版本：2026-11-06 ｜ 目标系统：NVIDIA DGX Spark（GB10，aarch64，Ubuntu 24.04）
> 全部路径用占位符：`$ROOT` = 安装根目录，默认 `$HOME/AI/minimax-h3`

---

## 0. 执行纪律（先读，违反会装坏）

1. **按顺序做，不跳步。** 每节有「验收」，输出不对就停，把原始报错贴出来，不要自己改参数硬试。
2. **不要升级 torch 和 ComfyUI。** 这套组合是锁死版本的，`pip install -U` 会把 ARM64 的 CUDA 轮子换掉。
3. **不要加 `--lowvram`。** Spark 是 128GB 统一内存，整模常驻；加 lowvram 反而把带宽优势打没。
4. **所有 `pip install` 都要带 `-c $ROOT/torch-constraints.txt`。** 很多自定义节点会在 requirements 里写 `torch`，不带约束会把 CUDA 版换成 CPU 版。
5. **下载中断就重跑同一条命令。** `hf download` 自带断点续传，不要删半成品。
6. **每一步只认命令输出，不认“应该可以”。** 验收不过就是没过。

---

## 1. 这套东西是什么

一条本地视频生产线，三个进程：

| 进程 | 端口 | 作用 |
| --- | --- | --- |
| ComfyUI | 8188 | 真正跑模型的引擎，加载 MiniMax H3 33B |
| H3 面板 | 8199 | 网页面板，上传图、填提示词、看进度、下载成片 |
| Ollama（可选） | 11434 | 只给面板的「AI 优化描述」用，不装也能出片 |

模型是 **MiniMax H3 33B 的 INT8 剪枝版**，加一个 NVFP4 文本编码器和两个 VAE，总共约 44GB。另有一个「预焊 turbo」整模，8 步出片，用于快速试构图。

为什么选 INT8：满血 BF16 要 130GB 显存，Spark 的 128GB 统一内存刚好不够；INT8 画质大约是满血的 90–95%，出片速度能用。2K 和提示词编排（H3-Context-IR）官方没开源，只能走云端 API。

---

## 2. 先体检

```bash
bash scripts/00_check_env.sh
```

通过标准：结尾打印「环境检查通过」，退出码 0。

硬门槛三条，缺一条就别往下装：

- 架构 `aarch64`（Spark 是 ARM，Windows 和 x86 的轮子一律装不上）
- 内存 ≥100GB（`free -g` 看 total）
- 家目录可用 ≥120GB

`python3.12`、`git`、`ffmpeg` 缺失不算失败，下一步会装。

---

## 3. 装 ComfyUI 和 PyTorch

```bash
bash scripts/01_install.sh
```

这一步做六件事：apt 依赖 → Python 3.12 venv → torch 2.10.0+cu130 → 克隆 ComfyUI 并锁到指定 commit → 装依赖（带 torch 约束）→ 自检。

**验收**：结尾出现下面三行，且没有 `NOT AVAILABLE`。

```text
  python : 3.12.x
  torch  : 2.10.0+cu130 cuda: 13.0
  gpu    : NVIDIA GB10
  bf16 matmul: OK
```

**为什么锁 `e06965c4d6e07a546b91a70b4c6a0ec54bbdceb8`**：这个 commit 已经内置了原生 H3 节点（`comfy_extras/nodes_minimax_h3.py`），文生视频和图生视频不需要任何第三方节点。更老的版本没有这些节点，会白装。

**常见失败**：

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `not a supported wheel on this platform` | 用了 x86_64 或 Windows 的轮子 | 确认 `uname -m` 是 aarch64，重跑本步 |
| `CUDA available: False` | torch 装成了 CPU 版 | `python -m pip list` 看 torch 版本，重装 cu130 |
| `comfy-aimdo` 编译失败 | 缺 python3.12-dev / build-essential | `sudo apt install python3.12-dev build-essential` 后重跑 |
| `pip check` 报 cusparselt 不支持 | `manylinux2014_sbsa` 标签误报 | 不用管，用 bf16 matmul 自检为准 |

---

## 4. 下模型（约 66GB）

```bash
bash scripts/02_download_models.sh
```

默认下五个文件：

| 文件 | 大小 | 放哪 | 作用 |
| --- | ---: | --- | --- |
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 21.0GB | `models/diffusion_models/MiniMax-H3/` | 基模，20 步，画质上限 |
| `minimax_h3_fl2va_pruned_turbo_int8_convrot.safetensors` | 21.0GB | 同上 | 预焊 turbo，8 步，日常用 |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 15.7GB | `models/text_encoders/MiniMax-H3/` | 文本编码器 |
| `minimax_h3_video_vae_fp16.safetensors` | 5.2GB | `models/vae/MiniMax-H3/` | 视频解码 |
| `minimax_h3_audio_vae_fp32.safetensors` | 0.6GB | `models/vae/MiniMax-H3/` | 音频解码（出片带声音） |

可选：

```bash
bash scripts/02_download_models.sh --with-ref2v   # 参考生视频，+21GB
bash scripts/02_download_models.sh --with-image   # 生图 + 改图，+12GB
```

**网络**：脚本先试 huggingface.co，5 秒不通自动切 `hf-mirror.com`。国内直连 HF 基本不通，走镜像时注意 `HF_HUB_DISABLE_XET=1` 必须开，否则 xet 分片下载会 401。

**验收**：脚本结尾五个 `[OK]`，大小对得上。少 1GB 以上就删掉重下，不要凑合——半截 safetensors 会在加载时报 `header too large`。

---

## 5. 启动

```bash
bash scripts/03_start_comfyui.sh
```

看到 `To see the GUI go to: http://127.0.0.1:8188` 就是起来了。首次加载模型要 1–3 分钟，别急着重启。

启动参数只有三个，别加：

- `--listen 127.0.0.1`：只给本机访问。要给局域网用，改成 `0.0.0.0`，同时确认防火墙。
- `--port 8188`：固定端口，面板靠它找服务。
- `--use-pytorch-cross-attention`：Spark 上 xformers 要自己编译，直接用 PyTorch 原生注意力最稳。

**不要加 `--lowvram`、`--normalvram`、`--reserve-vram`**，那是 16GB 显卡的补丁，在统一内存机器上是负优化。

---

## 6. 冒烟测试（必做）

另开一个终端：

```bash
python3 scripts/04_smoke_test.py
```

它会：等 8188 起来 → 检查原生 H3 节点 → 用 `panel/template_t2v.json` 提交一条 2 秒 0.2MP 的片子 → 轮询到出片 → 校验 mp4 非空。

**通过标准**：

```text
[PASS] 出片成功，耗时 XX.X 秒
       文件: smoke/H3_smoke_00001_.mp4  X.XX MB
```

Spark 上首次含模型加载，预计 2–5 分钟；第二次起 1 分钟左右。超过 15 分钟没出片算失败。

失败时先跑：

```bash
python3 scripts/07_doctor.py
```

---

## 7. 装面板（可选，实验性）

面板是一个 8199 的网页壳，转发到 ComfyUI 8188，额外提供：上传首帧、真实进度条、成片在线播放、长视频分段续接、视频复刻、历史记录。

```bash
$ROOT/.venv/bin/python panel/fix_templates_for_os.py
bash panel/start_panel.sh
```

然后浏览器开 `http://127.0.0.1:8199/`。

**必须知道的四件事**：

1. 面板原来是在 Windows + 16GB 显卡上调的，**Linux 分支没有在真机上跑过**。交付包里已经把硬编码路径改成读环境变量、模板分隔符改成当前系统，但第一次跑仍要按下面清单验一遍。
2. 面板模板默认选「turbo 8 步」。没下 turbo 整模时，面板会走基模 20 步，速度慢约 2.5 倍。
3. 「高清 0.9MP」档在 16GB 卡上要靠 Real-ESRGAN 超分补，Spark 上不需要这个补丁；没装 ncnn 二进制时这个档会失败，用「标清 0.4MP」或直接 0.9MP 原生出图。

4. 「✨ AI 优化描述」依赖本机 Ollama（qwen3-vl 或 qwen3.5 视觉模型），本包不带；没装只是这个按钮不能用，出片不受影响。「照片清晰化」要 GFPGAN 权重（约 350MB），也没随包，需要时再单独下。

面板首次验收清单：

- [ ] 页面打开不白屏
- [ ] 右上角显示「本地 H3 服务：在线」
- [ ] 「文字生视频」提交一条 2 秒 0.2MP，进度条从 0 走到 100%
- [ ] 成片能在页面里播放
- [ ] 「图片生视频」上传一张图能提交

五项里有任何一项不过，先看 `panel/panel.log`，再对照第 11 节。

---

## 8. 常驻成服务

```bash
bash scripts/06_install_services.sh
```

生成两个 systemd 用户服务：`comfyui-h3` 和 `h3-panel`，开机自启、崩了 10 秒后自动重拉。

```bash
systemctl --user status comfyui-h3
journalctl --user -u comfyui-h3 -f
```

卸载：`bash scripts/06_install_services.sh --uninstall`。

注意：用户服务要 `loginctl enable-linger $USER` 才能在 SSH 退出后继续跑，脚本会自动执行，失败会提示。

---

## 9. 日常怎么用

三档清晰度，按用途选：

| 档位 | 分辨率（16:9） | 8 步耗时（Spark 估算） | 什么时候用 |
| --- | --- | --- | --- |
| 流畅 0.2MP | 608×352 | 1–2 分钟 | 试构图、试动作、批量抽卡 |
| 标清 0.4MP | 864×480 | 2–4 分钟 | 日常交付、发短视频 |
| 高清 0.9MP | 1280×720 | 6–9 分钟 | 成品、投流素材 |

工作流固定：**先用流畅档试，动作和构图满意了，再用同一段提示词跑标清或高清。** 不要一上来就 720p 20 步，一条十几分钟，改一次重跑一次，时间全浪费在等待上。

步数怎么选：

- **8 步 turbo**：日常默认，速度快，细节略软。
- **20 步基模**：人物特写、产品细节、要放大看的镜头才用，耗时约 2.5 倍。

时长：1–15 秒自由填，建议 2–10 秒。超过 10 秒优先用面板的「长视频模式」，它会自动拆段、上段尾帧当下段首帧，比硬跑一条 15 秒稳。

提示词只记三条：

1. 先写镜头（景别、机位、运动），再写主体，再写环境，最后写光线和质感。
2. 有人的镜头必须写「面部特征保持一致、五官不漂移」，否则跨镜头会换脸。
3. 要声音就在描述里写清环境音和动作音，音频 VAE 会自动出，不用另外配。

---

## 10. 运维

```bash
python3 scripts/07_doctor.py        # 出问题先跑，按提示走
python3 scripts/05_benchmark.py     # 跑三档标准测试，拿本机真实耗时
```

`05_benchmark.py` 会依次跑 2s/0.2MP、5s/0.4MP、10s/0.9MP，结果写 `scripts/benchmark-result.json`。装完机器先跑一次，把三个数字记下来，以后“变慢了”才有对照。

清理：成片在 `$ROOT/ComfyUI/output/video/`，面板删除先进 `output/_trash/`，保留 7 天。磁盘紧张时先清 `_trash` 和 `output/bench/`。

---

## 11. 故障排查

按症状查，不要从头读。

| 症状 | 先查 | 处理 |
| --- | --- | --- |
| `CUDA available: False` | `nvidia-smi` 是否有输出 | 驱动没起；重启后重试 |
| 提交任务报 `value_not_in_list` | 模型文件名与模板是否一致 | 跑 `panel/fix_templates_for_os.py`；确认模型在对应子目录 |
| 报 `out of memory` | `free -g` 可用内存 | 关掉其他吃内存的进程；Spark 是统一内存，浏览器和 Docker 都算 |
| 出片没声音 | 音频 VAE 是否加载 | 检查 `minimax_h3_audio_vae_fp32.safetensors` 是否在 `models/vae/MiniMax-H3/` |
| 采样到一半进程消失 | `dmesg -T` 末尾 | 多半是内存打满被 OOM killer 杀；减分辨率或时长 |
| 首次采样特别慢，之后正常 | 正常现象 | sm_121 的部分算子走 PTX JIT，第一次编译慢 |
| `No module named 'comfy_aimdo'` | 用了系统 python | 必须用 `$ROOT/.venv/bin/python` |
| 面板显示离线但 8188 正常 | 面板连的是系统代理 | 面板已强制回环直连；确认 `H3_COMFY_URL` 没写错 |
| `pip check` 报一堆冲突 | 自定义节点改过 torch | `pip install -r requirements.txt -c torch-constraints.txt` 重装 |
| 下载卡在 0% | 网络 | 手动 `export HF_ENDPOINT=https://hf-mirror.com` 重跑 |

---

## 12. 不要做的事

- 不要 `pip install -U torch`，会换掉 ARM64 CUDA 轮子。
- 不要用系统 Python 跑 ComfyUI 或面板，缺 `comfy_aimdo`。
- 不要在 Spark 上照抄 16GB 显卡的 `--lowvram --reserve-vram 7`。
- 不要把模型放在 NFS / 移动硬盘上跑，加载会慢到怀疑人生。
- 不要并行提交两条 720p 任务，统一内存会被吃满，两条一起挂。
- 不要把 `panel/` 里的 `panel_config.json` 提交到公开仓库，里面会写本机路径。

---

## 附录 A：不是 Spark 的机器怎么办

这套文档的主线只对 **128GB 统一内存的 ARM64 Linux** 成立。其他机器看 `01-部署路线选择.md`：

- Windows + 16GB 显卡：走 INT8 + lowvram + turbo 8 步，参数完全不同。
- 12GB 以下：不建议本地跑 33B，改云端或小模型。
- 24GB 以上独显：可以关掉 lowvram，分辨率提到 0.9MP，其余同 Windows 路线。
- Mac：官方不支持，别试。

## 附录 B：版本锁定清单

| 组件 | 版本 / commit | 锁定原因 |
| --- | --- | --- |
| Python | 3.12 | 3.13 的部分轮子在 aarch64 上没有 |
| PyTorch | 2.10.0+cu130 | cu128 对 sm_121 支持不全 |
| torchvision | 0.25.0+cu130 | 跟 torch 配套 |
| ComfyUI | `e06965c4d6e07a546b91a70b4c6a0ec54bbdceb8` | 内置原生 H3 节点 |
| Comfy-Org/MiniMax-H3 | `e5eb578a89295337b8ff433a035929ce0279e0b6` | 模型文件版本 |
| 面板 | 随包 | 基于 Windows 版改造，Linux 未真机验证 |

## 附录 C：这份包做了什么改动

原始环境是 Windows 11 + RTX 3080 Ti Laptop 16GB。搬到 Spark 做了这些适配：

1. 启动参数去掉 lowvram / reserve-vram / cache-ram，改整模常驻。
2. PyTorch 从 cu128 换 cu130（sm_121 需要）。
3. 面板硬编码路径改成环境变量 + `panel_config.json`。
4. 模板模型路径按当前系统分隔符重写。
5. 去掉 Windows 专属的进程守护、TCC/页面文件修复、GBK 编码 bat。
6. 文档里的机器名、用户名、内部路径、个人项目名全部替换成占位符。

对应地，**Windows 那套的很多坑在这台上不存在**：没有进程杀手、不需要页面文件、不需要锁频、不需要 GBK bat。反过来 Spark 有自己的坑：sm_121 的 PTX JIT、aarch64 轮子、onnxruntime-gpu 没有 ARM 版。两边的坑不要互相套。
