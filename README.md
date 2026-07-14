# Local AI API Gateway

> 本地 AI 工作流 API 网关 — 通过 Cloudflare Quick Tunnel 暴露临时公网 URL，以 OpenAI 兼容 API 提供本机 AI 能力。

## 是什么

```text
本机显卡 / 本地工作流
    ↓
Local AI API Gateway
    ↓
Cloudflare Quick Tunnel 临时公网 URL
    ↓
外部服务端 / 其他程序
```

外部调用方只需要 `Base URL + API Key` 即可调用本机能力。

## 快速开始

### 1. 环境要求

- Windows 10 / 11 x64
- NVIDIA GPU（RTX 20 系及以上，8GB+ 显存）
- Python 3.10+

### 2. 安装依赖

推荐从 [GitHub Releases](https://github.com/Yaro-lu/API/releases) 下载与客户端提示同名的
`runtime-nvidia-rtx30plus-cu130-v*.7z` 便携环境包，并同时下载对应的 `.sha256` 文件。
客户端支持在线下载、本地选择和同目录自动安装；安装时会校验 SHA256、压缩包目录结构和安装结果。

环境包包含便携 Python、`.venv` 依赖、ComfyUI 与 Cloudflared，不包含任何模型、API Key、账户会话、请求记录或生成结果。
模型应单独放在客户端根目录的 `models/` 中。

开发模式也可以自行安装依赖：

```bash
pip install -r requirements.txt
```

### 3. 启动

```bash
# 仅 API（无 GUI）
python -m app.main

# API + GUI
python -m app.gateway_app
```

### 4. 调用

启动后，GUI 会显示 Base URL 和 API Key：

```bash
# 健康检查
curl http://127.0.0.1:18188/health

# 查询模型
curl http://127.0.0.1:18188/v1/models \
  -H "Authorization: Bearer sk-local-xxx"

# 文字对话
curl -X POST http://127.0.0.1:18188/v1/chat/completions \
  -H "Authorization: Bearer sk-local-xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"local-text-default","messages":[{"role":"user","content":"Hello"}]}'

# 首尾帧视频
curl -X POST http://127.0.0.1:18188/v1/videos/generations \
  -H "Authorization: Bearer sk-local-xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"seedance-1.5-pro","prompt":"...","first_frame_url":"...","last_frame_url":"..."}'

# 查询任务
curl http://127.0.0.1:18188/v1/tasks/{task_id} \
  -H "Authorization: Bearer sk-local-xxx"
```

## API 端点

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | /health | 否 | 健康检查 |
| GET | /v1/models | 是 | 模型列表 |
| POST | /v1/chat/completions | 是 | 文字对话 |
| POST | /v1/videos/generations | 是 | 首尾帧视频 |
| GET | /v1/tasks/{task_id} | 是 | 任务查询 |
| GET | /v1/files/{task_id}/{filename} | 是 | 文件下载 |

## 配置

编辑 `runtime/config.local.json` 修改端口、并发、ComfyUI 地址、模型别名等。

## 安全提示

- 本程序启动后会通过 Cloudflare Quick Tunnel 生成临时公网 URL，该 URL 可被公网访问
- 请妥善保管 API Key
- 程序默认保存请求、输入图片、提示词、生成结果到 `runtime/` 目录
- Quick Tunnel URL 每次启动可能变化，不适合作为长期生产域名
- 如需持久域名，请自行配置 Cloudflare Named Tunnel

## 目录结构

```
├─ app/
│  ├─ main.py              # API 入口
│  ├─ gateway_app.py       # GUI 入口
│  ├─ config.py            # 配置加载
│  ├─ api/                 # API 路由
│  ├─ core/                # 核心模块
│  ├─ tunnel/              # Cloudflare Tunnel
│  ├─ adapters/            # 模型适配器
│  ├─ engines/             # 执行引擎
│  ├─ workflows/           # 工作流定义
│  ├─ gui/                 # GUI
│  └─ worker_service/      # 原有 Worker 服务模块
├─ bin/                    # cloudflared.exe
├─ runtime/                # 运行时数据（不上传 Git）
└─ config/                 # 原有配置文件
```

## 版本

v0.1 — 首次发布，支持文字 Mock + 视频首尾帧 T2V + Cloudflare Quick Tunnel
