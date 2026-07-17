# Branded Client Guide Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a branded root launcher, an installed Chinese tutorial PDF, and an honest copyable GitHub environment-package dialog to the lightweight Windows client.

**Architecture:** Inno Setup creates a root shortcut that reuses the verified embedded Python launcher and app icon. A reproducible ReportLab generator produces the PDF copied into release staging. Existing online-install UI entry points are redirected to one modal manual-download guide while the verified local 7z installer remains unchanged.

**Tech Stack:** Python 3.13, Tkinter, PowerShell 7.2, Inno Setup 7, ReportLab, Poppler, `unittest`.

---

### Task 1: Lock the user-visible release contract

**Files:**
- Modify: `tests/test_release_packaging.py`
- Modify: `tests/test_dashboard_ui.py`

**Step 1:** Add assertions that `LingJing.iss` creates `{app}\灵境造片厂` with `app.ico` and that `build_release.ps1` copies `docs\灵境造片厂使用教学.pdf` to the staging root.

**Step 2:** Add a GUI test that invokes `_install_runtime_from_mirror`, finds a copyable `https://github.com/Yaro-lu/API` field, invokes “复制地址”, and confirms `_download_runtime` was not called.

**Step 3:** Update the resources-page text contract from “一键安装” to “获取环境包”.

**Step 4:** Run the focused tests and confirm they fail before implementation.

### Task 2: Add the formal branded launcher

**Files:**
- Modify: `installer/LingJing.iss`

**Step 1:** Add a root `{app}\{#MyAppName}` shortcut targeting the embedded `pythonw.exe`, with the existing script parameters, working directory, and branded icon.

**Step 2:** Keep the start-menu, optional desktop, and post-install launch entries aligned to the same target and icon.

**Step 3:** Run the packaging contract test and confirm the launcher assertion passes.

### Task 3: Create and stage the tutorial PDF

**Files:**
- Create: `scripts/build_user_guide.py`
- Create: `docs/灵境造片厂使用教学.pdf`
- Modify: `scripts/build_release.ps1`
- Modify: `README.md`

**Step 1:** Generate a polished Chinese PDF with project overview, installation, environment package workflow, model/workflow setup, API authentication, endpoint table, curl examples, async task flow, and troubleshooting.

**Step 2:** Add the PDF to required release inputs, copy it to the staging root as `灵境造片厂使用教学.pdf`, and require it in staging validation.

**Step 3:** Update README to describe manual GitHub download and the installed tutorial PDF.

**Step 4:** Extract text with `pypdf`, render every page with Poppler, and inspect the PNGs for clipping, overlap, broken Chinese glyphs, and page-number consistency.

### Task 4: Replace inactive one-click repair with download guidance

**Files:**
- Modify: `app/gui/main_gateway.py`
- Modify: `app/gui/dashboard_pages.py`
- Modify: `tests/test_dashboard_ui.py`

**Step 1:** Change `_install_runtime_from_mirror` to open a modal guide instead of starting `_download_runtime`.

**Step 2:** Show the project URL in a read-only entry with “复制地址”, “打开 GitHub”, and “选择本地环境包” actions.

**Step 3:** Change missing-environment and maintenance copy to “获取环境包” and manual-download wording.

**Step 4:** Make `_open_install_guide` prefer the installed PDF, then README, then the project homepage.

**Step 5:** Run focused GUI tests and visually inspect the real popup.

### Task 5: Build and cold-install the revised package

**Files:**
- Generated: `dist/LingJingAI-Setup-1.0.0-win-x64.exe`
- Generated: `dist/LingJingAI-Setup-1.0.0-win-x64.exe.sha256`

**Step 1:** Run Python compilation, PowerShell AST parsing, `git diff --check`, and the full `unittest` suite.

**Step 2:** Run GitNexus `detect_changes`; if the regular-clone limitation remains, record it and manually review the exact staged diff.

**Step 3:** Build the installer against the audited external bootstrap/runtime inputs.

**Step 4:** Install to a new exact directory under `C:\tmp`, verify the branded root shortcut and tutorial PDF, launch the shortcut, exercise the download dialog, and uninstall.

**Step 5:** Upload exactly the final 1.0 installer and environment 7z to the GitHub `v1.0.0` release; keep hashes and manifests as local verification records.

### Task 6: Commit and publish

**Files:**
- Commit only the intentional source, tests, plan, generator, and PDF files.

**Step 1:** Confirm no installer, staging tree, runtime archive, model, output, cache, credential, or temporary render is staged.

**Step 2:** Commit with a terse release-oriented message and push `codex/ui-navigation-20260714`.

**Step 3:** Verify the remote branch hash equals local HEAD and report the installer path, size, SHA256, tests, and commit URL.
