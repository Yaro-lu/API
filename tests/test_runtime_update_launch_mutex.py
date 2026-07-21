"""Launch-side serialization tests for the detached runtime updater."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.core import runtime_update
from app.core.runtime_package import REQUIRED_RUNTIME_PATHS


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HELPER_SOURCE = REPOSITORY_ROOT / "app" / "core" / "runtime_update_helper.ps1"


def _prepare_handoff(container: Path, operation_id: str) -> tuple[Path, Path]:
    base = container / "客户端 中文 安装 路径"
    helper = base / runtime_update.RUNTIME_UPDATE_HELPER
    helper.parent.mkdir(parents=True)
    shutil.copyfile(HELPER_SOURCE, helper)
    staging = base / f".runtime-install-staging-{operation_id}"
    for relative in REQUIRED_RUNTIME_PATHS:
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture")
    return base, staging


@unittest.skipUnless(os.name == "nt", "Win32 named mutexes are required")
class RuntimeUpdateLaunchMutexTests(unittest.TestCase):
    def test_mutex_name_matches_powershell_algorithm_for_unicode_space_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "灵境 客户端"
            base.mkdir()
            normalized = str(base.resolve()).rstrip("\\/").upper()
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

            self.assertEqual(
                runtime_update._runtime_update_mutex_name(base, "Transaction"),
                f"Local\\LingJingAI.RuntimeUpdate.Transaction.{digest}",
            )
            self.assertEqual(
                runtime_update._runtime_update_mutex_name(base, "Launch"),
                f"Local\\LingJingAI.RuntimeUpdate.Launch.{digest}",
            )

    def test_busy_transaction_is_rejected_before_handoff_markers_are_cleared(self):
        operation_id = "8" * 32
        with tempfile.TemporaryDirectory() as tmp:
            base, staging = _prepare_handoff(Path(tmp), operation_id)
            runtime_dir = base / "runtime"
            runtime_dir.mkdir(exist_ok=True)
            started, _ = runtime_update._helper_started_paths(base, operation_id)
            commit, _ = runtime_update._helper_commit_paths(base, operation_id)
            started.write_text("first-helper-started", encoding="ascii")
            commit.write_text("first-helper-commit", encoding="ascii")

            ready = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []

            def hold_transaction_mutex() -> None:
                lease = None
                try:
                    lease = runtime_update._acquire_windows_named_mutex(
                        runtime_update._runtime_update_mutex_name(
                            base, "Transaction"
                        ),
                        busy_message="unexpected contention",
                    )
                    ready.set()
                    release.wait(10)
                except BaseException as exc:  # pragma: no cover - diagnostic path
                    errors.append(exc)
                    ready.set()
                finally:
                    if lease is not None:
                        lease.release()

            holder = threading.Thread(target=hold_transaction_mutex, daemon=True)
            holder.start()
            self.assertTrue(ready.wait(5), "transaction mutex holder did not start")
            self.assertEqual(errors, [])
            popen = mock.Mock()
            try:
                with self.assertRaisesRegex(RuntimeError, "环境更新已在后台进行"):
                    runtime_update.launch_runtime_update(
                        base,
                        staging,
                        parent_pid=os.getpid(),
                        popen_factory=popen,
                    )
            finally:
                release.set()
                holder.join(5)

            self.assertFalse(holder.is_alive())
            self.assertEqual(errors, [])
            popen.assert_not_called()
            self.assertEqual(started.read_text(encoding="ascii"), "first-helper-started")
            self.assertEqual(commit.read_text(encoding="ascii"), "first-helper-commit")


if __name__ == "__main__":
    unittest.main()
