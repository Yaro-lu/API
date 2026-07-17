# Lightweight Bootstrap Installer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a small Windows installer that opens the full LingJingAI GUI on a clean Windows 10 machine while keeping ComfyUI, Torch, CUDA, Cloudflared, models, and user data in the separately distributed runtime package.

**Architecture:** The installer will carry the application, workflows, a pruned portable Python/Tk bootstrap, the small Python dependency closure required to import the GUI, and a redistributable 7-Zip executable used to install the external `.7z` runtime. The existing runtime installer remains authoritative and transactionally replaces only environment roots after hash verification. The bootstrap installer must never stage heavyweight AI runtime files or mutable user data.

**Tech Stack:** PowerShell 7.2, Inno Setup 7, portable CPython 3.13/Tk, Python `unittest`, 7-Zip, Git/GitHub.

---

### Task 1: Lock the lightweight release contract

**Files:**
- Create: `tests/test_release_packaging.py`
- Modify: `scripts/build_release.ps1`
- Modify: `installer/LingJing.iss`

**Step 1:** Add a failing static contract test asserting that the release script does not copy `runtime/ComfyUI`, `.venv/share`, Torch, or `bin/cloudflared.exe`.

**Step 2:** Assert that the script stages portable Python, an explicit bootstrap package allowlist, `7z.exe`, `7z.dll`, and the 7-Zip license.

**Step 3:** Assert that the Inno shortcut still launches `runtime/python/pythonw.exe` and that the installer metadata describes a bootstrap client rather than a complete offline AI runtime.

**Step 4:** Run `runtime/python/python.exe -s -B -m unittest tests.test_release_packaging -v` with the project root first on `sys.path`; expect the new contract to fail before implementation.

### Task 2: Produce an allowlisted bootstrap staging tree

**Files:**
- Modify: `scripts/build_release.ps1`

**Step 1:** Add optional source parameters for portable Python, bootstrap site-packages, and 7-Zip so a clean Git checkout can build against audited external runtime inputs.

**Step 2:** Extend the Python copy profile to exclude `diffusers`, `pip`, `setuptools`, IDLE, tests, caches, and bytecode while retaining the standard library, Tk, Pillow, CustomTkinter, and their metadata.

**Step 3:** Copy only the explicit dependency closure required by the GUI/API bootstrap from `.venv/Lib/site-packages`; fail if any required entry is missing.

**Step 4:** Copy `7z.exe`, `7z.dll`, and `License.txt` into `bin/`, renaming the license to `7-Zip-License.txt`.

**Step 5:** Strengthen staging policy so ComfyUI, Torch/CUDA packages, Cloudflared, models, logs, sessions, outputs, Git data, and secrets fail the build if they appear.

**Step 6:** Change `release-info.json` to declare `bootstrap-python-separate-ai-runtime` and `environment_included=false`.

**Step 7:** Run the packaging contract test again; expect PASS.

### Task 3: Update installer and user documentation

**Files:**
- Modify: `installer/LingJing.iss`
- Modify: `README.md`
- Modify: `start.bat`

**Step 1:** Update installer comments and description to state that this is the lightweight client and that the AI runtime is installed separately.

**Step 2:** Keep the per-user LocalAppData installation, direct `pythonw.exe` shortcut, no elevation, and no self-extracting/obfuscated executable scheme.

**Step 3:** Update README first-run instructions and distribution layout: small client installer, versioned runtime `.7z` plus SHA256, models and user data separate.

**Step 4:** Run documentation/static contract checks; expect PASS.

### Task 4: Build and validate real staging

**Files:**
- Generated only: `dist/staging/LingJingAI-0.2.0-win-x64/`
- Generated only: `dist/LingJingAI-0.2.0-win-x64.members.json`

**Step 1:** Run `scripts/build_release.ps1 -StageOnly` against the clean checkout, using the audited portable Python and dependency roots from the existing local runtime and the installed 7-Zip root.

**Step 2:** Verify required members exist and forbidden heavyweight/user-data paths are absent.

**Step 3:** Run `app/gui/main_gateway.py` by path with the staged Python and `-s -B`; expect `bootstrap imports ok` without using the external `.venv` or system Python.

**Step 4:** Use the staged `bin/7z.exe` to list the pinned runtime archive; expect exit code 0.

**Step 5:** Record staged file count, installed size, and compare against the previous all-in-one staging size.

### Task 5: Compile and cold-install the installer

**Files:**
- Generated: `dist/LingJingAI-Setup-0.2.0-win-x64.exe`
- Generated: `dist/LingJingAI-Setup-0.2.0-win-x64.exe.sha256`

**Step 1:** Compile with Inno Setup 7 and require the build to remain well below the 2 GiB GitHub asset ceiling.

**Step 2:** Verify installer SHA256, Authenticode status, PE metadata, and the generated member manifest.

**Step 3:** Silently install to an isolated directory under `C:\tmp`, verify exact resolved target, then import the GUI using only installed files.

**Step 4:** Launch the installed GUI, confirm the process stays alive and reports the missing heavy environment instead of crashing, then close it cleanly.

**Step 5:** Run the installer uninstaller silently and verify program files are removed while mutable runtime state may be preserved; then delete only the resolved isolated test directory without touching the source runtime, models, or outputs.

### Task 6: Full verification and GitHub publication

**Files:**
- Modify only the files listed above.

**Step 1:** Run Python compilation and the full `unittest` suite with the project checkout first on `sys.path`; expect all tests to pass.

**Step 2:** Run `git diff --check`, secret/path scans, and GitNexus `detect_changes`; review every affected flow.

**Step 3:** Confirm no runtime archive, model, output, credential, cache, staging tree, or installer binary is staged in Git.

**Step 4:** Commit the intentional source, test, documentation, and plan files with a terse release-oriented message.

**Step 5:** Push the current `codex/ui-navigation-20260714` branch to `Yaro-lu/API`. If GitHub authentication remains invalid, stop after the local commit and request re-authentication without losing the verified installer.
