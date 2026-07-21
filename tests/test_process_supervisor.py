import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

from app.core.process_supervisor import ProcessSupervisor, WindowsProcessJob


class ProcessSupervisorTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows hidden background process")
    def test_run_observed_hides_console_window_by_default(self):
        supervisor = ProcessSupervisor(Path.cwd())
        real_popen = subprocess.Popen
        launched = []

        def capture_popen(*args, **kwargs):
            launched.append(dict(kwargs))
            return real_popen(*args, **kwargs)

        with mock.patch(
            "app.core.process_supervisor.subprocess.Popen",
            side_effect=capture_popen,
        ):
            result = supervisor.run_observed(
                "runtime-install",
                [sys.executable, "-c", "pass"],
                timeout=5,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(launched), 1)
        self.assertTrue(launched[0]["creationflags"] & subprocess.CREATE_NO_WINDOW)

    def test_run_observed_streams_output_and_releases_process(self):
        supervisor = ProcessSupervisor(Path.cwd())
        chunks = []
        ticks = []

        result = supervisor.run_observed(
            "runtime-install",
            [
                sys.executable,
                "-c",
                (
                    "import sys,time;"
                    "sys.stdout.buffer.write(b'\\r10%\\r');sys.stdout.flush();"
                    "time.sleep(0.15);"
                    "sys.stdout.buffer.write(b'60%\\r');sys.stdout.flush()"
                ),
            ],
            timeout=5,
            tick_interval=0.02,
            on_stdout=chunks.append,
            on_tick=ticks.append,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn(b"10%", b"".join(chunks))
        self.assertIn(b"60%", b"".join(chunks))
        self.assertTrue(ticks)
        self.assertFalse(supervisor.is_running("runtime-install"))

    def test_run_observed_reports_when_timed_out_process_cannot_be_stopped(self):
        supervisor = ProcessSupervisor(Path.cwd())
        try:
            with mock.patch.object(
                supervisor,
                "_terminate_one",
                return_value="process is still busy",
            ):
                with self.assertRaisesRegex(RuntimeError, "无法安全停止"):
                    supervisor.run_observed(
                        "runtime-install",
                        [sys.executable, "-c", "import time;time.sleep(60)"],
                        timeout=0.05,
                        tick_interval=0.02,
                    )

            self.assertTrue(supervisor.is_running("runtime-install"))
        finally:
            supervisor.shutdown_all(timeout=4)

        self.assertFalse(supervisor.is_running("runtime-install"))

    def test_prepare_port_never_terminates_external_listener(self):
        supervisor = ProcessSupervisor(Path.cwd())
        with (
            mock.patch.object(supervisor, "_listener_pids", return_value=[24680]),
            mock.patch.object(supervisor, "_record_for_pid", return_value=None),
            mock.patch.object(supervisor, "_terminate_one") as terminate,
        ):
            ready, message = supervisor.prepare_port(18188)

        self.assertFalse(ready)
        self.assertIn("24680", message)
        terminate.assert_not_called()

    def test_real_external_listener_is_reported_and_left_running(self):
        external = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import socket,time;"
                    "s=socket.socket();s.bind(('127.0.0.1',0));s.listen();"
                    "print(s.getsockname()[1],flush=True);time.sleep(60)"
                ),
            ],
            cwd=str(Path.cwd()),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            port = int(external.stdout.readline().strip())
            supervisor = ProcessSupervisor(Path.cwd())

            ready, message = supervisor.prepare_port(port)

            self.assertFalse(ready)
            self.assertIn(str(external.pid), message)
            self.assertIsNone(external.poll(), "An unregistered listener must never be terminated")
        finally:
            if external.poll() is None:
                external.kill()
            external.wait(timeout=2)
            external.stdout.close()
            external.stderr.close()

    def test_shutdown_all_is_idempotent(self):
        supervisor = ProcessSupervisor(Path.cwd())
        self.assertEqual(supervisor.shutdown_all(), {})
        self.assertEqual(supervisor.shutdown_all(), {})

    def test_long_running_helper_is_stopped_by_shutdown_all(self):
        supervisor = ProcessSupervisor(Path.cwd())
        finished = threading.Event()

        def run_helper():
            try:
                supervisor.run(
                    "runtime-install",
                    [sys.executable, "-c", "import time;time.sleep(60)"],
                    timeout=60,
                )
            finally:
                finished.set()

        worker = threading.Thread(target=run_helper, daemon=True)
        worker.start()
        deadline = time.time() + 5
        while time.time() < deadline and not supervisor.is_running("runtime-install"):
            time.sleep(0.02)
        helper_pid = supervisor.pid("runtime-install")
        self.assertGreater(helper_pid, 0)

        errors = supervisor.shutdown_all(timeout=4)

        self.assertFalse(any(errors.values()))
        self.assertTrue(finished.wait(3))
        self.assertFalse(supervisor.is_running("runtime-install"))

    @unittest.skipUnless(sys.platform == "win32", "Windows suspended-launch race")
    def test_shutdown_waits_for_inflight_launch_and_cleans_it(self):
        supervisor = ProcessSupervisor(Path.cwd())
        process_created = threading.Event()
        allow_register = threading.Event()
        launched = []
        errors = []
        real_popen = subprocess.Popen

        def slow_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            launched.append(process)
            process_created.set()
            allow_register.wait(5)
            return process

        def launch_worker():
            try:
                supervisor.launch("runtime-install", [sys.executable, "-c", "import time;time.sleep(60)"])
            except Exception as exc:
                errors.append(exc)

        with mock.patch("app.core.process_supervisor.subprocess.Popen", side_effect=slow_popen):
            launcher = threading.Thread(target=launch_worker, daemon=True)
            launcher.start()
            self.assertTrue(process_created.wait(3))

            shutdown_result = []
            shutdown = threading.Thread(
                target=lambda: shutdown_result.append(supervisor.shutdown_all(timeout=4)),
                daemon=True,
            )
            shutdown.start()
            time.sleep(0.1)
            self.assertTrue(shutdown.is_alive(), "Shutdown must wait for an in-flight Popen/register")
            allow_register.set()
            launcher.join(3)
            shutdown.join(6)

        self.assertFalse(errors)
        self.assertFalse(shutdown.is_alive())
        self.assertEqual(len(launched), 1)
        self.assertIsNotNone(launched[0].poll())
        self.assertEqual(supervisor.remaining(), {})
        with self.assertRaisesRegex(RuntimeError, "正在退出"):
            supervisor.launch("late", [sys.executable, "-c", "pass"])

    @unittest.skipUnless(sys.platform == "win32", "Windows process-tree behavior")
    def test_launch_assigns_job_before_immediate_child(self):
        supervisor = ProcessSupervisor(Path.cwd())
        parent = supervisor.launch(
            "api",
            [
                sys.executable,
                "-c",
                (
                    "import subprocess,sys,time;"
                    "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                    "print(child.pid,flush=True);time.sleep(60)"
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        child_pid = 0
        child_process = None
        try:
            child_pid = int(parent.stdout.readline().strip())
            child_process = psutil.Process(child_pid)
            self.assertTrue(psutil.pid_exists(parent.pid))
            self.assertTrue(psutil.pid_exists(child_pid))

            error = supervisor.terminate("api", timeout=8)

            self.assertEqual(error, "")
            deadline = time.time() + 3
            while time.time() < deadline and (psutil.pid_exists(parent.pid) or psutil.pid_exists(child_pid)):
                time.sleep(0.1)
            self.assertFalse(psutil.pid_exists(parent.pid))
            self.assertFalse(psutil.pid_exists(child_pid))
        finally:
            supervisor.shutdown_all(timeout=2)
            if child_process is not None:
                try:
                    child_process.kill()
                except psutil.Error:
                    pass
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=2)
            for stream in (parent.stdin, parent.stdout, parent.stderr):
                if stream is not None:
                    stream.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows process ownership behavior")
    def test_failed_role_replacement_keeps_both_records_owned(self):
        first = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        second = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        supervisor = ProcessSupervisor(Path.cwd())
        try:
            supervisor.register("api", first)
            with (
                mock.patch.object(supervisor, "_terminate_record", return_value="busy"),
                mock.patch.object(supervisor, "_same_process", return_value=True),
            ):
                supervisor.register("api", second)
            self.assertEqual(len(supervisor._owned["api"]), 2)
        finally:
            supervisor.shutdown_all(timeout=4)
            for process in (first, second):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

    @unittest.skipUnless(sys.platform == "win32", "Windows Job requirement")
    def test_launch_refuses_to_run_when_job_assignment_fails(self):
        supervisor = ProcessSupervisor(Path.cwd())
        launched = []
        real_popen = subprocess.Popen

        def capture_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            launched.append(process)
            return process

        with (
            mock.patch("app.core.process_supervisor.subprocess.Popen", side_effect=capture_popen),
            mock.patch.object(WindowsProcessJob, "assign", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "无法建立后台进程组"):
                supervisor.launch(
                    "api",
                    [sys.executable, "-c", "import time;time.sleep(60)"],
                )

        self.assertEqual(len(launched), 1)
        self.assertIsNotNone(launched[0].poll())
        self.assertEqual(supervisor.remaining(), {})


if __name__ == "__main__":
    unittest.main()
