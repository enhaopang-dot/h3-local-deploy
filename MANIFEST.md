# 交付包清单与验证状态

打包日期：2026-11-06
包名：H3 本地视频站 · DGX Spark 安装包

---

## 一、目录结构

```
交付包-H3本地部署/
├── README.md                      人读版，五步装完
├── 00-AGENT-INSTALL.md            给 Agent 执行的主文档，带每步验收
├── 01-部署路线选择.md             什么硬件走什么路线，什么任务用什么模型
├── 02-性能预期与两台并联.md        单台耗时推算、两台并联的真实收益
├── MANIFEST.md                    本文件
├── scripts/
│   ├── 00_check_env.sh            装前体检，只读不写
│   ├── 01_install.sh              装 ComfyUI + PyTorch cu130
│   ├── 02_download_models.sh      下模型，约 66GB，支持断点续传
│   ├── 03_start_comfyui.sh        前台启动
│   ├── 04_smoke_test.py           最小出片验收
│   ├── 05_benchmark.py            三档标准测试，出本机真实耗时
│   ├── 06_install_services.sh     注册 systemd 用户服务
│   └── 07_doctor.py               故障体检，按症状给下一步
└── panel/
    ├── server.py                  面板后端（8199），路径已改成环境变量
    ├── index.html                 面板前端，单文件
    ├── guide.js                   面板内置使用说明
    ├── replicate.py               视频复刻后端
    ├── upscale720.py              超高分辨率补丁链路（Spark 上可不装）
    ├── face_restore.py            人脸修复（需 gfpgan 权重，未随包）
    ├── fix_templates_for_os.py    按当前系统改模型路径分隔符
    ├── start_panel.sh             面板启动脚本
    ├── panel_config.json          面板配置，路径留空走默认
    └── template_*.json            五套工作流模板（t2v/i2v/r2v/t2i/i2e）
```

---

## 二、验证状态（哪些真跑过，哪些没有）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 模型文件名、大小、目录结构 | 实测 | 在原始 Windows 机器上加载出片验证过 |
| ComfyUI 原生 H3 节点存在 | 实测 | 原机 ComfyUI v0.42.0 上确认 `MiniMaxH3ImageToVideo` |
| 16GB 显卡的启动参数与耗时 | 实测 | 3s/0.2MP/8 步 90 秒；10s/0.4MP/20 步 21 分钟 |
| DGX Spark 的安装命令 | **未实测** | 按 2026-10-27 的公开 ARM64 教程与 NVIDIA 文档整理，未在真机跑过 |
| DGX Spark 的 10s 720P 耗时 | **推算** | 由 Wan2.2 公开实测按参数量折算，误差 ±50% |
| 面板在 Linux 上运行 | **未实测** | 原版在 Windows 验过；路径与分隔符已改，仍需真机验 |
| 脚本语法 | 已检查 | 全部 `bash -n` / `py_compile` 通过 |
| 模板改写逻辑 | 已干跑 | 在本机加载模板、套用冒烟参数、序列化通过 |
| 端到端出片 | **未在 Spark 上跑** | 交付方需按第 6 节跑一次 `04_smoke_test.py` |

结论：这套包是「可执行 + 需现场验收」，不是「装完即用」。第一次装的人必须跑冒烟测试，把结果记下来。

---

## 三、脱敏处理

已处理：

1. **机器与账号**：去掉机器名、Windows 用户名、家目录绝对路径。
2. **个人项目引用**：去掉「画布工坊」「MoneyPrinterTurbo」等本机其他项目的名字和路径，改成通用描述。
3. **绝对路径**：`D:\AI\ComfyUI\...` 全部改成 `$ROOT`、环境变量或相对路径。
4. **启动脚本**：Windows 的 bat、PowerShell 守护、进程杀手规避、页面文件修复、GBK 编码处理全部不带（这些是 Windows 特有的，Spark 上不存在）。
5. **凭证**：包内无 API key、token、账号密码、cookie。
6. **对话与迭代记录**：原始项目的过程记录、个人备注、沟通记录一律未打包。

保留的：

- 模型仓库名和文件名（公开资源，必须保留才能下载）。
- ComfyUI 的 commit 号、PyTorch 版本号（版本锁，必须保留）。
- 面板里的中文界面和功能名（「首帧库」这类是功能名，不是个人信息）。

---

## 四、来源与时效

- 模型：Comfy-Org/MiniMax-H3、PulpCut/MiniMax-H3-Turbo-INT8-ConvRot（公开仓库）
- ComfyUI：comfyanonymous/ComfyUI，锁 commit `e06965c4d6e07a546b91a70b4c6a0ec54bbdceb8`
- ARM64 安装口径：公开教程（2026-10-27）+ NVIDIA DGX Spark 文档
- 性能锚点：见 `02-性能预期与两台并联.md` 第六节

时效提醒：本文档所有「不支持 / 拿不到 / 只能这样」的结论，都是 2026-11-06 查到的口径。平台发版、模型更新后要重新核对，不要把一次实查当成永久事实。

---

## 五、文件校验

见文末 `SHA256SUMS`。校验方法：

```bash
sha256sum -c SHA256SUMS
```
