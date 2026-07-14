# AI Worker 分步开发方案

## 设计原则

本方案将开发过程拆分为多个独立阶段，每个阶段：
- 目标明确、范围可控
- 可独立测试和验证
- 有清晰的验收标准
- 产生可交付的中间产物

---

## 阶段 1：基础框架与环境检测

**目标**：建立项目骨架，实现 GPU 和 PyTorch 检测

### 1.1 创建项目结构（已完成）
- [x] 目录结构
- [x] 配置文件模板
- [x] 基础 Python 模块
- [x] PowerShell 脚本骨架

### 1.2 实现 GPU 检测
**文件**：`app/worker_service/gpu_check.py`

**任务**：
1. 测试 `nvidia-smi` 调用
2. 验证 GPU 型号检测（RTX 20+）
3. 验证驱动版本检测（>= 572.61）
4. 验证显存检测
5. 编写单元测试

**验收标准**：
- [ ] 在支持的 GPU 上返回 `success=True`
- [ ] 在不支持的 GPU 上返回正确的错误码
- [ ] 驱动版本过低时提示升级
- [ ] 输出 `logs/preflight.json`

**测试命令**：
```powershell
# 手动测试
cd app/worker_service
python -c "from gpu_check import GPUChecker; r = GPUChecker().check(); print(r)"
```

### 1.3 实现 PyTorch 检测
**文件**：`app/worker_service/torch_check.py`

**任务**：
1. 测试调用内置 Python
2. 验证 `torch.cuda.is_available()`
3. 验证版本信息获取
4. 处理导入失败情况
5. 编写单元测试

**验收标准**：
- [ ] PyTorch+CUDA 可用时返回 `success=True`
- [ ] PyTorch 缺失时返回 `TORCH_IMPORT_FAILED`
- [ ] CUDA 不可用时返回 `CUDA_NOT_AVAILABLE`

---

## 阶段 2：ComfyUI 管理与模型检测

**目标**：实现 ComfyUI 启动/停止和模型文件检测

### 2.1 实现 ComfyUI 管理器（已完成）
**文件**：`app/worker_service/comfy_manager.py`

**任务**：
1. 实现 `is_running()` 方法 ✅
2. 实现 `start()` 方法（支持 lowvram）✅
3. 实现 `stop()` 方法 ✅
4. 实现日志捕获 ✅
5. 测试启动超时处理 ✅
6. **新增**：支持自定义模型路径（`--extra-model-paths`）✅

**验收标准**：
- [x] 能检测 ComfyUI 是否运行
- [x] 能启动 ComfyUI 并等待就绪
- [x] 能正常停止 ComfyUI
- [x] ComfyUI 日志写入 `logs/comfyui.log`
- [x] 支持自定义模型目录，无需复制文件到ComfyUI目录

**测试命令**：
```powershell
# 手动测试
python -c "
from comfy_manager import ComfyUIManager
from pathlib import Path
mgr = ComfyUIManager(Path('runtime/ComfyUI'), Path('runtime/python/python.exe'))
print('Running:', mgr.is_running())
mgr.start()
print('Started:', mgr.is_running())
mgr.stop()
"
```

### 2.2 实现模型管理器（已完成）
**文件**：`app/worker_service/model_manager.py`

**任务**：
1. 解析 `model_manifest.yaml` ✅
2. 检查模型文件是否存在 ✅
3. 支持 SHA256 校验（可选）✅
4. 支持模型目录配置 ✅
5. **新增**：模型下载管理（支持HTTP下载、暂停/恢复、进度上报）✅
6. **新增**：模型管理API端点 ✅
7. **新增**：存储使用统计 ✅

**验收标准**：
- [x] 正确列出可用/不可用模型
- [x] 找到对应的 workflow 文件
- [x] 支持自定义模型目录
- [x] 支持Flux.2和Wan 2.1模型完整配置

---

## 阶段 3：本地 API 与任务执行

**目标**：实现 FastAPI 服务和本地任务执行

### 3.1 实现 FastAPI 端点（已完成）
**文件**：`app/worker_service/main.py`

**任务**：
1. 实现 `/health` 端点 ✅
2. 实现 `/status` 端点（整合 GPU、PyTorch、ComfyUI 状态）✅
3. 实现 `/repair` 端点 ✅
4. 实现 `/tasks/local/video_flf2v` 端点（Wan 2.1 视频生成）✅
5. **新增**：实现 `/tasks/local/image_t2i` 端点（Flux.2 文生图）✅
6. **新增**：实现完整的模型管理API：
   - `GET /models` - 列出所有模型
   - `GET /models/{model_id}` - 获取模型详情
   - `GET /models/{model_id}/status` - 检查模型状态
   - `POST /models/{model_id}/download` - 下载模型
   - `DELETE /models/{model_id}` - 删除模型
   - `GET /downloads` - 列出下载任务
   - `GET /downloads/{task_id}` - 下载进度
   - `POST /downloads/{task_id}/pause/resume/cancel` - 下载控制
