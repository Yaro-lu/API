"""Transactional ComfyUI updater worker tests."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.core import comfyui_update_worker as worker


OPERATION_ID = "a" * 32
TEMP_ROOT = Path("E:/CodexTemp")
RELEASE_METADATA = {
    "repository_url": "https://github.com/Comfy-Org/ComfyUI",
    "tag_name": "v0.4.0",
    "version": "0.4.0",
    "release_id": 123,
    "archive_sha256": "b" * 64,
}


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _prepare_client(tmp: str, *, with_overlay: bool = True):
    base = Path(tmp) / "client"
    live = base / "runtime" / "ComfyUI"
    staging = base / f".comfyui-update-staging-{OPERATION_ID}"
    overlay = base / f".comfyui-update-overlay-{OPERATION_ID}"
    _write(live / "main.py", "old-core")
    _write(live / "old_only.py", "old-only")
    _write(staging / "main.py", "new-core")
    _write(staging / "new_only.py", "new-only")
    _write(staging / "input" / "from-release.txt", "discard-me")
    _write(live / "models" / "keep.bin", "model")
    _write(live / "custom_nodes" / "keep.py", "node")
    _write(live / "user" / "settings.json", "settings")
    _write(live / "extra_model_paths.yaml", "paths")
    site_packages = base / ".venv" / "Lib" / "site-packages"
    _write(site_packages / "comfy_kitchen" / "version.py", "old-dependency")
    _write(
        site_packages / "comfy_kitchen-1.0.0.dist-info" / "METADATA",
        "old-metadata",
    )
    if with_overlay:
        _write(overlay / "comfy_kitchen" / "version.py", "new-dependency")
        _write(
            overlay / "comfy_kitchen-2.0.0.dist-info" / "METADATA",
            "new-metadata",
        )
    return base, live, staging, overlay if with_overlay else None


def _write_manifest(
    base: Path,
    staging: Path,
    overlay: Path | None,
    *,
    cleanup_warnings: list[str] | None = None,
) -> Path:
    manifest = base / f".comfyui-update-manifest-{OPERATION_ID}.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "base_dir": str(base),
                "staging_core": str(staging),
                "dependency_overlay": str(overlay or ""),
                "release_metadata": RELEASE_METADATA,
                "dependency_plan": {},
                "cleanup_warnings": cleanup_warnings or [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest


def _prepare_worker_launch_files(base: Path) -> Path:
    pythonw = base / "runtime" / "python" / "pythonw.exe"
    _write(pythonw, "portable-python")
    _write(base / "app" / "core" / "comfyui_update_worker.py", "# bundled worker")
    _write(base / "app" / "gui" / "main_gateway.py", "# bundled gui")
    return pythonw


def _interrupt_update_after_move(
    base: Path,
    staging: Path,
    overlay: Path | None,
    should_interrupt,
) -> None:
    original_rename = worker._rename_path

    def rename_then_interrupt(source: Path, destination: Path):
        original_rename(source, destination)
        if should_interrupt(source, destination):
            raise KeyboardInterrupt("simulated power loss")

    with mock.patch.object(worker, "_rename_path", rename_then_interrupt):
        try:
            worker.apply_prepared_update(
                base,
                staging,
                overlay,
                RELEASE_METADATA,
                probe_runner=lambda _candidate: True,
            )
        except KeyboardInterrupt:
            return
    raise AssertionError("the simulated interruption point was not reached")


class ApplyPreparedUpdateTests(unittest.TestCase):
    def test_contract_lists_every_permanently_protected_comfyui_path(self):
        self.assertEqual(
            {path.as_posix() for path in worker.PROTECTED_COMFYUI_PATHS},
            {
                "models",
                "custom_nodes",
                "user",
                "input",
                "inputs",
                "output",
                "outputs",
                "temp",
                "logs",
                "tasks",
                "cache",
                "extra_model_paths.yaml",
            },
        )

    def test_success_atomically_installs_core_dependencies_and_preserves_user_data(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)

            result = worker.apply_prepared_update(
                base,
                staging,
                overlay,
                RELEASE_METADATA,
                probe_runner=lambda candidate: (candidate / "main.py").read_text(
                    encoding="utf-8"
                )
                == "new-core",
            )

            self.assertEqual(result["status"], "installed")
            self.assertTrue(result["success"])
            self.assertFalse(result["rolled_back"])
            self.assertEqual((live / "main.py").read_text(encoding="utf-8"), "new-core")
            self.assertFalse((live / "old_only.py").exists())
            self.assertEqual((live / "models" / "keep.bin").read_text(), "model")
            self.assertEqual((live / "custom_nodes" / "keep.py").read_text(), "node")
            self.assertEqual((live / "user" / "settings.json").read_text(), "settings")
            self.assertEqual((live / "extra_model_paths.yaml").read_text(), "paths")
            self.assertFalse((live / "input" / "from-release.txt").exists())
            self.assertEqual(
                (base / ".venv" / "Lib" / "site-packages" / "comfy_kitchen" / "version.py").read_text(),
                "new-dependency",
            )
            site_packages = base / ".venv" / "Lib" / "site-packages"
            self.assertFalse((site_packages / "comfy_kitchen-1.0.0.dist-info").exists())
            self.assertTrue((site_packages / "comfy_kitchen-2.0.0.dist-info").is_dir())
            version = json.loads(
                (live / ".lingjing-comfyui-version.json").read_text(encoding="utf-8")
            )
            self.assertEqual(version["version"], "0.4.0")
            self.assertFalse(staging.exists())
            self.assertFalse(overlay.exists())

    def test_build_command_is_shell_free_and_pins_fixed_result_and_restart_paths(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            pythonw = _prepare_worker_launch_files(base)
            manifest = _write_manifest(base, staging, overlay)
            result_path = base / worker.COMFYUI_UPDATE_RESULT

            command = worker.build_worker_command(
                manifest,
                result_path,
                pythonw,
                parent_pid=4321,
            )

            self.assertIsInstance(command, list)
            self.assertEqual(command[0], str(pythonw.resolve()))
            self.assertEqual(command[command.index("--manifest") + 1], str(manifest.resolve()))
            self.assertEqual(command[command.index("--result") + 1], str(result_path))
            self.assertEqual(command[command.index("--restart-client") + 1], str(pythonw.resolve()))
            self.assertEqual(command[command.index("--parent-pid") + 1], "4321")
            self.assertEqual(command[command.index("--wait-timeout-seconds") + 1], "60")
            self.assertNotIn("-m", command)
            self.assertEqual(
                command[3],
                str((base / "app" / "core" / "comfyui_update_worker.py").resolve()),
            )

            with self.assertRaisesRegex(ValueError, "固定路径"):
                worker.build_worker_command(
                    manifest,
                    base / "outside-result.json",
                    pythonw,
                    parent_pid=4321,
                )

    def test_launch_worker_is_detached_and_receives_a_controlled_environment(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            pythonw = _prepare_worker_launch_files(base)
            manifest = _write_manifest(base, staging, overlay)
            popen = mock.Mock(return_value=mock.sentinel.process)

            process = worker.launch_worker(
                manifest,
                base / worker.COMFYUI_UPDATE_RESULT,
                pythonw,
                parent_pid=4321,
                popen_factory=popen,
            )

            self.assertIs(process, mock.sentinel.process)
            args, kwargs = popen.call_args
            self.assertIsInstance(args[0], list)
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["cwd"], str(base.resolve()))
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            self.assertEqual(kwargs["env"]["PYTHONNOUSERSITE"], "1")
            self.assertTrue(kwargs["creationflags"] & 0x00000008)
            self.assertTrue(kwargs["creationflags"] & 0x00000200)

    def test_parent_exit_is_observed_before_the_update_is_applied(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            manifest = _write_manifest(base, staging, overlay)
            order: list[str] = []

            def wait_for_parent(pid: int, timeout: int) -> bool:
                self.assertEqual((pid, timeout), (4321, 60))
                order.append("wait")
                return True

            def apply_after_wait(*_args, **_kwargs):
                order.append("apply")
                return {
                    "schema_version": 1,
                    "status": "installed",
                    "success": True,
                    "rolled_back": False,
                    "message": "done",
                }

            with mock.patch.object(worker, "apply_prepared_update", apply_after_wait):
                result = worker.run_prepared_manifest(
                    manifest,
                    parent_pid=4321,
                    parent_waiter=wait_for_parent,
                )

            self.assertEqual(result["status"], "installed")
            self.assertEqual(order, ["wait", "apply"])

    def test_restart_is_launched_only_after_the_updater_lock_is_released(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            manifest = _write_manifest(base, staging, overlay)
            restart_client = _prepare_worker_launch_files(base)
            lock_was_available: list[bool] = []

            def launch_after_unlock(*_args, **_kwargs):
                competing = worker._UpdaterFileLock(base)
                acquired = competing.acquire(blocking=False)
                lock_was_available.append(acquired)
                if acquired:
                    competing.release()
                return mock.sentinel.process

            result = worker.run_prepared_manifest(
                manifest,
                restart_client=restart_client,
                probe_runner=lambda _candidate: True,
                popen_factory=launch_after_unlock,
            )

            self.assertEqual(result["status"], "installed")
            self.assertEqual(lock_was_available, [True])

    def test_parent_exit_timeout_writes_failure_without_calling_apply(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            manifest = _write_manifest(
                base,
                staging,
                overlay,
                cleanup_warnings=["准备阶段旧暂存目录未能删除"],
            )
            restart_client = _prepare_worker_launch_files(base)

            with (
                mock.patch.object(worker, "apply_prepared_update") as apply,
                mock.patch.object(worker, "_launch_restart") as restart,
            ):
                result = worker.run_prepared_manifest(
                    manifest,
                    restart_client=restart_client,
                    parent_pid=4321,
                    wait_timeout_seconds=5,
                    parent_waiter=lambda _pid, _timeout: False,
                )

            apply.assert_not_called()
            restart.assert_not_called()
            self.assertEqual(result["status"], "failed_rolled_back")
            self.assertIn("超时", result["error"])
            self.assertEqual((live / "main.py").read_text(), "old-core")
            self.assertFalse(staging.exists())
            self.assertFalse(overlay.exists())
            self.assertFalse(manifest.exists())
            persisted = json.loads(
                (base / worker.COMFYUI_UPDATE_RESULT).read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["status"], "failed_rolled_back")
            self.assertEqual(
                persisted["cleanup_warnings"],
                ["准备阶段旧暂存目录未能删除"],
            )

    @unittest.skipUnless(os.name == "nt", "Windows process handles are required")
    def test_windows_parent_wait_really_blocks_until_timeout(self):
        started = time.monotonic()

        exited = worker._wait_for_parent_exit(os.getpid(), 1)

        self.assertFalse(exited)
        self.assertGreaterEqual(time.monotonic() - started, 0.8)

    def test_recovery_restores_live_after_power_loss_immediately_after_backup(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            _interrupt_update_after_move(
                base,
                staging,
                overlay,
                lambda source, destination: source == live
                and destination.name.startswith(".comfyui-update-backup-"),
            )
            self.assertFalse(live.exists())

            result = worker.recover_interrupted_update(base)

            self.assertEqual(result["status"], "recovered")
            self.assertEqual((live / "main.py").read_text(), "old-core")
            self.assertFalse(staging.exists())
            self.assertFalse(overlay.exists())
            self.assertFalse(list(base.glob(".comfyui-update-backup-*")))
            self.assertFalse(list(base.glob(".comfyui-update-journal-*")))
            again = worker.recover_interrupted_update(base)
            self.assertEqual(again["status"], "no_recovery_needed")

    def test_recovery_lock_busy_does_not_touch_the_interrupted_transaction(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            _interrupt_update_after_move(
                base,
                staging,
                overlay,
                lambda source, destination: source == live
                and destination.name.startswith(".comfyui-update-backup-"),
            )
            journal = next(base.glob(".comfyui-update-journal-*"))
            journal_before = journal.read_bytes()
            backup = next(base.glob(".comfyui-update-backup-*"))

            with mock.patch.object(
                worker._UpdaterFileLock,
                "acquire",
                return_value=False,
            ):
                result = worker.recover_interrupted_update(base)

            self.assertEqual(result["status"], "recovery_in_progress")
            self.assertTrue(result["in_progress"])
            self.assertFalse(live.exists())
            self.assertTrue(backup.is_dir())
            self.assertEqual(journal.read_bytes(), journal_before)

    def test_recovery_restores_old_core_after_staging_was_activated(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            _interrupt_update_after_move(
                base,
                staging,
                overlay,
                lambda source, destination: source == staging and destination == live,
            )
            self.assertEqual((live / "main.py").read_text(), "new-core")
            self.assertTrue(list(base.glob(".comfyui-update-backup-*")))

            result = worker.recover_interrupted_update(base)

            self.assertEqual(result["status"], "recovered")
            self.assertEqual((live / "main.py").read_text(), "old-core")
            self.assertEqual((live / "models" / "keep.bin").read_text(), "model")
            self.assertFalse(staging.exists())
            self.assertFalse(overlay.exists())

    def test_recovery_reverses_an_interrupted_dependency_overlay(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            site_packages = base / ".venv" / "Lib" / "site-packages"
            _interrupt_update_after_move(
                base,
                staging,
                overlay,
                lambda source, _destination: source.parent == overlay
                and source.name == "comfy_kitchen",
            )
            self.assertEqual(
                (site_packages / "comfy_kitchen" / "version.py").read_text(),
                "new-dependency",
            )

            result = worker.recover_interrupted_update(base)

            self.assertEqual(result["status"], "recovered")
            self.assertEqual((live / "main.py").read_text(), "old-core")
            self.assertEqual(
                (site_packages / "comfy_kitchen" / "version.py").read_text(),
                "old-dependency",
            )
            self.assertTrue((site_packages / "comfy_kitchen-1.0.0.dist-info").is_dir())
            self.assertFalse((site_packages / "comfy_kitchen-2.0.0.dist-info").exists())
            self.assertFalse(staging.exists())
            self.assertFalse(overlay.exists())

    def test_recovery_rejects_a_journal_path_escape_without_mutation(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, _overlay = _prepare_client(tmp, with_overlay=False)
            journal = base / f".comfyui-update-journal-{OPERATION_ID}.json"
            journal.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": OPERATION_ID,
                        "phase": "backing_up_core",
                        "actions": [
                            {
                                "kind": "move_live_to_backup",
                                "state": "planned",
                                "source": "runtime/ComfyUI",
                                "destination": "../outside",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = worker.recover_interrupted_update(base)

            self.assertEqual(result["status"], "recovery_incomplete")
            self.assertEqual((live / "main.py").read_text(), "old-core")
            self.assertTrue(staging.is_dir())
            self.assertTrue(journal.is_file())

    def test_incomplete_recovery_can_retry_idempotently_on_next_start(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            _interrupt_update_after_move(
                base,
                staging,
                overlay,
                lambda source, destination: source == staging and destination == live,
            )
            original_rename = worker._rename_path

            def fail_first_core_restore(source: Path, destination: Path):
                if source == live and destination == staging:
                    raise OSError("simulated recovery interruption")
                return original_rename(source, destination)

            with mock.patch.object(worker, "_rename_path", fail_first_core_restore):
                first = worker.recover_interrupted_update(base)

            self.assertEqual(first["status"], "recovery_incomplete")
            journal = next(base.glob(".comfyui-update-journal-*.json"))
            payload = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "recovery_incomplete")

            second = worker.recover_interrupted_update(base)

            self.assertEqual(second["status"], "recovered")
            self.assertEqual((live / "main.py").read_text(), "old-core")
            self.assertFalse(journal.exists())

    def test_manifest_runner_writes_and_consumer_removes_the_fixed_result_once(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            manifest = _write_manifest(
                base,
                staging,
                overlay,
                cleanup_warnings=[
                    "准备阶段旧暂存目录未能删除",
                    "  准备阶段旧暂存目录未能删除  ",
                ],
            )

            result = worker.run_prepared_manifest(
                manifest,
                probe_runner=lambda _candidate: True,
            )

            self.assertEqual(result["status"], "installed")
            result_path = base / worker.COMFYUI_UPDATE_RESULT
            self.assertTrue(result_path.is_file())
            self.assertFalse(manifest.exists())
            consumed = worker.consume_update_result(base)
            self.assertEqual(consumed["status"], "installed")
            self.assertEqual(
                consumed["cleanup_warnings"],
                ["准备阶段旧暂存目录未能删除"],
            )
            self.assertFalse(result_path.exists())
            self.assertIsNone(worker.consume_update_result(base))

    def test_manifest_runner_rejects_invalid_cleanup_warnings_before_changes(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            manifest = _write_manifest(base, staging, overlay)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["cleanup_warnings"] = "not-a-list"
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cleanup_warnings.*列表"):
                worker.run_prepared_manifest(
                    manifest,
                    probe_runner=lambda _candidate: True,
                )

            self.assertEqual((live / "main.py").read_text(), "old-core")
            self.assertTrue(staging.is_dir())
            self.assertTrue(overlay.is_dir())
            self.assertTrue(manifest.is_file())

    def test_manifest_runner_rejects_a_non_ready_handoff_without_changes(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            manifest = _write_manifest(base, staging, overlay)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["status"] = "full_environment_required"
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "尚未就绪"):
                worker.run_prepared_manifest(
                    manifest,
                    probe_runner=lambda _candidate: True,
                )

            self.assertEqual((live / "main.py").read_text(), "old-core")

    def test_manifest_runner_never_deletes_an_arbitrary_manifest_path(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            controlled = _write_manifest(base, staging, overlay)
            outside = Path(tmp) / controlled.name
            controlled.rename(outside)

            with self.assertRaisesRegex(ValueError, "固定名称"):
                worker.run_prepared_manifest(
                    outside,
                    probe_runner=lambda _candidate: True,
                )

            self.assertTrue(outside.is_file())
            self.assertEqual((live / "main.py").read_text(), "old-core")

    def test_manifest_is_removed_after_a_written_rolled_back_result(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            manifest = _write_manifest(base, staging, overlay)

            result = worker.run_prepared_manifest(
                manifest,
                probe_runner=lambda _candidate: False,
            )

            self.assertEqual(result["status"], "failed_rolled_back")
            self.assertFalse(manifest.exists())
            self.assertTrue((base / worker.COMFYUI_UPDATE_RESULT).is_file())
            self.assertEqual((live / "main.py").read_text(), "old-core")

    def test_default_probe_uses_quick_ci_mode_without_custom_nodes(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, _staging, _overlay = _prepare_client(tmp, with_overlay=False)
            python = base / "runtime" / "python" / "python.exe"
            _write(python, "portable-python")
            completed = mock.Mock(returncode=0, stdout="ok")

            with mock.patch.object(worker.subprocess, "run", return_value=completed) as run:
                self.assertTrue(worker._default_probe_runner(live))

            command = run.call_args.args[0]
            self.assertEqual(command[0], str(python))
            self.assertIn("--quick-test-for-ci", command)
            self.assertIn("--disable-all-custom-nodes", command)
            self.assertEqual(run.call_args.kwargs["cwd"], str(live))
            self.assertEqual(run.call_args.kwargs["timeout"], 180)

    def test_failed_probe_rolls_back_core_dependencies_and_staged_content(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)

            result = worker.apply_prepared_update(
                base,
                staging,
                overlay,
                RELEASE_METADATA,
                probe_runner=lambda _candidate: False,
            )

            self.assertEqual(result["status"], "failed_rolled_back")
            self.assertFalse(result["success"])
            self.assertTrue(result["rolled_back"])
            self.assertEqual((live / "main.py").read_text(), "old-core")
            self.assertEqual((live / "models" / "keep.bin").read_text(), "model")
            self.assertEqual(
                (base / ".venv" / "Lib" / "site-packages" / "comfy_kitchen" / "version.py").read_text(),
                "old-dependency",
            )
            self.assertEqual((staging / "main.py").read_text(), "new-core")
            self.assertEqual(
                (staging / "input" / "from-release.txt").read_text(),
                "discard-me",
            )
            self.assertEqual(
                (overlay / "comfy_kitchen" / "version.py").read_text(),
                "new-dependency",
            )
            site_packages = base / ".venv" / "Lib" / "site-packages"
            self.assertTrue((site_packages / "comfy_kitchen-1.0.0.dist-info").is_dir())
            self.assertFalse((site_packages / "comfy_kitchen-2.0.0.dist-info").exists())
            self.assertFalse((live / worker.COMFYUI_VERSION_RECORD).exists())
            self.assertEqual(list(base.glob(".comfyui-update-backup-*")), [])
            self.assertEqual(list(base.glob(".comfyui-update-journal-*")), [])

    def test_dist_info_replacement_never_removes_an_unrelated_distribution(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            site_packages = base / ".venv" / "Lib" / "site-packages"
            _write(
                site_packages / "unrelated_package-9.9.9.dist-info" / "METADATA",
                "unrelated",
            )

            result = worker.apply_prepared_update(
                base,
                staging,
                overlay,
                RELEASE_METADATA,
                probe_runner=lambda _candidate: True,
            )

            self.assertEqual(result["status"], "installed")
            self.assertTrue(
                (site_packages / "unrelated_package-9.9.9.dist-info").is_dir()
            )

    def test_workflow_template_split_roots_removed_by_new_distribution_stay_gone(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            site_packages = base / ".venv" / "Lib" / "site-packages"
            _write(
                site_packages / "comfyui_workflow_templates" / "old.py",
                "old-base-template",
            )
            _write(
                site_packages
                / "comfyui_workflow_templates-0.10.0.dist-info"
                / "RECORD",
                "comfyui_workflow_templates/old.py,,\n",
            )
            _write(
                site_packages / "comfyui_workflow_templates_legacy" / "old.py",
                "removed-split",
            )
            _write(
                site_packages
                / "comfyui_workflow_templates_legacy-0.1.0.dist-info"
                / "RECORD",
                "comfyui_workflow_templates_legacy/old.py,,\n",
            )
            _write(
                overlay / "comfyui_workflow_templates" / "new.py",
                "new-base-template",
            )
            _write(
                overlay
                / "comfyui_workflow_templates-0.11.9.dist-info"
                / "METADATA",
                "new-template-metadata",
            )

            result = worker.apply_prepared_update(
                base,
                staging,
                overlay,
                RELEASE_METADATA,
                probe_runner=lambda _candidate: True,
            )

            self.assertEqual(result["status"], "installed")
            self.assertTrue((site_packages / "comfyui_workflow_templates" / "new.py").is_file())
            self.assertFalse((site_packages / "comfyui_workflow_templates_legacy").exists())
            self.assertFalse(
                (site_packages / "comfyui_workflow_templates_legacy-0.1.0.dist-info").exists()
            )

    def test_workflow_subpackage_overlay_replaces_old_split_family_transactionally(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            site_packages = base / ".venv" / "Lib" / "site-packages"
            _write(
                site_packages / "comfyui_workflow_templates_legacy" / "old.py",
                "old-split-template",
            )
            _write(
                site_packages
                / "comfyui_workflow_templates_legacy-0.1.0.dist-info"
                / "RECORD",
                "comfyui_workflow_templates_legacy/old.py,,\n",
            )
            _write(
                overlay / "comfyui_workflow_templates_video" / "new.py",
                "new-video-template",
            )
            _write(
                overlay
                / "comfyui_workflow_templates_video-0.2.0.dist-info"
                / "METADATA",
                "new-video-metadata",
            )

            result = worker.apply_prepared_update(
                base,
                staging,
                overlay,
                RELEASE_METADATA,
                probe_runner=lambda _candidate: True,
            )

            self.assertEqual(result["status"], "installed")
            self.assertFalse((site_packages / "comfyui_workflow_templates_legacy").exists())
            self.assertFalse(
                (site_packages / "comfyui_workflow_templates_legacy-0.1.0.dist-info").exists()
            )
            self.assertEqual(
                (site_packages / "comfyui_workflow_templates_video" / "new.py").read_text(),
                "new-video-template",
            )

    def test_workflow_template_split_roots_are_restored_after_probe_failure(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            site_packages = base / ".venv" / "Lib" / "site-packages"
            _write(
                site_packages / "comfyui_workflow_templates_legacy" / "old.py",
                "removed-split",
            )
            _write(
                site_packages
                / "comfyui_workflow_templates_legacy-0.1.0.dist-info"
                / "RECORD",
                "comfyui_workflow_templates_legacy/old.py,,\n",
            )
            _write(
                overlay / "comfyui_workflow_templates" / "new.py",
                "new-base-template",
            )
            _write(
                overlay
                / "comfyui_workflow_templates-0.11.9.dist-info"
                / "METADATA",
                "new-template-metadata",
            )

            result = worker.apply_prepared_update(
                base,
                staging,
                overlay,
                RELEASE_METADATA,
                probe_runner=lambda _candidate: False,
            )

            self.assertEqual(result["status"], "failed_rolled_back")
            self.assertEqual(
                (site_packages / "comfyui_workflow_templates_legacy" / "old.py").read_text(),
                "removed-split",
            )
            self.assertTrue(
                (site_packages / "comfyui_workflow_templates_legacy-0.1.0.dist-info").is_dir()
            )
            self.assertTrue((overlay / "comfyui_workflow_templates" / "new.py").is_file())

    def test_rejects_staging_outside_the_client_before_any_move(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            outside = Path(tmp) / staging.name
            staging.rename(outside)

            with self.assertRaisesRegex(ValueError, "直接子目录"):
                worker.apply_prepared_update(
                    base,
                    outside,
                    overlay,
                    RELEASE_METADATA,
                    probe_runner=lambda _candidate: True,
                )

            self.assertEqual((live / "main.py").read_text(), "old-core")
            self.assertFalse(list(base.glob(".comfyui-update-backup-*")))

    def test_rejects_git_managed_live_core_and_existing_backup(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            (live / ".git").mkdir()

            with self.assertRaisesRegex(ValueError, "带 .git"):
                worker.apply_prepared_update(
                    base,
                    staging,
                    overlay,
                    RELEASE_METADATA,
                    probe_runner=lambda _candidate: True,
                )

            (live / ".git").rmdir()
            backup = base / f".comfyui-update-backup-{OPERATION_ID}"
            backup.mkdir()
            with self.assertRaisesRegex(ValueError, "备份目录已存在"):
                worker.apply_prepared_update(
                    base,
                    staging,
                    overlay,
                    RELEASE_METADATA,
                    probe_runner=lambda _candidate: True,
                )

    def test_rejects_mismatched_or_unsafe_dependency_overlay(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            mismatched = base / f".comfyui-update-overlay-{'c' * 32}"
            overlay.rename(mismatched)

            with self.assertRaisesRegex(ValueError, "事务 ID 不匹配"):
                worker.apply_prepared_update(
                    base,
                    staging,
                    mismatched,
                    RELEASE_METADATA,
                    probe_runner=lambda _candidate: True,
                )

            mismatched.rename(overlay)
            _write(overlay / "unsafe package.pth", "bad")
            with self.assertRaisesRegex(ValueError, "不安全的顶层条目"):
                worker.apply_prepared_update(
                    base,
                    staging,
                    overlay,
                    RELEASE_METADATA,
                    probe_runner=lambda _candidate: True,
                )

    def test_overlay_allows_only_audited_modules_and_workflow_template_prefixes(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)
            _write(
                overlay / "comfyui_workflow_templates_video" / "__init__.py",
                "official-template-module",
            )
            _write(
                overlay
                / "comfyui_workflow_templates_video-0.11.9.dist-info"
                / "METADATA",
                "official-template-metadata",
            )

            result = worker.apply_prepared_update(
                base,
                staging,
                overlay,
                RELEASE_METADATA,
                probe_runner=lambda _candidate: True,
            )

            self.assertEqual(result["status"], "installed")
            site_packages = base / ".venv" / "Lib" / "site-packages"
            self.assertTrue((site_packages / "comfyui_workflow_templates_video").is_dir())

        for unsafe_name in (
            "requests",
            "bootstrap.pth",
            "Scripts",
            "comfyui_workflow_templates_evil.exe",
        ):
            with self.subTest(unsafe_name=unsafe_name):
                with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
                    base, _live, staging, overlay = _prepare_client(tmp)
                    _write(overlay / unsafe_name, "not allowed")
                    with self.assertRaisesRegex(ValueError, "未授权"):
                        worker.apply_prepared_update(
                            base,
                            staging,
                            overlay,
                            RELEASE_METADATA,
                            probe_runner=lambda _candidate: True,
                        )

    def test_move_failure_after_dependency_replacement_restores_everything(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            original_rename = worker._rename_path

            def fail_on_second_overlay_entry(source: Path, destination: Path):
                if source.parent == overlay and source.name.endswith(".dist-info"):
                    raise OSError("simulated dependency move failure")
                return original_rename(source, destination)

            with mock.patch.object(worker, "_rename_path", fail_on_second_overlay_entry):
                result = worker.apply_prepared_update(
                    base,
                    staging,
                    overlay,
                    RELEASE_METADATA,
                    probe_runner=lambda _candidate: True,
                )

            self.assertEqual(result["status"], "failed_rolled_back")
            self.assertIn("simulated dependency move failure", result["error"])
            self.assertEqual((live / "main.py").read_text(), "old-core")
            self.assertEqual(
                (base / ".venv" / "Lib" / "site-packages" / "comfy_kitchen" / "version.py").read_text(),
                "old-dependency",
            )
            self.assertEqual(
                (overlay / "comfy_kitchen" / "version.py").read_text(),
                "new-dependency",
            )

    def test_rollback_move_failure_is_reported_and_keeps_the_journal(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, live, staging, overlay = _prepare_client(tmp)
            original_rename = worker._rename_path

            def fail_core_rollback(source: Path, destination: Path):
                if source == live and destination == staging:
                    raise OSError("simulated rollback failure")
                return original_rename(source, destination)

            with mock.patch.object(worker, "_rename_path", fail_core_rollback):
                result = worker.apply_prepared_update(
                    base,
                    staging,
                    overlay,
                    RELEASE_METADATA,
                    probe_runner=lambda _candidate: False,
                )

            self.assertEqual(result["status"], "rollback_incomplete")
            self.assertFalse(result["rolled_back"])
            self.assertTrue(result["rollback_errors"])
            self.assertTrue(Path(result["journal_path"]).is_file())

    def test_probe_observes_an_atomic_completed_action_journal(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            base, _live, staging, overlay = _prepare_client(tmp)

            def inspect_journal(_candidate: Path):
                journals = list(base.glob(".comfyui-update-journal-*.json"))
                self.assertEqual(len(journals), 1)
                payload = json.loads(journals[0].read_text(encoding="utf-8"))
                self.assertEqual(payload["phase"], "probing")
                self.assertTrue(payload["actions"])
                self.assertTrue(all(action["state"] == "done" for action in payload["actions"]))
                return True

            result = worker.apply_prepared_update(
                base,
                staging,
                overlay,
                RELEASE_METADATA,
                probe_runner=inspect_journal,
            )

            self.assertEqual(result["status"], "installed")


if __name__ == "__main__":
    unittest.main()
