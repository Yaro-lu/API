import asyncio
import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import server


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def asgi_request(app, method, path, *, headers=None, json_body=None):
    request_headers = {"host": "client.example", **(headers or {})}
    body = b""
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("content-type", "application/json")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in request_headers.items()
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("client.example", 443),
    }
    sent = []
    request_delivered = False

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(item for item in sent if item["type"] == "http.response.start")
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start.get("headers", [])
    }
    response_body = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    return start["status"], response_headers, response_body


class FakeRegistry:
    workflow_defs = []

    def __init__(self, *args, **kwargs):
        self.workflows = list(self.workflow_defs)
        self.default_workflow_id = self.workflows[0].id

    def scan_folder(self):
        return []

    def resolve(self, workflow_id=None):
        if workflow_id in (None, ""):
            return self.workflows[0]
        for workflow in self.workflows:
            if workflow.id == workflow_id:
                return workflow
        return None


class FakeComfyUIClient:
    release = threading.Event()
    started = threading.Event()

    def __init__(self, *args, **kwargs):
        pass

    def queue_prompt(self, workflow_data):
        if any(
            node.get("class_type") == "TextOutput"
            for node in workflow_data.values()
        ):
            return "prompt-text-test"
        return "prompt-image-test"

    def get_progress(self, prompt_id, **kwargs):
        self.started.set()
        self.release.wait(timeout=5)
        return {
            "status": "completed",
            "value": 1,
            "max": 1,
            "percent": 100,
            "phase": "completed",
            "label": "completed",
        }

    def get_history(self, prompt_id):
        return {prompt_id: {"kind": "text" if "text" in prompt_id else "image"}}

    def get_output_files(self, history):
        if history.get("kind") == "text":
            return [{"type": "text", "text": "OK"}]
        return [{"filename": "generated.png", "type": "output"}]


class ImageCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        (self.base / "outputs").mkdir(parents=True)
        (self.base / "runtime" / "requests").mkdir(parents=True)
        (self.base / "runtime" / "logs").mkdir(parents=True)
        workflow_dir = self.base / "workflows" / "flux_t2i_v1"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "workflow.json").write_text(
            json.dumps(
                {
                    "1": {
                        "class_type": "SaveImage",
                        "inputs": {"filename_prefix": "test"},
                    }
                }
            ),
            encoding="utf-8",
        )
        image_workflow = SimpleNamespace(
            id="flux_t2i_v1",
            name="Flux Test",
            folder=workflow_dir,
            output_type="image",
        )
        text_workflow_dir = self.base / "workflows" / "llm_qwen3_text_gen"
        text_workflow_dir.mkdir(parents=True)
        (text_workflow_dir / "workflow.json").write_text(
            json.dumps(
                {
                    "1": {
                        "class_type": "TextOutput",
                        "inputs": {"prompt": ""},
                    }
                }
            ),
            encoding="utf-8",
        )
        text_workflow = SimpleNamespace(
            id="llm_qwen3_text_gen",
            name="Text Test",
            folder=text_workflow_dir,
            output_type="text",
        )
        FakeRegistry.workflow_defs = [image_workflow, text_workflow]
        FakeComfyUIClient.release.clear()
        FakeComfyUIClient.started.clear()
        self.fake_state = SimpleNamespace(
            api_key="sk-test-image",
            base_url="https://client.example",
            local_api="http://127.0.0.1:18188",
            set_offline=lambda: None,
        )
        self.fake_config = SimpleNamespace(
            requests_dir=self.base / "runtime" / "requests",
            logs_dir=self.base / "runtime" / "logs",
            runtime_dir=self.base / "runtime",
            comfyui_url="http://127.0.0.1:8188",
            server_port=18188,
        )
        self.patchers = [
            mock.patch.object(server, "BASE_DIR", self.base),
            mock.patch.object(server, "config", self.fake_config),
            mock.patch.object(server, "state", self.fake_state),
            mock.patch.object(server, "tunnel", None),
            mock.patch.object(server, "WorkflowRegistry", FakeRegistry),
            mock.patch.object(server, "ComfyUIClient", FakeComfyUIClient),
        ]
        for patcher in self.patchers:
            patcher.start()
        with server._task_lock:
            server.current_task.clear()
            server.task_records.clear()
        self.app = server.create_app()
        self.auth = {"authorization": "Bearer sk-test-image"}

    async def asyncTearDown(self):
        FakeComfyUIClient.release.set()
        await asyncio.sleep(0.05)
        with server._task_lock:
            server.current_task.clear()
            server.task_records.clear()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _image_body(**extra):
        return {
            "model": "doubao-seedream-5-0-260128",
            "prompt": "一枚紫色水晶立方体",
            "size": "1024x1024",
            **extra,
        }

    async def test_prefer_respond_async_returns_task_contract_immediately(self):
        release_timer = threading.Timer(0.8, FakeComfyUIClient.release.set)
        release_timer.start()
        started = time.perf_counter()
        status, headers, body = await asgi_request(
            self.app,
            "POST",
            "/api/v3/images/generations",
            headers={**self.auth, "prefer": "respond-async"},
            json_body=self._image_body(),
        )
        elapsed = time.perf_counter() - started
        payload = json.loads(body)

        self.assertEqual(status, 202)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(payload["id"], payload["task_id"])
        self.assertEqual(payload["status"], "submitted")
        self.assertEqual(payload["workflow_id"], "flux_t2i_v1")
        self.assertEqual(payload["data"], [])
        self.assertEqual(headers["location"], payload["status_path"])
        self.assertEqual(headers["retry-after"], "3")
        self.assertEqual(headers["preference-applied"], "respond-async")
        FakeComfyUIClient.release.set()
        release_timer.cancel()

    async def test_body_async_true_returns_task_contract(self):
        release_timer = threading.Timer(0.8, FakeComfyUIClient.release.set)
        release_timer.start()
        status, _headers, body = await asgi_request(
            self.app,
            "POST",
            "/api/v3/images/generations",
            headers=self.auth,
            json_body=self._image_body(**{"async": True}),
        )
        payload = json.loads(body)

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "submitted")
        self.assertTrue(payload["status_url"].endswith(payload["status_path"]))
        FakeComfyUIClient.release.set()
        release_timer.cancel()

    async def test_sync_wait_keeps_event_loop_responsive_and_ark_shape(self):
        release_timer = threading.Timer(0.35, FakeComfyUIClient.release.set)
        release_timer.start()
        started = time.perf_counter()
        post_task = asyncio.create_task(
            asgi_request(
                self.app,
                "POST",
                "/api/v3/images/generations",
                headers=self.auth,
                json_body=self._image_body(),
            )
        )
        await asyncio.sleep(0.05)
        heartbeat_elapsed = time.perf_counter() - started
        status_status, _headers, status_body = await asyncio.wait_for(
            asgi_request(
                self.app,
                "GET",
                "/v1/tasks/status",
                headers=self.auth,
            ),
            timeout=0.25,
        )
        FakeComfyUIClient.release.set()
        status, _headers, body = await asyncio.wait_for(post_task, timeout=2)
        payload = json.loads(body)

        self.assertLess(heartbeat_elapsed, 0.2)
        self.assertEqual(status_status, 200)
        self.assertIn(json.loads(status_body)["status"], {"pending", "running", "completed"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "doubao-seedream-5-0-260128")
        self.assertEqual(len(payload["data"]), 1)
        self.assertTrue(payload["data"][0]["url"].endswith("/generated.png"))
        self.assertTrue(payload["task_id"].startswith("task_"))
        release_timer.cancel()

    async def test_chat_compatibility_wait_is_nonblocking_and_shape_is_unchanged(self):
        release_timer = threading.Timer(0.35, FakeComfyUIClient.release.set)
        release_timer.start()
        started = time.perf_counter()
        post_task = asyncio.create_task(
            asgi_request(
                self.app,
                "POST",
                "/v1/chat/completions",
                headers=self.auth,
                json_body={
                    "model": "llm_qwen3_text_gen",
                    "messages": [{"role": "user", "content": "只回复 OK"}],
                },
            )
        )
        await asyncio.sleep(0.05)
        heartbeat_elapsed = time.perf_counter() - started
        status_status, _headers, _body = await asyncio.wait_for(
            asgi_request(
                self.app,
                "GET",
                "/v1/tasks/status",
                headers=self.auth,
            ),
            timeout=0.25,
        )
        FakeComfyUIClient.release.set()
        status, _headers, body = await asyncio.wait_for(post_task, timeout=2)
        payload = json.loads(body)

        self.assertLess(heartbeat_elapsed, 0.2)
        self.assertEqual(status_status, 200)
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "OK")
        self.assertNotIn("data", payload)
        release_timer.cancel()

    async def test_completed_task_has_ark_data_and_relative_download_path(self):
        record = {
            "id": "task_done",
            "task_id": "task_done",
            "workflow_id": "flux_t2i_v1",
            "status": "completed",
            "outputs": [{"filename": "result.png", "type": "output"}],
        }
        payload = server._task_api_response(record)

        self.assertEqual(payload["data"], [{"url": payload["outputs"][0]["url"]}])
        self.assertEqual(
            payload["outputs"][0]["download_path"],
            "/v1/files/task_done/result.png",
        )
        self.assertNotIn("sk-test-image", json.dumps(payload))

    async def test_text_and_unknown_outputs_are_not_reported_as_ark_images(self):
        text_payload = server._task_api_response(
            {
                "task_id": "task_text",
                "status": "completed",
                "outputs": [{"type": "text", "text": "OK"}],
            }
        )
        unknown_payload = server._task_api_response(
            {
                "task_id": "task_unknown",
                "status": "completed",
                "outputs": [{"filename": "result.bin", "type": "output"}],
            }
        )

        self.assertNotIn("data", text_payload)
        self.assertNotIn("data", unknown_payload)

    async def test_cross_origin_file_download_requires_bearer_and_returns_png(self):
        output = self.base / "outputs" / "result.png"
        output.write_bytes(PNG_BYTES)
        with server._task_lock:
            server.task_records["task_file"] = {
                "task_id": "task_file",
                "status": "completed",
                "outputs": [{"filename": output.name}],
            }
        origin = "http://100.77.118.69"

        preflight_status, preflight_headers, _ = await asgi_request(
            self.app,
            "OPTIONS",
            "/v1/files/task_file/result.png",
            headers={
                "origin": origin,
                "access-control-request-method": "GET",
                "access-control-request-headers": "authorization",
            },
        )
        unauth_status, _headers, _body = await asgi_request(
            self.app,
            "GET",
            "/v1/files/task_file/result.png",
            headers={"origin": origin},
        )
        auth_status, auth_headers, auth_body = await asgi_request(
            self.app,
            "GET",
            "/v1/files/task_file/result.png",
            headers={**self.auth, "origin": origin},
        )

        self.assertEqual(preflight_status, 200)
        self.assertEqual(preflight_headers["access-control-allow-origin"], origin)
        self.assertEqual(unauth_status, 401)
        self.assertEqual(auth_status, 200)
        self.assertEqual(auth_headers["content-type"], "image/png")
        self.assertEqual(auth_body, PNG_BYTES)

    async def test_output_path_guard_rejects_sibling_prefix(self):
        outputs = (self.base / "outputs").resolve()
        sibling = (self.base / "outputs_evil" / "secret.png").resolve()

        self.assertFalse(server._path_is_within(sibling, outputs))
        self.assertTrue(server._path_is_within(outputs / "safe.png", outputs))


if __name__ == "__main__":
    unittest.main()