7. **新增**：`GET /storage` - 存储使用统计
8. 配置 CORS ✅

**验收标准**：
- [x] Swagger UI 可访问（http://127.0.0.1:8090/docs）
- [x] `/health` 返回正确 JSON
- [x] `/status` 返回完整状态
- [x] Flux.2文生图API完整可用
- [x] 完整的模型管理API可用

**启动命令**：
```powershell
cd app
.\start_worker.bat
```

### 3.2 实现 ComfyUI 客户端
**文件**：`app/worker_service/comfy_client.py`

**任务**：
1. 实现 workflow 提交
2. 实现进度等待
3. 实现输出文件获取
4. 保存提交的 workflow 到日志

**验收标准**：
- [ ] 能提交 workflow 到 ComfyUI
- [ ] 能等待并获取完成结果
- [ ] 能找到输出文件路径
- [ ] Workflow JSON 保存到 `logs/workflows/{task_id}.json`

### 3.3 实现任务执行器
**文件**：`app/worker_service/task_runner.py`

**任务**：
1. 加载 workflow JSON
2. 替换参数（prompt、尺寸、seed 等）
3. 处理首帧/末帧输入
4. 执行并返回结果
5. 错误处理和重试

**验收标准**：
- [ ] 参数正确替换到 workflow
- [ ] 输出文件复制到 `outputs/` 目录
- [ ] 视频生成任务可完整执行

---

## 阶段 4：服务器通信协议

**目标**：实现与服务器的通信（注册、心跳、拉取任务、上传结果）

### 4.1 实现服务器客户端
**文件**：`app/worker_service/server_client.py`

**任务**：
1. 实现 `register()` - 客户端注册
2. 实现 `heartbeat()` - 心跳上报
3. 实现 `get_next_task()` - 拉取任务
4. 实现 `update_progress()` - 进度上报
5. 实现 `upload_result()` - 结果上传
6. 实现 `report_error()` - 错误上报
7. Token 管理

**验收标准**：
- [ ] 所有 API 调用正确处理 HTTP 错误
- [ ] 超时和重试机制
- [ ] Token 正确添加到请求头

### 4.2 实现任务轮询循环
**新建文件**：`app/worker_service/task_poller.py`

**任务**：
1. 实现后台线程拉取任务
2. 实现任务状态机（queued -> running -> completed/failed）
3. 实现进度上报
4. 实现结果上传
5. 实现错误处理和上报

**验收标准**：
- [ ] 能持续轮询服务器获取任务
- [ ] 任务执行期间正确上报进度
- [ ] 任务完成后自动上传结果
- [ ] 任务失败时上报错误

---

## 阶段 5：一键修复与维护功能

**目标**：实现环境修复和维护工具

### 5.1 实现修复管理器
**文件**：`app/worker_service/repair.py`

**任务**：
1. 实现快速修复（恢复 Python 依赖）
2. 实现完整修复（重建 runtime）
3. 日志备份
4. 不删除 models、outputs、config

**验收标准**：
- [ ] 快速修复能修复常见依赖问题
- [ ] 完整修复能重置 runtime 目录
- [ ] 用户数据目录（models、outputs、config）不受影响

### 5.2 完善 PowerShell 脚本
**文件**：`scripts/*.ps1`

**任务**：
1. `build_runtime.ps1` - 自动化下载和设置 runtime
2. `preflight_check.ps1` - 完整预检
3. `repair_runtime.ps1` - 修复脚本
4. `build_portable.ps1` - 打包绿色版
5. `build_installer.ps1` - 打包安装版

---

## 阶段 6：UI 与交互（可选 MVP）

**目标**：实现系统托盘图标和本地网页控制台

### 6.1 实现系统托盘
**新建文件**：`app/tray_app.py`（使用 PySide6）

**任务**：
1. 显示托盘图标
2. 右键菜单（启动/停止、设置、退出）
3. 气泡通知

### 6.2 实现本地 Web 控制台
**目录**：`app/ui/`

**任务**：
1. 简单 HTML/JS 界面
2. 显示客户端状态
3. 服务器登录界面
4. 模型目录选择
5. 日志查看
6. 一键修复按钮

---

## 阶段 7：打包与分发

**目标**：生成绿色压缩包和安装程序

