import tempfile
import unittest
from pathlib import Path

from app.core.runtime_package import (
    RUNTIME_PACKAGE_NAME,
    RUNTIME_PACKAGE_SHA256,
    REQUIRED_RUNTIME_PATHS,
    install_staged_runtime,
    invalid_archive_entries,
    missing_archive_entries,
    missing_runtime_paths,
    parse_archive_members,
    read_sha256_sidecar,
    sha256_file,
    validate_staged_runtime,
    verify_runtime_package,
    verify_sha256,
)


class RuntimePackageContractTests(unittest.TestCase):
    def test_archive_requires_portable_dependencies_and_no_nested_root(self):
        members = [path.as_posix() for path in REQUIRED_RUNTIME_PATHS]
        self.assertEqual(missing_archive_entries(members), [])

        nested = [f"package/{member}" for member in members]
        self.assertEqual(
            set(missing_archive_entries(nested)),
            {path.as_posix() for path in REQUIRED_RUNTIME_PATHS},
        )

    def test_runtime_layout_reports_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for relative in REQUIRED_RUNTIME_PATHS[:-1]:
                target = base / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"fixture")
            self.assertEqual(missing_runtime_paths(base), ["bin/cloudflared.exe"])

    def test_runtime_layout_rejects_empty_core_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for relative in REQUIRED_RUNTIME_PATHS:
                target = base / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()

            self.assertEqual(
                set(missing_runtime_paths(base)),
                {path.as_posix() for path in REQUIRED_RUNTIME_PATHS},
            )

    def test_archive_rejects_traversal_and_project_or_user_data(self):
        members = [
            ".venv/Lib/site-packages/torch/__init__.py",
            "runtime/python/python.exe",
            "runtime/ComfyUI/main.py",
            "runtime/ComfyUI/models/secret.safetensors",
            "bin/cloudflared.exe",
            "../outside.txt",
            "runtime/account_session.json",
            "app/gui/main_gateway.py",
        ]
        self.assertEqual(
            invalid_archive_entries(members),
            [
                "runtime/ComfyUI/models/secret.safetensors",
                "../outside.txt",
                "runtime/account_session.json",
                "app/gui/main_gateway.py",
            ],
        )

    def test_7z_compact_listing_preserves_member_paths_with_spaces(self):
        listing = (
            "2026-07-16 10:30:00 ....A 123 100 runtime/python/python.exe\n"
            "2026-07-16 10:30:01 ....A 456 300 runtime/ComfyUI/models/my model.bin\n"
        )

        self.assertEqual(
            parse_archive_members(("7z", "7z.exe"), listing),
            ["runtime/python/python.exe", "runtime/ComfyUI/models/my model.bin"],
        )

    def test_sha256_sidecar_is_parsed_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = base / "runtime.7z"
            package.write_bytes(b"portable runtime fixture")
            expected = sha256_file(package)
            sidecar = base / "runtime.7z.sha256"
            sidecar.write_text(f"{expected}  {package.name}\n", encoding="utf-8")

            self.assertEqual(read_sha256_sidecar(sidecar), expected)
            self.assertEqual(verify_sha256(package, sidecar), (True, expected, expected))

    def test_runtime_package_requires_pinned_hash_even_with_matching_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = base / RUNTIME_PACKAGE_NAME
            package.write_bytes(b"tampered runtime")
            sidecar = Path(f"{package}.sha256")
            actual = sha256_file(package)
            sidecar.write_text(f"{actual}  {package.name}\n", encoding="utf-8")

            valid, expected, observed = verify_runtime_package(package, sidecar)

            self.assertFalse(valid)
            self.assertEqual(expected, RUNTIME_PACKAGE_SHA256)
            self.assertEqual(observed, actual)

    def test_staged_runtime_rejects_extra_roots_and_file_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            for relative in REQUIRED_RUNTIME_PATHS:
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"runtime")
            (staging / "app" / "malicious.py").parent.mkdir(parents=True)
            (staging / "app" / "malicious.py").write_text("bad", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "不允许的路径"):
                validate_staged_runtime(staging)
            (staging / "app" / "malicious.py").unlink()
            (staging / "app").rmdir()
            with self.assertRaisesRegex(ValueError, "文件数量"):
                validate_staged_runtime(staging, max_files=1)

    def test_staged_runtime_install_replaces_only_environment_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "client"
            staging = Path(tmp) / "staging"
            (base / "models").mkdir(parents=True)
            (base / "models" / "keep.bin").write_bytes(b"model")
            (base / "runtime" / "python").mkdir(parents=True)
            (base / "runtime" / "python" / "old.txt").write_text("old", encoding="utf-8")
            (base / ".venv" / "Scripts").mkdir(parents=True)
            (base / ".venv" / "Scripts" / "python.exe").write_bytes(b"broken-launcher")
            (base / ".venv" / "pyvenv.cfg").write_text("stale", encoding="utf-8")
            for relative in REQUIRED_RUNTIME_PATHS:
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"new-runtime")

            install_staged_runtime(staging, base)

            self.assertEqual((base / "models" / "keep.bin").read_bytes(), b"model")
            self.assertFalse((base / "runtime" / "python" / "old.txt").exists())
            self.assertFalse((base / ".venv" / "Scripts").exists())
            self.assertFalse((base / ".venv" / "pyvenv.cfg").exists())
            self.assertEqual(
                (base / "runtime" / "python" / "python.exe").read_bytes(),
                b"new-runtime",
            )


if __name__ == "__main__":
    unittest.main()
