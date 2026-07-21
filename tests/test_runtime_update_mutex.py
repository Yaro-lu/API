"""Cross-process runtime update serialization tests."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.core import runtime_update
from app.core.runtime_package import REQUIRED_RUNTIME_PATHS


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HELPER_SOURCE = REPOSITORY_ROOT / "app" / "core" / "runtime_update_helper.ps1"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _write_runtime_fixture(root: Path, payload: bytes) -> None:
    for relative in REQUIRED_RUNTIME_PATHS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _wait_for(paths: list[Path], timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(path.is_file() for path in paths):
            return
        time.sleep(0.05)
    raise AssertionError(message)


@unittest.skipUnless(
    os.name == "nt" and shutil.which("powershell.exe"),
    "Windows PowerShell 5.1 is required",
)
class RuntimeUpdateMutexIntegrationTests(unittest.TestCase):
    def _start_helper(
        self,
        base: Path,
        staging: Path,
        operation_id: str,
        parent_pid: int,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                shutil.which("powershell.exe"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(base / runtime_update.RUNTIME_UPDATE_HELPER),
                "-ParentPid",
                str(parent_pid),
                "-BaseDir",
                str(base),
                "-StagingDir",
                str(staging),
                "-OperationId",
                operation_id,
                "-WaitTimeoutSeconds",
                "30",
                "-NoRestart",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )

    def test_two_operations_cannot_enter_the_same_runtime_transaction(self):
        operations = {
            "1" * 32: b"new-runtime-one",
            "2" * 32: b"new-runtime-two",
        }
        processes: dict[str, subprocess.Popen[str]] = {}
        gate = None

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "client 中文 安装路径"
            helper = base / runtime_update.RUNTIME_UPDATE_HELPER
            helper.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HELPER_SOURCE, helper)
            _write_runtime_fixture(base, b"old-runtime")
            (base / "outputs").mkdir()
            (base / "outputs" / "keep.txt").write_text("user asset", encoding="utf-8")

            staging_by_operation: dict[str, Path] = {}
            for operation_id, payload in operations.items():
                staging = base / f".runtime-install-staging-{operation_id}"
                _write_runtime_fixture(staging, payload)
                staging_by_operation[operation_id] = staging

            try:
                gate = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
                winner_id, loser_id = operations.keys()
                winner = self._start_helper(
                    base,
                    staging_by_operation[winner_id],
                    winner_id,
                    gate.pid,
                )
                processes[winner_id] = winner
                winner_started = (
                    base / "runtime" / f"runtime-update-started-{winner_id}.json"
                )
                _wait_for(
                    [winner_started],
                    15,
                    "the first helper did not acknowledge startup",
                )
                runtime_update._write_helper_commit(base, winner_id, winner.pid)

                # The Python handoff guard must derive the exact same mutex
                # name as the PowerShell helper and reject before spawning or
                # clearing the competing operation's markers.
                blocked_popen = mock.Mock()
                with self.assertRaisesRegex(
                    RuntimeError, "环境更新已在后台进行"
                ):
                    runtime_update.launch_runtime_update(
                        base,
                        staging_by_operation[loser_id],
                        parent_pid=gate.pid,
                        popen_factory=blocked_popen,
                    )
                blocked_popen.assert_not_called()

                loser = self._start_helper(
                    base,
                    staging_by_operation[loser_id],
                    loser_id,
                    gate.pid,
                )
                processes[loser_id] = loser
                loser_stdout, loser_stderr = loser.communicate(timeout=5)
                self.assertEqual(loser.returncode, 4, loser_stderr or loser_stdout)
                self.assertFalse(
                    (
                        base
                        / "runtime"
                        / f"runtime-update-started-{loser_id}.json"
                    ).exists(),
                    "a rejected helper must not publish STARTED_ACK",
                )
                self.assertFalse(
                    (base / runtime_update.RUNTIME_UPDATE_RESULT).exists(),
                    "a rejected helper must not overwrite the fixed result file",
                )
                for relative in REQUIRED_RUNTIME_PATHS:
                    self.assertEqual((base / relative).read_bytes(), b"old-runtime")
                self.assertTrue(staging_by_operation[winner_id].is_dir())
                self.assertTrue(staging_by_operation[loser_id].is_dir())

                gate.terminate()
                gate.wait(timeout=5)
                gate = None
                winner_stdout, winner_stderr = winner.communicate(timeout=30)
                self.assertEqual(winner.returncode, 0, winner_stderr or winner_stdout)

                winner_payload = operations[winner_id]
                for relative in REQUIRED_RUNTIME_PATHS:
                    self.assertEqual((base / relative).read_bytes(), winner_payload)
                self.assertEqual(
                    (base / "outputs" / "keep.txt").read_text(encoding="utf-8"),
                    "user asset",
                )
                self.assertFalse(staging_by_operation[winner_id].exists())
                self.assertTrue(
                    staging_by_operation[loser_id].is_dir(),
                    "the rejected operation must remain retryable",
                )
                self.assertEqual(list(base.glob(".runtime-install-backup-*")), [])

                log = (base / "runtime" / "runtime-update-helper.log").read_text(
                    encoding="utf-8-sig",
                    errors="replace",
                )
                self.assertEqual(log.count("TRANSACTION_START"), 1, log)
                self.assertIn(f"operation={winner_id} MUTEX_ACQUIRED", log)
                self.assertIn(f"operation={loser_id} MUTEX_BUSY", log)
                canonical = str(base.resolve()).rstrip("\\").upper()
                root_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                mutex_name = (
                    "Local\\LingJingAI.RuntimeUpdate.Transaction." + root_hash
                )
                self.assertTrue(mutex_name.isascii())
                self.assertIn(f"name={mutex_name}", log)

                result = json.loads(
                    (base / runtime_update.RUNTIME_UPDATE_RESULT).read_text(
                        encoding="utf-8-sig"
                    )
                )
                self.assertTrue(result["success"], result)
                self.assertEqual(result["operation_id"], winner_id)
            finally:
                for process in processes.values():
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
                if gate is not None and gate.poll() is None:
                    gate.terminate()
                    gate.wait(timeout=5)

    def test_same_operation_reentry_cannot_replace_the_owner_started_marker(self):
        operation_id = "3" * 32
        owner = None
        duplicate = None
        gate = None

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "same operation 中文 path"
            helper = base / runtime_update.RUNTIME_UPDATE_HELPER
            helper.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HELPER_SOURCE, helper)
            _write_runtime_fixture(base, b"old-runtime")
            staging = base / f".runtime-install-staging-{operation_id}"
            _write_runtime_fixture(staging, b"new-runtime")
            started = (
                base / "runtime" / f"runtime-update-started-{operation_id}.json"
            )

            try:
                gate = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
                owner = self._start_helper(base, staging, operation_id, gate.pid)
                _wait_for([started], 15, "the owner helper did not acknowledge startup")
                owner_marker = started.read_bytes()
                runtime_update._write_helper_commit(base, operation_id, owner.pid)

                duplicate = self._start_helper(base, staging, operation_id, gate.pid)
                duplicate_stdout, duplicate_stderr = duplicate.communicate(timeout=5)
                self.assertEqual(
                    duplicate.returncode,
                    4,
                    duplicate_stderr or duplicate_stdout,
                )
                self.assertEqual(
                    started.read_bytes(),
                    owner_marker,
                    "a duplicate helper must not overwrite the owner's PID-bound ACK",
                )
                self.assertFalse((base / runtime_update.RUNTIME_UPDATE_RESULT).exists())
                self.assertTrue(staging.is_dir())
                for relative in REQUIRED_RUNTIME_PATHS:
                    self.assertEqual((base / relative).read_bytes(), b"old-runtime")

                gate.terminate()
                gate.wait(timeout=5)
                gate = None
                owner_stdout, owner_stderr = owner.communicate(timeout=30)
                self.assertEqual(owner.returncode, 0, owner_stderr or owner_stdout)
                for relative in REQUIRED_RUNTIME_PATHS:
                    self.assertEqual((base / relative).read_bytes(), b"new-runtime")
                self.assertFalse(staging.exists())
                log = (base / "runtime" / "runtime-update-helper.log").read_text(
                    encoding="utf-8-sig",
                    errors="replace",
                )
                self.assertEqual(log.count("TRANSACTION_START"), 1, log)
                self.assertEqual(log.count("MUTEX_BUSY"), 1, log)
            finally:
                for process in (duplicate, owner):
                    if process is not None and process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
                if gate is not None and gate.poll() is None:
                    gate.terminate()
                    gate.wait(timeout=5)
