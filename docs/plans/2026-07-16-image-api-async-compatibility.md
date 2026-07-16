# Image API Async Compatibility Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the Ark-compatible image endpoint usable through a short-lived public tunnel without breaking existing synchronous callers, and make completed images safely consumable by the supplied web page.

**Architecture:** Keep `POST /api/v3/images/generations` synchronous by default for existing Ark-style callers, but add explicit asynchronous negotiation through `Prefer: respond-async` or `async: true`. Asynchronous requests return HTTP 202 plus a task URL immediately; callers poll the existing task endpoint and download the protected output with the same Bearer key as a Blob. Synchronous compatibility waits move to a worker thread so they never block FastAPI's event loop.

**Tech Stack:** Python 3.13, FastAPI/ASGI, unittest, ComfyUI background execution, browser Fetch/Blob APIs.

---

### Task 1: Lock the response and path contracts with failing tests

**Files:**
- Create: `tests/test_image_compatibility.py`
- Test: `app/server.py`

**Step 1: Add a dependency-free ASGI test helper**

Build HTTP scopes directly and capture `http.response.start` / `http.response.body`, so both the system Python and bundled runtime can run the tests without adding `httpx`.

**Step 2: Add failing contract tests**

- `Prefer: respond-async` and body `async: true` return HTTP 202 with `id`, `task_id`, `status`, `workflow_id`, `status_path`, and an empty `data` list before the fake ComfyUI task finishes.
- A default synchronous request preserves HTTP 200 and Ark `data[0].url`, while `/v1/tasks/status` remains responsive during the wait.
- A completed image task exposes both normalized `outputs` and Ark-compatible `data[0].url`, including a relative `download_path`.
- File downloads require Bearer authentication and reject sibling-prefix paths outside the allowed output roots.

**Step 3: Run the focused tests and verify they fail**

Run: `python -m unittest tests.test_image_compatibility -v`

Expected: failures for missing asynchronous negotiation, blocking synchronous wait, missing `data`/`download_path`, and unsafe prefix containment.

### Task 2: Implement the minimal server behavior

**Files:**
- Modify: `app/server.py:398-511`
- Modify: `app/server.py:887-931`
- Modify: `app/server.py:1140-1302`

**Step 1: Normalize task output paths**

Add a relative `download_path` and keep the public absolute `url`. When the first output has an image suffix, mirror it to `data: [{"url": ...}]` for Ark-style polling consumers.

**Step 2: Add asynchronous response negotiation**

Recognize `Prefer: respond-async` and JSON `async: true`. Return `JSONResponse(status_code=202)` with `Location` and `Retry-After`, a real task ID, relative/absolute status URLs, and no final image URL yet.

**Step 3: Preserve old synchronous compatibility without blocking**

Run `_wait_for_task` through `asyncio.to_thread` in both image and DeepSeek-compatible synchronous routes. Background ComfyUI polling remains on its existing worker thread.

**Step 4: Harden output containment**

Replace string-prefix directory checks with `Path.is_relative_to()` so similarly named sibling folders and reparse points cannot be treated as valid output roots.

**Step 5: Run focused tests**

Run: `python -m unittest tests.test_image_compatibility -v`

Expected: all focused tests pass.

### Task 3: Run complete regression and real public-tunnel acceptance

**Files:**
- Verify: `app/server.py`
- Verify: `tests/test_image_compatibility.py`

**Step 1: Run the full suite with both interpreters**

Run: `python -m unittest discover -s tests -v`

Run: `.venv\\Scripts\\python.exe -m unittest discover -s tests -v`

Expected: all existing and new tests pass.

**Step 2: Restart only the API backend after warning the user**

Keep the GUI and ComfyUI open; restart the backend so the new route is loaded, then re-read `runtime/session.json` without printing the key.

**Step 3: Reproduce the supplied page flow safely**

- POST the page's original `model + prompt + size` body through the current public URL with `Prefer: respond-async`.
- Confirm HTTP 202 returns quickly and `/v1/tasks/{task_id}` stays responsive.
- Poll to completion.
- Fetch the returned output URL with Bearer, validate `image/*`, PNG/JPEG magic bytes, nonzero dimensions, and visually inspect it.
- Do not enter the key into the remote HTTP page; use an in-memory test harness matching its Fetch logic.

**Step 4: Record the required page-side change**

The page must use 202 polling and authenticated Blob downloads, must not persist the key in `localStorage`, and must not put API-returned URLs into `innerHTML` without validation.

**Step 5: Commit and push the scoped change**

Run `git diff --check`, confirm no model/runtime/output/key files are staged, commit only the server, tests, and this plan, then push the current branch.
