# MiniMax H3 本地部署包 · 一键把视频生成模型装到自己的机器上

> 不订阅、不排队、不按条计费。把 MiniMax H3 跑在你自己的显卡上，配一个 Web 面板，点几下就出片。

**关键词**：MiniMax H3 · 本地部署 · AI 视频生成 · Local Video Generation · ComfyUI · 图生视频 · 文生视频 · 参考图生视频 · 视频复刻 · INT8 量化 · CUDA · PyTorch · DGX Spark · ARM64 · 16GB 显存 · 自托管 · Self-hosted · 工作流模板 · i2v · t2v · r2v

---

## 这是什么

一套把 MiniMax H3 视频模型部署到本地机器的完整工具包，包含：

- **部署脚本**：环境体检 → 装 ComfyUI → 下模型 → 启动 → 冒烟测试 → 性能基准 → 注册服务 → 故障诊断，八步全自动
- **Web 面板**：单文件前端 + FastAPI 后端，浏览器里选模板、填提示词、出片
- **五套工作流模板**：文生视频（t2v）、图生视频（i2v）、参考图生视频（r2v）、文生图（t2i）、图生表情（i2e）
- **诊断工具**：`07_doctor.py` 按症状给下一步，不用猜

## 为什么自己做

云端视频 API 按条计费，长视频更贵；本地跑一次成本是电费和显卡折旧。素材敏感、不想上云的场景，本地是唯一选择。

## 硬件支持

| 路线 | 状态 | 说明 |
|---|---|---|
| Windows + 16GB 显卡 | **已实测** | 原机 ComfyUI v0.42.0 出片验证 |
| NVIDIA DGX Spark（ARM64） | 脚本已备，**未在真机跑过** | 按公开教程与 NVIDIA 文档整理 |

真实性能（16GB 显卡实测）：

- 3 秒 / 0.2MP / 8 步：**约 90 秒**
- 10 秒 / 0.4MP / 20 步：**约 21 分钟**

DGX Spark 的 10 秒 720P 耗时是按参数量推算的，误差 ±50%，**不要当成承诺**。

## 快速开始

```bash
# 0. 先体检，只读不写
bash scripts/00_check_env.sh

# 1. 装 ComfyUI + PyTorch
bash scripts/01_install.sh

# 2. 下模型（约 66GB，支持断点续传）
bash scripts/02_download_models.sh

# 3. 启动
bash scripts/03_start_comfyui.sh
```

看到 `To see the GUI go to: http://127.0.0.1:8188` 就是起来了。首次加载模型要 1–3 分钟。

```bash
# 4. 冒烟测试：最小出片验收
python scripts/04_smoke_test.py

# 5. 出本机真实耗时
python scripts/05_benchmark.py
```

### 起面板

```bash
bash panel/start_panel.sh
# 浏览器打开 http://127.0.0.1:8199/
```

## 目录

```
├── 00-AGENT-INSTALL.md    给 Agent 执行的主文档，每一步带验收标准
├── 01-部署路线选择.md      什么硬件走什么路线，什么任务用什么模型
├── 02-性能预期与两台并联.md 单台耗时推算、双机并联的真实收益
├── MANIFEST.md            清单、验证状态、脱敏说明
├── scripts/               八个部署与诊断脚本
└── panel/                 面板后端 + 前端 + 工作流模板
```

## 验证状态（哪些真跑过）

这份包是「可执行 + 需现场验收」，不是「装完即用」。

| 项 | 状态 |
|---|---|
| 模型加载、出片 | 已在 16GB 显卡实测 |
| 脚本语法 | 全部 `bash -n` / `py_compile` 通过 |
| DGX Spark 安装 | 未实测，需现场跑 `04_smoke_test.py` |
| 面板在 Linux 运行 | 未实测，路径已改成环境变量 |

**第一次装的人必须跑冒烟测试，把结果记下来。**

## 需要的模型

- `Comfy-Org/MiniMax-H3`（公开）
- `PulpCut/MiniMax-H3-Turbo-INT8-ConvRot`（INT8 量化版，公开）

## 常见问题

**Q：显存不够怎么办？**
A：用 INT8 量化版，或降分辨率先跑通再往上加。

**Q：能不能不用 ComfyUI？**
A：面板依赖 ComfyUI 的工作流接口，换后端需要自己改 `panel/server.py`。

**Q：模型下载很慢？**
A：`02_download_models.sh` 支持断点续传，中断后重跑即可。

## 声明

包内不含任何 API 密钥、账号、cookie。模型权重不随包分发，按脚本从公开仓库下载。

部署结论有时效：文中所有「不支持 / 只能这样」的说法基于 2026-11-06 的公开信息，模型和框架更新后请自行复核。

## 授权

MIT License

## 联系

卡住了，或想在团队里落地，加微信：**enhaopang**

添加时请备注：GitHub-h3-local-deploy

> 不提供免费技术支持。问题请走 Issue。
