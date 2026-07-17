# Local Example Page Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build and package a local beginner-facing example page that discovers client models and calls text, image, and video APIs using a user-entered URL and Key.

**Architecture:** A self-contained HTML file contains all CSS and JavaScript and uses only the existing client HTTP API. The release script copies it into the install root, while Inno Setup creates a same-logo desktop shortcut that opens the local file in the default browser.

**Tech Stack:** HTML5, CSS, vanilla JavaScript, existing FastAPI endpoints, Python `unittest`, Inno Setup.

---

### Task 1: Define release and page contract tests

**Files:**
- Modify: `tests/test_release_packaging.py`
- Create: `tests/test_example_page.py`

**Steps:**
1. Assert the example HTML exists, is local-only, carries the correct brand, and contains the three categories and current client endpoints.
2. Assert the release script stages the page at install root.
3. Assert the installer creates a same-logo desktop shortcut.
4. Run the new tests and verify they fail before implementation.

### Task 2: Implement the self-contained example page

**Files:**
- Create: `examples/灵境造片厂示例页.html`

**Steps:**
1. Build the branded connection and three-category interface.
2. Implement URL normalization and authenticated `/v1/models` discovery.
3. Implement category-aware model selection and unavailable-model states.
4. Implement text, image, video submission, task polling, protected media loading, and actionable error messages.
5. Run page contract tests and verify they pass.

### Task 3: Add the page to release staging and desktop delivery

**Files:**
- Modify: `scripts/build_release.ps1`
- Modify: `installer/LingJing.iss`
- Modify: `README.md`

**Steps:**
1. Add the source HTML to required release inputs and copy it to the staging root.
2. Require the staged HTML in the release member contract.
3. Add a desktop shortcut using `app.ico`, and make desktop icons selected by default.
4. Document that the page is a local learning tool and still requires a running, configured client.
5. Run packaging tests and PowerShell/Inno static checks.

### Task 4: Browser and package verification

**Files:**
- Verify: `examples/灵境造片厂示例页.html`
- Verify: `dist/LingJingAI-Setup-1.0.0-win-x64.exe`

**Steps:**
1. Open the local file in a browser and visually inspect desktop and narrow layouts.
2. Mock `/v1/models` and generation responses to verify connection, automatic selection, and rendered results.
3. Run the full Python test suite and source checks.
4. Rebuild and cold-install only after the user finishes the current batch of adjustments.
5. Do not commit or push until the user explicitly ends the adjustment batch.
