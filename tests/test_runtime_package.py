import tempfile
import unittest
from pathlib import Path

from app.core.runtime_package import (
    REQUIRED_RUNTIME_PATHS,
    invalid_archive_entries,
    missing_archive_entries,
    missing_runtime_paths,
    read_sha256_sidecar,
    sha256_file,
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
            "bin/cloudflared.exe",
            "../outside.txt",
            "runtime/account_session.json",
            "app/gui/main_gateway.py",
        ]
        self.assertEqual(
            invalid_archive_entries(members),
            ["../outside.txt", "runtime/account_session.json", "app/gui/main_gateway.py"],
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


if __name__ == "__main__":
    unittest.main()
