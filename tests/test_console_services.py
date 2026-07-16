import threading
import time
import unittest
from unittest import mock

from app.gui import main_gateway
from app.gui.main_gateway import GatewayApp


class ConsoleServiceTests(unittest.TestCase):
    @staticmethod
    def _app():
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app._backend_action_lock = threading.Lock()
        app._backend_action_name = ""
        app._runtime_maintenance_active = mock.Mock(return_value=False)
        app._footer_label = mock.Mock()
        app._set_light = mock.Mock()
        app._health_poll_thread = None
        app._health_poll_lock = threading.Lock()
        app._health_poll_generation = 0
        app._poll_run = False
        app._ensure_health_polling = mock.Mock()
        app._tunnel_url = ""
        app._api_key = "sk-local-test"
        app._initial_session_sync_done = False
        app._set_public_url = mock.Mock()
        app.after = lambda _delay, callback: callback()
        return app

    @staticmethod
    def _wait_until(predicate, timeout=1.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_tunnel_retry_uses_private_admin_key(self):
        app = self._app()
        app._api_key = "sk-local-console-test"
        response = mock.Mock()
        response.read.return_value = b'{"ok":true}'

        with (
            mock.patch("app.gui.main_gateway.RuntimeState") as runtime_state,
            mock.patch("urllib.request.urlopen", return_value=response) as urlopen,
        ):
            runtime_state.return_value.admin_key = "sk-admin-console-test"
            app._retry_tunnel_service()

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer sk-admin-console-test",
        )
        app._ensure_health_polling.assert_called_once()

    def test_tunnel_retry_can_reload_persisted_admin_key(self):
        app = self._app()
        app._api_key = "sk-local-stale-test"
        response = mock.Mock()
        response.read.return_value = b'{"ok":true}'

        with (
            mock.patch("app.gui.main_gateway.RuntimeState") as runtime_state,
            mock.patch("urllib.request.urlopen", return_value=response) as urlopen,
        ):
            runtime_state.return_value.admin_key = "sk-admin-persisted-test"
            app._retry_tunnel_service()

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer sk-admin-persisted-test",
        )

    def test_tunnel_retry_without_any_key_fails_without_sending_request(self):
        app = self._app()
        app._api_key = ""

        with (
            mock.patch("app.gui.main_gateway.RuntimeState") as runtime_state,
            mock.patch("urllib.request.urlopen") as urlopen,
        ):
            runtime_state.return_value.admin_key = ""
            app._retry_tunnel_service()

        urlopen.assert_not_called()
        app._ensure_health_polling.assert_not_called()
        self.assertTrue(app._backend_action_lock.acquire(blocking=False))
        app._backend_action_lock.release()

    def test_tunnel_retry_reports_server_rejection_without_polling(self):
        app = self._app()
        app._api_key = "sk-local-console-test"
        response = mock.Mock()
        response.read.return_value = b'{"ok":false,"error":"restart refused"}'

        with (
            mock.patch("app.gui.main_gateway.RuntimeState") as runtime_state,
            mock.patch("urllib.request.urlopen", return_value=response),
        ):
            runtime_state.return_value.admin_key = "sk-admin-console-test"
            app._retry_tunnel_service()

        app._ensure_health_polling.assert_not_called()
        self.assertTrue(
            any(
                "restart refused" in str(call.kwargs.get("text", ""))
                for call in app._footer_label.config.call_args_list
            )
        )

    def test_tunnel_retry_finishing_during_shutdown_does_not_restart_polling(self):
        app = self._app()
        app._api_key = "sk-local-console-test"
        request_started = threading.Event()
        allow_response = threading.Event()
        errors = []
        response = mock.Mock()
        response.read.return_value = b'{"ok":true}'

        def slow_urlopen(*_args, **_kwargs):
            request_started.set()
            allow_response.wait(1)
            return response

        def run_retry():
            try:
                app._retry_tunnel_service()
            except BaseException as exc:
                errors.append(exc)

        with (
            mock.patch("app.gui.main_gateway.RuntimeState") as runtime_state,
            mock.patch("urllib.request.urlopen", side_effect=slow_urlopen),
        ):
            runtime_state.return_value.admin_key = "sk-admin-console-test"
            worker = threading.Thread(target=run_retry, daemon=True)
            worker.start()
            self.assertTrue(request_started.wait(0.5))
            app._shutting_down = True
            allow_response.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        app._ensure_health_polling.assert_not_called()

    def test_backend_restart_does_not_block_the_ui_caller(self):
        app = self._app()
        app._startup_sequence = mock.Mock()

        def slow_shutdown():
            time.sleep(0.25)
            return ""

        app._shutdown_backend = mock.Mock(side_effect=slow_shutdown)

        started_at = time.monotonic()
        app._restart_backend()
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.1)
        self.assertTrue(self._wait_until(lambda: app._startup_sequence.called))

    def test_repeated_restart_clicks_share_one_background_action(self):
        app = self._app()
        app._startup_sequence = mock.Mock()
        shutdown_started = threading.Event()
        allow_shutdown = threading.Event()

        def slow_shutdown():
            shutdown_started.set()
            allow_shutdown.wait(1)
            return ""

        app._shutdown_backend = mock.Mock(side_effect=slow_shutdown)

        app._restart_backend()
        self.assertTrue(shutdown_started.wait(0.5))
        app._restart_backend()

        self.assertEqual(app._shutdown_backend.call_count, 1)
        allow_shutdown.set()
        self.assertTrue(self._wait_until(lambda: app._startup_sequence.called))

    def test_failed_restart_releases_action_for_a_later_retry(self):
        app = self._app()
        app._startup_sequence = mock.Mock()
        app._shutdown_backend = mock.Mock(
            side_effect=[RuntimeError("service query failed"), ""]
        )

        app._restart_backend()
        self.assertTrue(
            self._wait_until(lambda: app._shutdown_backend.call_count == 1)
        )
        self.assertTrue(
            self._wait_until(
                lambda: app._backend_action_lock.acquire(blocking=False)
            )
        )
        app._backend_action_lock.release()

        app._restart_backend()
        self.assertTrue(self._wait_until(lambda: app._startup_sequence.called))
        app._ensure_health_polling.assert_called()

    def test_initial_startup_blocks_a_concurrent_manual_restart(self):
        app = self._app()
        startup_started = threading.Event()
        allow_startup = threading.Event()
        startup_finished = threading.Event()

        def slow_startup():
            startup_started.set()
            allow_startup.wait(1)
            startup_finished.set()

        app._startup_sequence = slow_startup
        app._shutdown_backend = mock.Mock(return_value="")

        self.assertTrue(app._request_backend_start("启动后台"))
        self.assertTrue(startup_started.wait(0.5))
        app._restart_backend()

        app._shutdown_backend.assert_not_called()
        allow_startup.set()
        self.assertTrue(startup_finished.wait(0.5))

    def test_environment_recheck_does_not_interrupt_startup_probe(self):
        app = self._app()
        startup_started = threading.Event()
        allow_startup = threading.Event()

        def slow_startup():
            startup_started.set()
            allow_startup.wait(1)

        app._startup_sequence = slow_startup
        app._retry_env_check = mock.Mock()

        self.assertTrue(app._request_backend_start("启动后台"))
        self.assertTrue(startup_started.wait(0.5))
        app._start_background_runtime_recheck()

        app._retry_env_check.assert_not_called()
        allow_startup.set()

    def test_health_polling_does_not_start_while_shutting_down(self):
        app = self._app()
        app._shutting_down = True
        app._poll_run = True

        with mock.patch.object(main_gateway.threading, "Thread") as thread:
            GatewayApp._ensure_health_polling(app)

        self.assertFalse(app._poll_run)
        thread.assert_not_called()

    def test_health_poll_replacement_supersedes_an_exiting_worker(self):
        app = self._app()
        old_thread = mock.Mock()
        old_thread.is_alive.return_value = True
        app._health_poll_thread = old_thread
        app._health_poll_generation = 1
        app._poll_run = False

        with mock.patch.object(main_gateway.threading, "Thread") as thread:
            replacement = mock.Mock()
            thread.return_value = replacement
            GatewayApp._ensure_health_polling(app)

        self.assertEqual(app._health_poll_generation, 2)
        self.assertIs(app._health_poll_thread, replacement)
        replacement.start.assert_called_once()
        self.assertFalse(app._health_poll_active(1))
        self.assertTrue(app._health_poll_active(2))

    def test_api_retry_retires_live_health_poll_before_restarting(self):
        app = self._app()
        old_thread = mock.Mock()
        old_thread.is_alive.return_value = True
        app._health_poll_thread = old_thread
        app._health_poll_generation = 7
        app._poll_run = True
        api_proc = mock.Mock()
        app._api_proc = api_proc
        app._kill_proc = mock.Mock(return_value="")
        app._start_api_service = mock.Mock()
        app._ensure_health_polling = GatewayApp._ensure_health_polling.__get__(app)
        scheduled_delays = []
        app.after = lambda delay, callback: (
            scheduled_delays.append(delay),
            callback(),
        )[1]

        with mock.patch.object(main_gateway.threading, "Thread") as thread:
            replacement = mock.Mock()
            replacement.is_alive.return_value = True
            thread.return_value = replacement
            app._retry_api_service()

        app._kill_proc.assert_called_once_with(api_proc, "API")
        app._start_api_service.assert_called_once_with()
        self.assertEqual(app._health_poll_generation, 9)
        self.assertIs(app._health_poll_thread, replacement)
        replacement.start.assert_called_once_with()
        self.assertNotIn(200, scheduled_delays)
        self.assertFalse(app._finish_health_poll(7))
        self.assertTrue(app._poll_run)

    def test_queued_old_health_snapshot_cannot_overwrite_replacement(self):
        app = self._app()
        app._on_first_health = mock.Mock()
        app._on_health_update = mock.Mock()
        app._health_poll_generation = 2
        app._poll_run = True

        app._deliver_health_snapshot(1, {"status": "stale"}, first=True)
        app._deliver_health_snapshot(1, {"status": "stale"})

        app._on_first_health.assert_not_called()
        app._on_health_update.assert_not_called()

    def test_queued_old_health_failure_cannot_clear_replacement(self):
        app = self._app()
        app._on_server_unreachable = mock.Mock()
        app._health_poll_generation = 2
        app._poll_run = True

        app._deliver_health_failure(1)

        app._on_server_unreachable.assert_not_called()

    def test_repeated_health_failures_mark_api_unreachable(self):
        app = self._app()
        app._ensure_health_polling = GatewayApp._ensure_health_polling.__get__(app)
        app._on_first_health = mock.Mock()
        app._on_health_update = mock.Mock()
        app._on_server_unreachable = mock.Mock()
        response = mock.Mock()
        response.read.return_value = b'{"status":"ok"}'

        with (
            mock.patch(
                "urllib.request.urlopen",
                side_effect=[response, OSError("down"), OSError("down"), OSError("down")],
            ),
            mock.patch.object(main_gateway.time, "sleep"),
        ):
            app._poll_health()

        app._on_first_health.assert_called_once()
        app._on_server_unreachable.assert_called_once()
        self.assertFalse(app._poll_run)

    def test_offline_tunnel_clears_stale_public_url(self):
        app = self._app()
        app._tunnel_url = "https://stale.example"
        app._initial_session_sync_done = True
        app._set_public_url = mock.Mock()
        app._server_mode = "guest"
        app._server_session_token = ""
        app._api_key = ""
        app._comfy_proc = None
        app._comfy_starting_until = 0

        app._update_status(
            {
                "base_url": "https://stale.example",
                "tunnel": {"status": "offline"},
                "comfyui": {"status": "online"},
            }
        )

        self.assertEqual(app._tunnel_url, "")
        app._set_public_url.assert_called_once_with("")
        self.assertIn(
            mock.call("tunnel", "offline", "未连接"),
            app._set_light.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
