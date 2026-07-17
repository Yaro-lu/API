import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import runtime_update
from app.core.runtime_package import REQUIRED_RUNTIME_PATHS


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HELPER_SOURCE = REPOSITORY_ROOT / "app" / "core" / "runtime_update_helper.ps1"


def _write_runtime_fixture(root: Path, payload: bytes):
    for relative in REQUIRED_RUNTIME_PATHS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _prepare_handoff(tmp: str, operation_id: str = "a" * 32):
    base = Path(tmp) / "client"
    helper = base / runtime_update.RUNTIME_UPDATE_HELPER
    helper.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HELPER_SOURCE, helper)
    staging = base / f".runtime-install-staging-{operation_id}"
    _write_runtime_fixture(staging, b"new-runtime")
    return base, staging, helper


class RuntimeUpdateLaunchTests(unittest.TestCase):
    def test_helper_source_is_ascii_for_windows_powershell_51(self):
        self.assertTrue(
            HELPER_SOURCE.read_bytes().isascii(),
            "Windows PowerShell 5.1 may parse a BOM-less script as ANSI",
        )

    def test_handoff_accepts_only_exact_direct_staging_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, helper = _prepare_handoff(tmp)

            actual = runtime_update.validate_runtime_update_handoff(base, staging)

            self.assertEqual(actual, (base.resolve(), staging.resolve(), helper.resolve(), "a" * 32))

    def test_handoff_rejects_staging_outside_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _staging, _helper = _prepare_handoff(tmp)
            outside = Path(tmp) / f".runtime-install-staging-{'b' * 32}"
            _write_runtime_fixture(outside, b"new-runtime")

            with self.assertRaisesRegex(ValueError, "直接子目录"):
                runtime_update.validate_runtime_update_handoff(base, outside)

    def test_handoff_rejects_unexpected_staging_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _staging, _helper = _prepare_handoff(tmp)
            staging = base / "runtime-staging"
            _write_runtime_fixture(staging, b"new-runtime")

            with self.assertRaisesRegex(ValueError, "名称无效"):
                runtime_update.validate_runtime_update_handoff(base, staging)

    def test_command_is_shell_free_and_pins_validated_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")

            command = runtime_update.build_runtime_update_command(
                base,
                staging,
                parent_pid=4321,
                powershell_path=powershell,
            )

            self.assertIsInstance(command, list)
            self.assertEqual(command[0], str(powershell.resolve()))
            self.assertEqual(command[command.index("-File") + 1], str(helper.resolve()))
            self.assertEqual(command[command.index("-BaseDir") + 1], str(base.resolve()))
            self.assertEqual(command[command.index("-StagingDir") + 1], str(staging.resolve()))
            self.assertEqual(command[command.index("-OperationId") + 1], "a" * 32)

    def test_launch_is_detached_and_not_registered_with_process_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")
            popen = mock.Mock(return_value=mock.sentinel.process)

            with mock.patch.object(runtime_update.os, "name", "nt"):
                result = runtime_update.launch_runtime_update(
                    base,
                    staging,
                    parent_pid=4321,
                    powershell_path=powershell,
                    popen_factory=popen,
                )

            self.assertIs(result, mock.sentinel.process)
            args, kwargs = popen.call_args
            self.assertIsInstance(args[0], list)
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["cwd"], str(base.resolve()))
            self.assertTrue(kwargs["creationflags"] & 0x00000008)
            self.assertTrue(kwargs["creationflags"] & 0x00000200)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)

    def test_result_is_consumed_once_from_fixed_runtime_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result_path = base / runtime_update.RUNTIME_UPDATE_RESULT
            result_path.parent.mkdir(parents=True)
            result_path.write_text(
                json.dumps({"schema_version": 1, "success": True, "message": "done"}),
                encoding="utf-8",
            )

            self.assertEqual(runtime_update.consume_runtime_update_result(base)["message"], "done")
            self.assertFalse(result_path.exists())
            self.assertIsNone(runtime_update.consume_runtime_update_result(base))

    def test_gui_never_replaces_its_own_runtime_in_process(self):
        source = (REPOSITORY_ROOT / "app" / "gui" / "main_gateway.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("install_staged_runtime(", source)
        self.assertIn("launch_runtime_update(", source)
        self.assertIn("_exit_for_runtime_update", source)


@unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is required")
class RuntimeUpdateHelperIntegrationTests(unittest.TestCase):
    def _run_helper(self, base: Path, staging: Path, operation_id: str):
        return subprocess.run(
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
                "2147483000",
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
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    def test_helper_swaps_only_runtime_roots_and_preserves_user_data(self):
        operation_id = "c" * 32
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp, operation_id)
            _write_runtime_fixture(base, b"old-runtime")
            (base / ".venv" / "Scripts").mkdir(parents=True)
            (base / ".venv" / "Scripts" / "python.exe").write_bytes(b"legacy")
            (base / ".venv" / "pyvenv.cfg").write_text("legacy", encoding="utf-8")
            (base / "models").mkdir()
            (base / "models" / "keep.bin").write_bytes(b"user-model")

            completed = self._run_helper(base, staging, operation_id)

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            for relative in REQUIRED_RUNTIME_PATHS:
                self.assertEqual((base / relative).read_bytes(), b"new-runtime")
            self.assertEqual((base / "models" / "keep.bin").read_bytes(), b"user-model")
            self.assertFalse((base / ".venv" / "Scripts").exists())
            self.assertFalse((base / ".venv" / "pyvenv.cfg").exists())
            self.assertFalse(staging.exists())
            self.assertFalse((base / f".runtime-install-backup-{operation_id}").exists())
            result = json.loads(
                (base / runtime_update.RUNTIME_UPDATE_RESULT).read_text(encoding="utf-8-sig")
            )
            self.assertTrue(result["success"])
            self.assertFalse(result["rolled_back"])

    def test_helper_rolls_back_completed_moves_after_later_failure(self):
        operation_id = "d" * 32
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp, operation_id)
            _write_runtime_fixture(base, b"old-runtime")
            (base / "models").mkdir()
            (base / "models" / "keep.bin").write_bytes(b"user-model")
            shutil.rmtree(base / "bin")
            (base / "bin").write_bytes(b"unmanaged-parent-collision")

            completed = self._run_helper(base, staging, operation_id)

            self.assertEqual(completed.returncode, 1, completed.stderr or completed.stdout)
            for relative in REQUIRED_RUNTIME_PATHS[:-1]:
                self.assertEqual((base / relative).read_bytes(), b"old-runtime")
            self.assertEqual((base / "bin").read_bytes(), b"unmanaged-parent-collision")
            self.assertEqual((base / "models" / "keep.bin").read_bytes(), b"user-model")
            self.assertFalse(staging.exists())
            self.assertFalse((base / f".runtime-install-backup-{operation_id}").exists())
            result = json.loads(
                (base / runtime_update.RUNTIME_UPDATE_RESULT).read_text(encoding="utf-8-sig")
            )
            self.assertFalse(result["success"])
            self.assertTrue(result["rolled_back"])


if __name__ == "__main__":
    unittest.main()