### 7.1 准备 Runtime（进行中）
**任务**：
1. 下载 Python embeddable（3.10+）- 已准备框架
2. 安装 PyTorch with CUDA - 待配置
3. 克隆 ComfyUI 并安装依赖 - 已准备框架
4. 安装 custom nodes - 待配置
5. 下载 FFmpeg - 已准备框架
6. 测试完整 runtime - 待测试

### 7.2 构建绿色版（已完成框架）
**启动脚本**：
- `start.bat` - 一键启动（已创建）✅
- `check-env.bat` - 环境检查（已完善）✅
- `app/start_worker.bat` - 原有脚本

**目录结构**：
```
ai-story-studio-agent/
├── start.bat          ⭐ 一键启动
├── check-env.bat      ⭐ 环境检查
├── app/
├── config/
├── models/            ⭐ 统一模型目录（无需复制到ComfyUI）
├── workflows/         ⭐ Flux.2 + Wan 2.1 工作流
├── runtime/
│   ├── ComfyUI/
│   ├── python/
│   └── ffmpeg/
├── outputs/
├── logs/
└── inputs/
```

**验收标准**：
- [x] 完整的项目目录框架
- [x] 一键启动脚本（start.bat）
- [x] ComfyUI自动使用项目models目录（无需复制）
- [ ] 完整runtime配置后，解压后可直接运行
- [ ] 不依赖系统 Python
- [ ] 不依赖系统 CUDA Toolkit

### 7.3 构建安装版
**使用**：`installer/AIWorker.iss` + Inno Setup

**验收标准**：
- [ ] 安装程序正常运行
- [ ] 创建桌面/开始菜单快捷方式
- [ ] 卸载程序正常
- [ ] 卸载时默认保留用户数据

---

## 阶段 8：测试与验收

### 8.1 端到端测试
**测试场景**：
1. 新机器解压绿色版
2. 启动客户端
3. 检测 GPU 环境
4. 登录服务器
5. 服务器下发 video_flf2v 任务
6. 客户端自动执行
7. 结果上传成功

### 8.2 错误场景测试
- [ ] GPU 不支持时的提示
- [ ] 驱动版本过低时的提示
- [ ] 模型缺失时的提示
- [ ] ComfyUI 启动失败时的提示
- [ ] 显存不足时的处理
- [ ] 网络断开时的重连

---

## 上下文管理建议

为避免大模型上下文限制，建议按以下方式使用本方案：

1. **每次只处理一个阶段**：专注于当前阶段，完成后再进入下一阶段
2. **使用本文件作为参考**：每次对话开始时重新提供本文件的相关部分
3. **保持每次对话范围小**：每次只实现 1-2 个模块或函数
4. **及时保存和测试**：完成一个小功能后立即测试，确保正确后再继续
5. **生成中间总结**：每个阶段完成后，生成该阶段的总结文档

---

## 依赖清单

### Python 依赖
```
fastapi>=0.104.0
uvicorn>=0.24.0
requests>=2.31.0
pydantic>=2.5.0
pyyaml>=6.0.0
psutil>=5.9.0
```

### 运行时依赖
- Python 3.10+ embeddable
- PyTorch with CUDA 12.x
- ComfyUI (特定 commit)
- FFmpeg
- Custom nodes (Wan2.1 相关)

### 构建工具
- 7-Zip
- Inno Setup 6

---

## 更新日志

### 2026-07-01 - v1.0 桌面客户端 + 服务器通信 + 任务系统完成

**架构**：
```
┌─────────────┐    HTTP API    ┌──────────────┐   轮询/HTTP    ┌────────────┐
│  GUI 桌面端  │ ←──────────→ │  FastAPI 服务 │ ←───────────→ │ 远端服务器  │
│ (tkinter)    │  localhost:8090│  (Worker API) │               │ (Linux)    │
└─────────────┘               └──────┬───────┘               └────────────┘
                                     │
                                     ├── ComfyUI Manager (启动/停止)
                                     ├── Model Manager (下载/校验/状态)
                                     ├── Task Runner (任务队列 + 状态机)
                                     ├── Task Poller (轮询服务器拉取任务)
                                     └── Bridge (外接 ComfyUI 接口)
```

**完成的功能模块**：

1. **原生桌面 GUI** (`app/gui_app.py`, ~766 行)
   - tkinter 原生界面，3 个标签页：状态、任务队列、桥接测试
   - 登录弹窗 → 自动连接远端服务器 → 启动 TaskPoller
   - 任务表格实时刷新（3秒间隔），支持取消/下载输出
   - 系统托盘（pystray），最小化到托盘
   - 自动启动/管理 API 后台进程
   - ComfyUI 启停控制

