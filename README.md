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

轻量客户端安装包包含打开完整界面所需的便携 Python、Tk 和 7-Zip，不需要用户另装 Python。ComfyUI、Torch、CUDA 与 Cloudflared 位于独立运行环境包中；客户端和环境包都不包含模型或用户数据。

### 发布包结构

```text
LingJingAI-Setup-1.0.0-win-x64.exe                 轻量客户端
runtime-nvidia-rtx20plus-cu130-v1.0.0.7z           独立运行环境包
```

- 轻量客户端可以在全新 Windows 10 上直接打开，并提供环境安装与维护界面。
- 独立运行环境包只在首次安装、修复或环境版本升级时下载。
- 模型、配置、日志和生成结果始终与程序及环境分离；更新客户端不会重复下载环境，也不会删除模型。

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

### 环境包下载地址如何固定与修改

- 正式客户端从 `app/runtime_release.json` 读取唯一的环境版本、文件名、官方 URL 和 SHA256。四项必须对应，客户端不会接受同名但校验值不同的包。
- 以后发布新环境时，使用 `scripts/build_runtime_release.ps1` 生成新的 7z、`.sha256` 和 `.release.json`，再把确认后的发布清单随下一版客户端一起更新；不要覆盖已有 Release 资产。
- 需要切换到公司镜像或临时下载站时，只覆盖下载 URL，不覆盖文件名和 SHA256。在 `runtime/config.local.json` 合并以下配置：

```json
{
  "runtime": {
    "download_url": "https://mirror.example/v1.0.0/{package_name}"
  }
}
```

`download_url` 可以是完整 `.7z` 地址、目录地址，或使用 `{version}`、`{release_tag}`、`{package_name}` 占位符。也可临时设置环境变量 `LINGJING_RUNTIME_DOWNLOAD_URL`。无论指向 GitHub 还是镜像，下载完成后都必须通过客户端内置的固定 SHA256 校验。

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

## 开发与测试

```powershell
$env:PYTHONNOUSERSITE = "1"
& .\runtime\python\python.exe -s -m unittest discover -s tests -v
& .\runtime\python\python.exe -s -m pip check
```

主要目录：

```text
app/                 客户端、API、工作流与通道管理代码
workflows/           内置工作流定义
tests/               自动化回归测试
scripts/             环境维护与发布脚本
installer/           Windows 安装器定义
requirements-runtime.lock  独立运行环境的完整已审计快照
runtime/python/      客户端启动组件；安装完整环境后由环境包维护
runtime/ComfyUI/     独立环境包内容（不进入 Git）
.venv/Lib/           独立环境包依赖；轻量包只带 GUI 启动所需最小子集
bin/                 轻量包带 7-Zip；环境包提供 Cloudflared
models/              用户模型（不进入 Git，也不包含在任何安装包中）
```

## 安全与隐私

- 生成 API Key 和本机管理 Key 权限分离；管理接口不应交给外部调用方。
- Windows 下的本地凭据使用当前用户 DPAPI 保护，日志不会输出完整 Key。
- 远程账号服务默认只接受 HTTPS；仅回环地址允许 HTTP 开发调试。
- 发行脚本使用白名单构建，并拒绝 ComfyUI、Torch/CUDA、模型、账号会话、请求、日志和生成结果进入轻量安装包。
- 公网 URL 仍然是互联网入口，请妥善保管 API Key，不要把它提交到代码仓库或公开截图。

## 发布说明

本仓库已公开，但尚未添加开源许可证；默认不代表允许复制、修改或再分发。正式商业分发前还需完成第三方许可证清单，并使用可信代码签名证书签署安装器。没有可信签名时，Windows SmartScreen 或第三方安全软件仍可能提示未知发布者；请只通过官方发布页获取产物，并以 Release 中显示的摘要校验文件，不要关闭系统安全防护。

当前版本：`1.0.0`
