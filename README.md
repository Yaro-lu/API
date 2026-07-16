# 灵镜造片厂本地客户端

灵镜造片厂是一个运行在 Windows 电脑上的本地 AI API 客户端。它把 ComfyUI 工作流统一成 `URL + API Key`，让网页、业务服务或其他软件无需安装模型环境，也能调用这台电脑上的文字、图片和视频能力。

```text
调用方（网页 / 服务端 / 其他软件）
        │  URL + API Key
        ▼
灵镜造片厂客户端 ── 工作流路由 ── ComfyUI / 本地模型
        │
        └── Cloudflare Tunnel（需要公网访问时）
```

## 使用者快速开始

### 系统要求

- Windows 10 22H2 或 Windows 11，64 位
- NVIDIA RTX 显卡；本版 CUDA 13 运行环境建议使用 580 或更高版本驱动
- 模型所需的磁盘空间和显存

完整离线安装包已经包含便携 Python、Python 依赖、ComfyUI 和 Cloudflared，不需要另装 Python，也不会内置模型或用户数据。

### 第一次使用

1. 安装并启动客户端。
2. 在“模型与环境”中确认运行环境正常，并把模型放入或导入 `models/`。
3. 在“工作流”中确认需要的工作流显示“可以使用”，选择默认工作流。
4. 回到“控制台”，复制公网 URL 和 API Key。
5. 把 URL 和 Key 填入需要调用本机 AI 的网页或软件。

客户端启动后会自动管理 API、ComfyUI 和临时公网通道。关闭主窗口会结束由客户端启动的后台进程。

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
requirements-runtime.lock  离线运行环境的完整已审计快照
runtime/python/      便携 Python（不进入 Git）
runtime/ComfyUI/     ComfyUI（不进入 Git）
.venv/Lib/           Python 依赖（不进入 Git）
bin/                 Cloudflared 等运行组件（不进入 Git）
models/              用户模型（不进入 Git，也不进入安装包）
```

## 安全与隐私

- 生成 API Key 和本机管理 Key 权限分离；管理接口不应交给外部调用方。
- Windows 下的本地凭据使用当前用户 DPAPI 保护，日志不会输出完整 Key。
- 远程账号服务默认只接受 HTTPS；仅回环地址允许 HTTP 开发调试。
- 发行脚本使用白名单构建，并拒绝模型、账号会话、请求、日志和生成结果进入安装包。
- 公网 URL 仍然是互联网入口，请妥善保管 API Key，不要把它提交到代码仓库或公开截图。

## 发布说明

本仓库目前为私有开发项目。正式公开前仍需选择并添加开源许可证、完成第三方许可证清单，并使用可信代码签名证书签署安装器。没有可信签名时，Windows SmartScreen 或第三方安全软件仍可能提示未知发布者；请通过官方发布页和 SHA256 校验产物，不要关闭系统安全防护。

当前版本：`0.2.0`
