import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.core import comfyui_update as update_module
from app.core.comfyui_update import (
    ComfyUIUpdateError,
    ArchiveSecurityError,
    DependencyPolicyError,
    PIP_BOOTSTRAP_CODE,
    build_offline_overlay_pip_command,
    ReleaseValidationError,
    download_release_archive,
    fetch_latest_release,
    plan_dependency_overlay,
    prepare_comfyui_update,
    read_current_comfyui_version,
    safe_extract_release_archive,
    validate_latest_release,
)


class Response(io.BytesIO):
    def __init__(self, payload: bytes, *, url: str, headers=None):
        super().__init__(payload)
        self._url = url
        self.headers = headers or {}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ComfyUIReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.release = {
            "id": 42,
            "tag_name": "v0.3.50",
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "html_url": "https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.3.50",
            "url": "https://api.github.com/repos/Comfy-Org/ComfyUI/releases/42",
            "zipball_url": "https://api.github.com/repos/Comfy-Org/ComfyUI/zipball/v0.3.50",
        }

    def test_accepts_an_official_stable_semver_release(self):
        release = validate_latest_release(self.release)

        self.assertEqual(release.version, "0.3.50")
        self.assertEqual(release.tag_name, "v0.3.50")

    def test_rejects_prerelease_and_non_official_downloads(self):
        prerelease = dict(self.release, prerelease=True)
        with self.assertRaisesRegex(ReleaseValidationError, "稳定版"):
            validate_latest_release(prerelease)

        wrong_repo = dict(
            self.release,
            zipball_url="https://evil.example/repos/Comfy-Org/ComfyUI/zipball/v0.3.50",
        )
        with self.assertRaisesRegex(ReleaseValidationError, "下载地址"):
            validate_latest_release(wrong_repo)

    def test_rejects_non_semver_tags(self):
        with self.assertRaisesRegex(ReleaseValidationError, "版本标签"):
            validate_latest_release(dict(self.release, tag_name="nightly"))

    def test_rejects_missing_or_false_immutable_release_flag(self):
        missing = dict(self.release)
        missing.pop("immutable")
        for payload in (missing, dict(self.release, immutable=False)):
            with self.subTest(immutable=payload.get("immutable", "missing")):
                with self.assertRaisesRegex(ReleaseValidationError, "不可变"):
                    validate_latest_release(payload)

    def test_rejects_a_policy_that_changes_the_canonical_repository(self):
        policy = json.loads(
            (Path(__file__).parents[1] / "app" / "comfyui_release.json").read_text(
                encoding="utf-8"
            )
        )
        policy["repository_url"] = "https://github.com/attacker/ComfyUI"
        with self.assertRaisesRegex(ReleaseValidationError, "策略"):
            validate_latest_release(self.release, policy)

    def test_fetch_latest_release_uses_injected_opener_and_validates_response(self):
        seen = []

        def opener(request, timeout):
            seen.append((request.full_url, timeout, request.get_header("Accept")))
            return Response(
                json.dumps(self.release).encode("utf-8"),
                url="https://api.github.com/repos/Comfy-Org/ComfyUI/releases/latest",
            )

        release = fetch_latest_release(opener=opener)

        self.assertEqual(release.version, "0.3.50")
        self.assertEqual(
            seen,
            [
                (
                    "https://api.github.com/repos/Comfy-Org/ComfyUI/releases/latest",
                    20,
                    "application/vnd.github+json",
                )
            ],
        )