2. **任务系统** (`task_runner.py`)
   - TaskStatus 状态机：PENDING → RUNNING → COMPLETED/FAILED/CANCELLED
   - 后台 worker 线程自动消费队列
   - 支持 image_t2i（Flux.2）和 video_flf2v（Wan 2.1）两种任务类型
   - 自动加载 workflow JSON、替换参数、提交 ComfyUI、等待结果

3. **服务器通信** (`server_client.py` + `task_poller.py`)
   - Worker 注册：POST `/api/worker/register`（上报 GPU/PyTorch/模型清单）
   - 心跳保活：POST `/api/worker/heartbeat`
   - 任务拉取：GET `/api/worker/tasks/next`（每 5 秒轮询）
   - 进度上报：POST `/api/worker/tasks/{id}/progress`
   - 结果上传：POST `/api/worker/tasks/{id}/result`
   - 错误上报：POST `/api/worker/tasks/{id}/error`
   - 轮询器状态机：IDLE → RUNNING → PAUSED → STOPPED

4. **ComfyUI 桥接** (`main.py` → `POST /bridge/comfyui/image-t2i`)
   - 接收外部 ComfyUI 地址 + 参数
   - 自动加载本地 workflow JSON 并替换参数
   - 提交到外部 ComfyUI → 轮询等待 → 返回结果 URL

5. **完整 API 端点** (`main.py`)
   - 健康检查：`GET /health`、`GET /status`
   - 任务管理：`GET /tasks`、`GET /tasks/{id}`、`POST /tasks/{id}/cancel`、`GET /tasks/{id}/output`
   - 任务提交：`POST /tasks/local/image-t2i`、`POST /tasks/local/video-flf2v`
   - 模型管理：完整的 CRUD + 下载 + 存储统计
   - 服务端连接：`POST /server/connect`、`POST /server/disconnect`、`GET /server/status`

6. **便携包基础**
   - `start.bat` → 一键启动 GUI
   - `start_gui.bat` → 开发模式启动
   - 嵌入 Python（`runtime/python_embeded/`）
   - ComfyUI（`runtime/ComfyUI/`）
   - 自动设置 PYTHONDONTWRITEBYTECODE=1（Z 盘只读兼容）

**当前项目结构**：
```
ai-story-studio-agent/
├── start.bat                 # 一键启动 GUI
├── start_gui.bat             # 开发模式启动
├── check-env.bat             # 环境检查
├── requirements.txt          # Python 依赖
├── VERSION                   # 版本号
├── DEVELOPMENT_PLAN.md       # 本文件
├── app/
│   ├── gui_app.py            # 桌面 GUI 入口
│   ├── start_worker.bat      # API 服务启动脚本
│   └── worker_service/
│       ├── main.py           # FastAPI 服务（全部端点）
│       ├── schemas.py        # Pydantic 数据模型
│       ├── config.py         # 配置加载器
│       ├── task_runner.py    # 任务执行器 + 状态机
│       ├── task_poller.py    # 服务器任务轮询器
│       ├── server_client.py  # 服务器 HTTP 客户端
│       ├── comfy_client.py   # ComfyUI HTTP 客户端
│       ├── comfy_manager.py  # ComfyUI 进程管理
│       ├── model_manager.py  # 模型管理 + 下载
│       ├── gpu_check.py      # GPU 检测
│       ├── torch_check.py    # PyTorch 检测
│       ├── repair.py         # 环境修复
│       └── log_manager.py    # 日志管理
├── config/
│   ├── client.yaml           # 客户端配置
│   └── model_manifest.yaml   # 模型清单
├── workflows/                # ComfyUI workflow JSON
├── models/                   # 模型文件目录
├── runtime/
│   ├── ComfyUI/              # ComfyUI 源码
│   ├── python_embeded/       # 嵌入式 Python
│   └── ffmpeg/               # FFmpeg（待配置）
├── outputs/                  # 生成结果
├── logs/                     # 日志
└── inputs/                   # 输入文件
```

**当前状态**：
- GUI 可正常启动（通过本地 venv 调试）
- API 服务端口 8090，Swagger UI 可访问
- 模型文件已就绪（Flux.2 + Wan 2.1）
- TaskPoller 流程完整（连接 → 轮询 → 执行 → 上传）
- 便携包框架完成，需将项目复制到本地可写目录运行

**服务端需实现的接口**（供同事参考）：
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/worker/register` | POST | 接收 Worker 注册（GPU/模型信息） |
| `/api/worker/heartbeat` | POST | 心跳保活 |
| `/api/worker/tasks/next` | GET | 下发待执行任务 |
| `/api/worker/tasks/{id}/progress` | POST | 接收进度更新 |
| `/api/worker/tasks/{id}/result` | POST | 接收结果文件上传 |
| `/api/worker/tasks/{id}/error` | POST | 接收错误上报 |
