[简体中文](README.md) | [English](README_EN.md)

# LingJing AI Studio

**Turn your Windows PC into a private, local AI creation station.**

LingJing AI Studio is designed for creators and everyday users who want a simpler way to run local AI. From one interface, you can prepare the runtime, install required models, choose a workflow, and generate text, images, or video on your own computer. A bundled example page lets you try it without learning ComfyUI first.

[Download the latest release](https://github.com/Yaro-lu/LingJingAI/releases/latest) · [Open the Chinese PDF guide](docs/灵境造片厂使用教学.pdf)

## What you can do

- **Generate text** for conversations, writing, and content organization.
- **Create and edit images** with text-to-image and image-to-image workflows.
- **Generate video** with supported keyframe and video workflows.
- **Maintain the runtime and models** with built-in checks, repair, download, and update actions.
- **Connect other software** through the URL and API Key shown on the dashboard.

Available capabilities depend on the workflows and models installed on your computer.

## First-time setup

1. Download and install `LingJingAI-Setup-1.0.1-win-x64.exe` from [Releases](https://github.com/Yaro-lu/LingJingAI/releases/latest).
2. Launch LingJing AI Studio, open **模型与环境 (Models & Runtime)**, and select **一键修复 (One-click Repair)**.
3. Wait while the client downloads, verifies, and installs the runtime. If the network download fails, use the manual runtime package offered by the error dialog.
4. Select **下载模型 (Download Models)** for the workflow you want to use.
5. Choose a workflow marked **可以使用 (Available)** and set it as the default.
6. Return to **控制台 (Dashboard)** and copy the URL and API Key.
7. Open **灵境造片厂示例页 (LingJing AI Studio Example)** from the desktop, enter the URL and Key, and try a text, image, or video request.

The Chinese PDF guide installed with the client contains a more detailed walkthrough.

## Before you install

- Windows 10 22H2 or Windows 11, 64-bit.
- NVIDIA RTX 20 series or newer with at least 8 GB of VRAM.
- NVIDIA driver version 580 or newer for the current CUDA 13 runtime.
- Enough disk space for the runtime, selected models, and generated files.
- An SSD is strongly recommended because first model load times are substantially faster than on a mechanical drive.

## Which file should I download?

| File | Purpose |
| --- | --- |
| `LingJingAI-Setup-1.0.1-win-x64.exe` | Required lightweight client containing the interface and launcher components |
| `runtime-nvidia-rtx20plus-cu130-v1.0.0.7z` | Separate runtime package; normally downloaded automatically, with manual installation as a fallback |
| Model files | Downloaded separately for each workflow; not bundled with the client or runtime |

The client and runtime are released separately. Updating the client does not require downloading the whole runtime again, and neither package contains models or generated user content.

## Frequently asked questions

### Why does a workflow still say that models are missing?

Large models are not preinstalled. Workflows remain visible and list the exact files they need. After adding the files, select **重新检查 (Check Again)**.

### Is an internet connection always required?

Internet access is required when downloading the runtime and models. Once they are installed, local generation can run on the same computer without an internet connection. Public tunnel access and online services still require a network connection.

### What is the example page?

It is a local, front-end-only page bundled with the client to help new users make their first request. It does not replace the client, runtime, or models. The API Key remains in the current browser session.

### Where are generated files stored?

They are stored in the `outputs` folder under the installation directory by default. Uninstalling removes the application, post-installed runtime, and models while preserving generated assets in `outputs`.

### Is my content uploaded automatically?

No. Generation runs locally by default. External devices can reach the client only when you intentionally enable the public Tunnel or share the URL and API Key.

## Download and usage notice

- Latest version: [GitHub Releases](https://github.com/Yaro-lu/LingJingAI/releases/latest)
- Current client version: `1.0.1`
- This project is publicly available as a non-commercial release, but no open-source license is currently granted.
- It may be used only for learning, testing, and evaluation. Models and third-party components remain subject to their own licenses.
- The installer is not commercially Authenticode-signed. Download it only from this repository's Releases page and verify the SHA-256 digest shown by GitHub.

---

## Technical notes

The following section is intended for developers integrating the API, building workflows, or diagnosing the local runtime.

### How it works

```text
Caller (example page / web app / backend / other software)
        │  URL + API Key
        ▼
LingJing AI Studio ── workflow router ── ComfyUI / local models
        │
        └── Cloudflare Tunnel (only when public access is needed)
```

The lightweight client bundles portable Python, Tk, and 7-Zip components required to open the full interface. ComfyUI, Torch, CUDA, and Cloudflared are distributed in the separate runtime package.

### API usage

Replace `BASE_URL` and `API_KEY` with the values shown on the client dashboard.

```bash
curl -X POST "BASE_URL/" \
  -H "Authorization: Bearer API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"A cat resting beside a sunny window"}'
```

To call a workflow with the public name `flux2`:

```bash
curl -X POST "BASE_URL/flux2" \
  -H "Authorization: Bearer API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"A cinematic city at night"}'
```

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Minimal unauthenticated liveness check |
| `GET` | `/v1/status` | Full client status |
| `GET` | `/v1/models` | List callable models |
| `GET` | `/v1/workflows` | List callable workflows |
| `POST` | `/v1/chat/completions` | OpenAI-compatible text endpoint |
| `POST` | `/api/v3/images/generations` | Jimeng / Volcano-style image endpoint |
| `POST` | `/v1/workflows/run/{workflow_id}` | Run a specific workflow |
| `GET` | `/v1/tasks/{task_id}` | Query an asynchronous task |
| `GET` | `/v1/files/{task_id}/{filename}` | Download a generated file |

Generation requests are asynchronous by default. Submit a request to receive a task ID, then query the task for progress and results.

### Workflow and model layout

- Each workflow is stored under `workflows/<workflow-name>/` and contains at least `manifest.json` and `workflow.json`.
- `manifest.json` declares the public name, output type, inputs, and model dependencies.
- Models are stored under the root `models/` directory and their names must match the workflow references.
- Prefer trusted `safetensors` or `GGUF` files. Do not import untrusted legacy PyTorch checkpoints.
- Workflows, runtime files, and models are kept separate. Updating the client does not remove models.

### Local launch and diagnostics

When the source directory already contains the complete portable runtime:

```bat
start.bat
```

Run environment diagnostics with:

```bat
check-env.bat
```

Trae or VS Code should use `runtime\python\python.exe`. The repository's `.vscode/settings.json` pins this interpreter; the portable environment uses `.venv\Lib` only for dependencies.

### Security and privacy

- Generation API Keys and local administrative keys have separate permission scopes.
- Local Windows credentials are protected with the current user's DPAPI, and full keys are not written to logs.
- Remote account services require HTTPS by default; plain HTTP is allowed only for loopback development.
- Release packaging uses an allowlist and excludes models, account sessions, requests, logs, and generated outputs.
- A public URL is still an internet entry point. Keep the API Key private and never commit it to a repository or include it in public screenshots.