class ComfyUIDownloadAndVersionTests(unittest.TestCase):
    def _release(self):
        return validate_latest_release(
            {
                "id": 42,
                "tag_name": "v0.3.50",
                "draft": False,
                "prerelease": False,
                "immutable": True,
                "html_url": "https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.3.50",
                "url": "https://api.github.com/repos/Comfy-Org/ComfyUI/releases/42",
                "zipball_url": "https://api.github.com/repos/Comfy-Org/ComfyUI/zipball/v0.3.50",
            }
        )

    def test_reads_version_assignment_without_executing_source(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            root = Path(tmp)
            (root / "comfyui_version.py").write_text(
                '__version__ = "0.3.49"\nraise RuntimeError("must not execute")\n',
                encoding="utf-8",
            )

            self.assertEqual(read_current_comfyui_version(root), "0.3.49")

    def test_download_reports_byte_progress_and_returns_sha256(self):
        payload = b"zip bytes from github"
        progress = []

        def opener(_request, timeout):
            self.assertEqual(timeout, 60)
            return Response(
                payload,
                url="https://codeload.github.com/Comfy-Org/ComfyUI/legacy.zip/refs/tags/v0.3.50",
                headers={"Content-Length": str(len(payload))},
            )

        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            target = Path(tmp) / "release.zip"
            result = download_release_archive(
                self._release(),
                target,
                opener=opener,
                progress_callback=lambda done, total: progress.append((done, total)),
                chunk_size=5,
            )

            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(result.bytes_downloaded, len(payload))
            self.assertEqual(progress[-1], (len(payload), len(payload)))

    def test_download_rejects_redirect_to_an_untrusted_host_and_removes_partial(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            target = Path(tmp) / "release.zip"
            with self.assertRaisesRegex(ComfyUIUpdateError, "重定向"):
                download_release_archive(
                    self._release(),
                    target,
                    opener=lambda *_args, **_kwargs: Response(
                        b"bad", url="https://evil.example/release.zip"
                    ),
                )
            self.assertFalse(target.exists())
            self.assertFalse(Path(f"{target}.part").exists())

    def test_partial_cleanup_failure_does_not_mask_download_validation_error(self):
        payload = b"incomplete"

        def opener(_request, **_kwargs):
            return Response(
                payload,
                url="https://codeload.github.com/Comfy-Org/ComfyUI/legacy.zip/refs/tags/v0.3.50",
                headers={"Content-Length": str(len(payload) + 10)},
            )

        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            target = Path(tmp) / "release.zip"
            partial = Path(f"{target}.part")
            original_unlink = Path.unlink

            def locked_partial(path, *args, **kwargs):
                if Path(path) == partial:
                    raise PermissionError("simulated antivirus lock")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", new=locked_partial):
                with self.assertRaisesRegex(ComfyUIUpdateError, "不完整") as raised:
                    download_release_archive(self._release(), target, opener=opener)

            self.assertTrue(partial.is_file())
            self.assertTrue(
                any("临时文件清理警告" in note for note in raised.exception.__notes__)
            )


class ComfyUIArchiveSafetyTests(unittest.TestCase):
    required = {
        "main.py": b"print('comfy')\n",
        "requirements.txt": b"torch==2.7.0\n",
        "pyproject.toml": b"[project]\nname='comfyui'\n",
        "comfyui_version.py": b'__version__ = "0.3.50"\n',
        "LICENSE": b"GPL\n",
    }

    def _write_zip(self, path: Path, entries: dict[str, bytes]):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, payload in entries.items():
                bundle.writestr(name, payload)

    def test_extracts_one_root_strips_it_and_excludes_protected_user_paths(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            base = Path(tmp)
            archive = base / "release.zip"
            entries = {f"ComfyUI-v0.3.50/{name}": data for name, data in self.required.items()}
            entries.update(
                {
                    "ComfyUI-v0.3.50/comfy/core.py": b"core",
                    "ComfyUI-v0.3.50/models/do-not-install.bin": b"model",
                    "ComfyUI-v0.3.50/custom_nodes/user_node.py": b"user",
                    "ComfyUI-v0.3.50/extra_model_paths.yaml": b"user config",
                }
            )
            self._write_zip(archive, entries)

            result = safe_extract_release_archive(archive, base / "staging")

            self.assertEqual((result.root / "comfy" / "core.py").read_bytes(), b"core")
            self.assertFalse((result.root / "models").exists())
            self.assertFalse((result.root / "custom_nodes").exists())
            self.assertFalse((result.root / "extra_model_paths.yaml").exists())
            self.assertEqual(
                set(result.excluded_protected_paths),
                {"models", "custom_nodes", "extra_model_paths.yaml"},
            )

    def test_rejects_traversal_multiple_roots_and_symlinks(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            base = Path(tmp)
            traversal = base / "traversal.zip"
            self._write_zip(
                traversal,
                {"ComfyUI-v0.3.50/../outside.txt": b"bad"},
            )
            with self.assertRaisesRegex(ArchiveSecurityError, "路径"):
                safe_extract_release_archive(traversal, base / "traversal-out")

            multiple = base / "multiple.zip"
            self._write_zip(multiple, {"root-a/a.txt": b"a", "root-b/b.txt": b"b"})
            with self.assertRaisesRegex(ArchiveSecurityError, "单一根目录"):
                safe_extract_release_archive(multiple, base / "multiple-out")

            symlink = base / "symlink.zip"
            with zipfile.ZipFile(symlink, "w") as bundle:
                info = zipfile.ZipInfo("ComfyUI-v0.3.50/link")
                info.create_system = 3
                info.external_attr = (0o120777 << 16)
                bundle.writestr(info, "main.py")
            with self.assertRaisesRegex(ArchiveSecurityError, "链接"):
                safe_extract_release_archive(symlink, base / "symlink-out")

    def test_rejects_entry_and_uncompressed_size_limits(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            base = Path(tmp)
            archive = base / "limit.zip"
            self._write_zip(
                archive,
                {"root/a.txt": b"1234", "root/b.txt": b"5678"},
            )
            policy = json.loads(
                (Path(__file__).parents[1] / "app" / "comfyui_release.json").read_text(
                    encoding="utf-8"
                )
            )
            policy["max_archive_members"] = 1
            with self.assertRaisesRegex(ArchiveSecurityError, "条目数量"):
                safe_extract_release_archive(archive, base / "count-out", policy=policy)

            policy["max_archive_members"] = 10
            policy["max_extracted_bytes"] = 7
            with self.assertRaisesRegex(ArchiveSecurityError, "解压大小"):
                safe_extract_release_archive(archive, base / "size-out", policy=policy)


class ComfyUIDependencyPolicyTests(unittest.TestCase):
    def _requirements(self, base: Path, current: str, target: str):
        current_path = base / "current.txt"
        target_path = base / "target.txt"
        current_path.write_text(current, encoding="utf-8")
        target_path.write_text(target, encoding="utf-8")
        return current_path, target_path

    def test_allows_only_changed_pinned_official_overlay_packages(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            current, target = self._requirements(
                Path(tmp),
                "torch==2.7.0\nnumpy==2.1.0\ncomfyui-frontend-package==1.20.0\n",
                "torch==2.7.0\nnumpy==2.1.0\ncomfyui-frontend-package==1.21.0\n",
            )

            plan = plan_dependency_overlay(current, target)

            self.assertFalse(plan.full_environment_required)
            self.assertEqual(
                plan.overlay_requirements,
                ("comfyui-frontend-package==1.21.0",),
            )

    def test_gpu_or_unapproved_dependency_changes_require_full_environment(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            current, target = self._requirements(
                Path(tmp),
                "torch==2.7.0\nnumpy==2.1.0\n",
                "torch==2.8.0\nnumpy==2.2.0\nnvidia-cuda-runtime-cu13==13.0\n",
            )

            plan = plan_dependency_overlay(current, target)

            self.assertTrue(plan.full_environment_required)
            self.assertEqual(plan.overlay_requirements, ())
            self.assertIn("torch", plan.blocked_changes)
            self.assertIn("nvidia-cuda-runtime-cu13", plan.blocked_changes)
            self.assertIn("numpy", plan.unsupported_changes)

    def test_rejects_requirement_options_urls_and_unpinned_overlay_packages(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            current, target = self._requirements(Path(tmp), "", "--extra-index-url https://evil\n")
            with self.assertRaisesRegex(DependencyPolicyError, "不安全"):
                plan_dependency_overlay(current, target)

            target.write_text("comfy-kitchen>=0.2\n", encoding="utf-8")
            plan = plan_dependency_overlay(current, target)
            self.assertTrue(plan.full_environment_required)
            self.assertIn("comfy-kitchen", plan.unsupported_changes)

    def test_builds_shell_free_exact_pin_binary_only_pypi_command(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            base = Path(tmp)
            operation = "a" * 32
            requirements = base / f".comfyui-update-requirements-{operation}.txt"
            requirements.write_text("comfy-kitchen==0.2.0\n", encoding="utf-8")
            overlay = base / f".comfyui-update-overlay-{operation}"
            updater_root = base / "app" / "updater_runtime"
            (updater_root / "pip").mkdir(parents=True)
            (updater_root / "pip" / "__main__.py").write_text("", encoding="utf-8")

            command = build_offline_overlay_pip_command(
                base / "runtime" / "python" / "python.exe",
                requirements,
                updater_root,
                overlay,
                base_dir=base,
            )

            self.assertEqual(
                command[:5],
                [
                    str(base / "runtime" / "python" / "python.exe"),
                    "-c",
                    PIP_BOOTSTRAP_CODE,
                    str(updater_root.resolve()),
                    "--isolated",
                ],
            )
            self.assertEqual(command[5], "install")
            self.assertNotIn(str(updater_root), PIP_BOOTSTRAP_CODE)
            self.assertNotIn(str(requirements), PIP_BOOTSTRAP_CODE)
            self.assertNotIn(str(overlay), PIP_BOOTSTRAP_CODE)
            self.assertNotIn("PYTHONPATH", PIP_BOOTSTRAP_CODE)
            for flag in (
                "--isolated",
                "--index-url",
                "--only-binary=:all:",
                "--no-cache-dir",
                "--disable-pip-version-check",
                "--no-input",
                "--target",
            ):
                self.assertIn(flag, command)
            self.assertIn("https://pypi.org/simple", command)
            self.assertNotIn("--no-index", command)
            self.assertNotIn("--find-links", command)
            self.assertNotIn("--no-deps", command)
            self.assertNotIn("shell", command)

    def test_fixed_bootstrap_imports_bundled_pip_without_pythonpath(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            updater_root = Path(tmp) / "updater_runtime"
            pip_package = updater_root / "pip"
            pip_package.mkdir(parents=True)
            (pip_package / "__init__.py").write_text("", encoding="utf-8")
            (pip_package / "__main__.py").write_text(
                "import json,sys; print(json.dumps(sys.argv))\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    PIP_BOOTSTRAP_CODE,
                    str(updater_root),
                    "probe",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=dict(os.environ, PYTHONPATH="E:\\must-not-be-used"),
            )

            self.assertEqual(json.loads(result.stdout), ["pip", "probe"])


class ComfyUIPreparationTests(unittest.TestCase):
    def test_rejects_git_managed_live_core_before_any_network_request(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            base = Path(tmp)
            live = base / "runtime" / "ComfyUI"
            live.mkdir(parents=True)
            (live / ".git").write_text("gitdir: user-owned", encoding="utf-8")
            (live / "comfyui_version.py").write_text('__version__ = "0.3.49"\n', encoding="utf-8")
            (live / "requirements.txt").write_text("torch==2.7.0\n", encoding="utf-8")
            calls = []

            with self.assertRaisesRegex(ComfyUIUpdateError, "Git"):
                prepare_comfyui_update(
                    base,
                    opener=lambda *_args, **_kwargs: calls.append(True),
                )

            self.assertEqual(calls, [])

    def test_prepare_returns_worker_manifest_without_mutating_live_core(self):
        with tempfile.TemporaryDirectory(dir="E:\\CodexTemp") as tmp:
            base = Path(tmp)
            live = base / "runtime" / "ComfyUI"
            live.mkdir(parents=True)
            updater_pip = base / "app" / "updater_runtime" / "pip"
            updater_pip.mkdir(parents=True)
            (updater_pip / "__main__.py").write_text("", encoding="utf-8")
            (live / "comfyui_version.py").write_text('__version__ = "0.3.49"\n', encoding="utf-8")
            (live / "requirements.txt").write_text(
                "torch==2.7.0\ncomfyui-frontend-package==1.20.0\n", encoding="utf-8"
            )
            archive_bytes = io.BytesIO()
            entries = dict(ComfyUIArchiveSafetyTests.required)
            entries["requirements.txt"] = (
                b"torch==2.7.0\ncomfyui-frontend-package==1.21.0\n"
            )
            with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as bundle:
                for name, payload in entries.items():
                    bundle.writestr(f"ComfyUI-v0.3.50/{name}", payload)
            release_payload = {
                "id": 42,
                "tag_name": "v0.3.50",
                "draft": False,
                "prerelease": False,
                "immutable": True,
                "html_url": "https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.3.50",
                "url": "https://api.github.com/repos/Comfy-Org/ComfyUI/releases/42",
                "zipball_url": "https://api.github.com/repos/Comfy-Org/ComfyUI/zipball/v0.3.50",
            }
            operation = "b" * 32
            archive_path = base / f".comfyui-update-release-{operation}.zip"
            stale_archive = base / f".comfyui-update-release-{'c' * 32}.zip.part"
            stale_archive.write_bytes(b"stale")
            old_time = time.time() - update_module._STALE_ARCHIVE_AGE_SECONDS - 60
            os.utime(stale_archive, (old_time, old_time))
            original_unlink = Path.unlink

            def locked_current_archive(path, *args, **kwargs):
                if Path(path) == archive_path:
                    raise PermissionError("simulated antivirus lock")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", new=locked_current_archive):
                prepared = prepare_comfyui_update(
                    base,
                    release_payload=release_payload,
                    operation_id=operation,
                    opener=lambda *_args, **_kwargs: Response(
                        archive_bytes.getvalue(),
                        url="https://codeload.github.com/Comfy-Org/ComfyUI/legacy.zip/refs/tags/v0.3.50",
                        headers={"Content-Length": str(len(archive_bytes.getvalue()))},
                    ),
                )

            manifest = prepared.to_manifest()
            self.assertEqual(manifest["status"], "overlay_build_required")
            self.assertEqual(
                manifest["staging_core"],
                str(base / f".comfyui-update-staging-{operation}"),
            )
            self.assertEqual(
                manifest["dependency_overlay"],
                str(base / f".comfyui-update-overlay-{operation}"),
            )
            self.assertEqual(manifest["release_metadata"]["archive_sha256"], hashlib.sha256(archive_bytes.getvalue()).hexdigest())
            self.assertEqual(manifest["release_metadata"]["immutable"], True)
            self.assertTrue(manifest["cleanup_warnings"])
            self.assertIn("antivirus lock", manifest["cleanup_warnings"][0])
            self.assertTrue(archive_path.is_file())
            self.assertFalse(stale_archive.exists())
            self.assertEqual((live / "comfyui_version.py").read_text(encoding="utf-8"), '__version__ = "0.3.49"\n')


if __name__ == "__main__":
    unittest.main()
