# 灵境造片厂本地客户端

灵境造片厂是一个运行在 Windows 电脑上的本地 AI API 客户端。它把 ComfyUI 工作流统一成 `URL + API Key`，让网页、业务服务或其他软件无需安装模型环境，也能调用这台电脑上的文字、图片和视频能力。

```text
调用方（网页 / 服务端 / 其他软件）
        │  URL + API Key
        ▼
灵境造片厂客户端 ── 工作流路由 ── ComfyUI / 本地模型
        │
        └── Cloudflare Tunnel（需要公网访问时）
```

## 使用者快速开始

### 系统要求

- Windows 10 22H2 或 Windows 11，64 位
- NVIDIA RTX 20 系列或更高、显存 8GB 或以上；本版 CUDA 13 运行环境要求使用 580 或更高版本驱动
- 模型所需的磁盘空间和显存
- 建议放在固态硬盘中，实测冷启动模型加载速度和机械硬盘相差7倍以上

轻量客户端安装包包含打开完整界面所需的便携 Python、Tk 和 7-Zip，不需要用户另装 Python。ComfyUI、Torch、CUDA 与 Cloudflared 位于独立运行环境包中；客户端和环境包都不包含模型或用户数据。

### 发布包结构

```text
LingJingAI-Setup-1.0.0-win-x64.exe                 轻量客户端
runtime-nvidia-rtx20plus-cu130-v1.0.0.7z           独立运行环境包
```

- 轻量客户端可以在全新 Windows 10 上直接打开，并提供环境安装与维护界面。
- 独立运行环境包只在首次安装、修复或环境版本升级时下载。


### 第一次使用

1. 安装并启动轻量客户端。
2. 在“模型与环境”中点击“一键修复”，客户端会自动拉取、校验并安装独立运行环境包。
3. 如果因网络问题拉取失败，再从失败弹窗复制并打开项目主页 `https://github.com/Yaro-lu/API`，手动下载环境包。
4. 手动下载时，回到客户端选择本地 `.7z` 环境包安装。安装根目录的《灵境造片厂使用教学.pdf》提供完整图文步骤和接口说明。
5. 把模型放入或导入 `models/`，确认所需工作流显示“可以使用”。
6. 选择默认工作流，回到“控制台”复制公网 URL 和 API Key。
7. 双击桌面或安装根目录中的《灵境造片厂示例页》，填入 URL 和 Key；页面会自动检测客户端可用的文字、图片和视频模型。
8. 也可以把 URL 和 Key 填入其他需要调用本机 AI 的网页或软件。

客户端启动后会自动管理 API、ComfyUI 和临时公网通道。关闭主窗口会结束由客户端启动的后台进程。

《灵境造片厂示例页》是完全本地的纯前端文件，不包含服务端代码，也不会替代运行环境或模型。API Key 仅保留在当前浏览器会话中。


## 调用方式

以下示例中的 `BASE_URL` 和 `API_KEY` 请替换为控制台显示的值。

### 默认工作流

请求根 URL 时，使用当前选中的默认工作流：

```bash
curl -X POST "BASE_URL/" \
  -H "Authorization: Bearer API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一只在窗边晒太阳的猫"}'
```

### 指定工作流

工作流公开名称可以直接拼在 URL 后。例如公开名称为 `flux2`：

```bash
curl -X POST "BASE_URL/flux2" \
  -H "Authorization: Bearer API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"电影感城市夜景"}'
```

### 兼容接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/healthz` | 无鉴权、仅返回最小存活状态 |
| `GET` | `/v1/status` | 客户端完整运行状态 |
| `GET` | `/v1/models` | 可调用模型列表 |
| `GET` | `/v1/workflows` | 可调用工作流列表 |
| `POST` | `/v1/chat/completions` | DeepSeek / OpenAI 风格文字接口 |
| `POST` | `/api/v3/images/generations` | 即梦 / 火山风格图片接口 |
| `POST` | `/v1/workflows/run/{workflow_id}` | 直接运行指定工作流 |
| `GET` | `/v1/tasks/{task_id}` | 查询异步任务 |
| `GET` | `/v1/files/{task_id}/{filename}` | 下载该任务生成的文件 |

生成请求统一按异步任务处理。接口会先返回任务 ID；调用方随后查询任务状态并获取结果，避免长时间生成造成连接超时。

## 工作流与模型

- 每个工作流位于 `workflows/<工作流名称>/`，至少包含 `manifest.json` 和 `workflow.json`。
- `manifest.json` 定义英文公开名称、输出类型、输入参数和模型依赖。
- 模型统一放在根目录 `models/`，客户端会把该目录提供给 ComfyUI。
- 模型名称必须与工作流内使用名称一致
- 推荐只使用可信来源的 `safetensors` 或 `GGUF` 模型。传统 PyTorch checkpoint 可能包含可执行反序列化内容，不应导入来历不明的文件。
- 工作流、运行环境和模型彼此分离；更新客户端不会删除模型。

## 本地启动与诊断

源码目录已经存在完整便携环境时，可以运行：

```bat
start.bat
```

环境诊断：

```bat
check-env.bat
```

Trae / VS Code 应使用 `runtime\python\python.exe`。仓库中的 `.vscode/settings.json` 已固定该解释器；不要选择 `.venv\Scripts\python.exe`，便携环境只使用 `.venv\Lib` 保存依赖。



## 安全与隐私

- 生成 API Key 和本机管理 Key 权限分离；管理接口不应交给外部调用方。
- Windows 下的本地凭据使用当前用户 DPAPI 保护，日志不会输出完整 Key。
- 远程账号服务默认只接受 HTTPS；仅回环地址允许 HTTP 开发调试。
- 发行脚本使用白名单构建，并拒绝 ComfyUI、Torch/CUDA、模型、账号会话、请求、日志和生成结果进入轻量安装包。
- 公网 URL 仍然是互联网入口，请妥善保管 API Key，不要把它提交到代码仓库或公开截图。

## 发布说明

本仓库已公开，但尚未添加开源许可证，仅作学习使用

当前版本：`1.0.0`
