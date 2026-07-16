import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.core.workflow_import import (
    cleanup_stale_workflow_imports,
    copy_workflow_assets,
    ensure_safe_workflows_root,
    extract_zip_safely,
)


class WorkflowImportSafetyTests(unittest.TestCase):
    @staticmethod
    def _zip(path: Path, entries: dict[str, bytes]) -> None:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, content in entries.items():
                bundle.writestr(name, content)

    def test_extracts_a_small_supported_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "workflow.zip"
            destination = root / "staging"
            destination.mkdir()
            self._zip(
                archive,
                {
                    "package/workflow.json": b'{"1":{"class_type":"SaveImage","inputs":{}}}',
                    "package/README.md": b"safe",
                },
            )

            extract_zip_safely(archive, destination)

            self.assertTrue((destination / "package" / "workflow.json").is_file())
            self.assertEqual((destination / "package" / "README.md").read_bytes(), b"safe")

    def test_rejects_unsafe_windows_and_traversal_names(self):
        unsafe_names = (
            "../escape.json",
            "/absolute.json",
            "C:/drive.json",
            "folder/file.json:stream",
            "CON.json",
            ".git/workflow.json",
            "bad?.json",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index, unsafe_name in enumerate(unsafe_names):
                with self.subTest(name=unsafe_name):
                    archive = root / f"unsafe-{index}.zip"
                    destination = root / f"staging-{index}"
                    destination.mkdir()
                    self._zip(archive, {unsafe_name: b"{}"})
                    with self.assertRaises(ValueError):
                        extract_zip_safely(archive, destination)
                    self.assertFalse(destination.exists())

    def test_rejects_case_collisions_models_and_oversized_content(self):
        cases = (
            {
                "Workflow.json": b"{}",
                "workflow.JSON": b"{}",
            },
            {"models/unsafe.safetensors": b"model"},
            {"workflow.json": b"x" * 33},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index, entries in enumerate(cases):
                with self.subTest(index=index):
                    archive = root / f"invalid-{index}.zip"
                    destination = root / f"destination-{index}"
                    destination.mkdir()
                    self._zip(archive, entries)
                    with self.assertRaises(ValueError):
                        extract_zip_safely(archive, destination, max_bytes=32)

    def test_existing_destination_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "broken.zip"
            archive.write_bytes(b"not a zip")
            destination = root / "keep"
            destination.mkdir()
            marker = destination / "keep.txt"
            marker.write_text("user data", encoding="utf-8")

            with self.assertRaises(ValueError):
                extract_zip_safely(archive, destination)

            self.assertEqual(marker.read_text(encoding="utf-8"), "user data")

    def test_reparse_destination_is_rejected_before_opening_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "workflow.zip"
            destination = root / "staging"
            destination.mkdir()
            self._zip(archive, {"workflow.json": b"{}"})
            with mock.patch(
                "app.core.workflow_import._is_reparse_point",
                side_effect=lambda path: Path(path) == destination,
            ):
                with self.assertRaises(ValueError):
                    extract_zip_safely(archive, destination)

    def test_workflows_root_rejects_a_windows_junction_or_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows = Path(temp_dir) / "workflows"
            workflows.mkdir()
            with mock.patch(
                "app.core.workflow_import._is_reparse_point",
                side_effect=lambda path: Path(path) == workflows,
            ):
                with self.assertRaises(ValueError):
                    ensure_safe_workflows_root(workflows)

    def test_folder_copy_rejects_models_and_keeps_destination_transactional(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "staging"
            source.mkdir()
            destination.mkdir()
            (source / "workflow.json").write_text("{}", encoding="utf-8")
            (source / "model.safetensors").write_bytes(b"model")

            with self.assertRaises(ValueError):
                copy_workflow_assets(source, destination)

            self.assertEqual(list(destination.iterdir()), [])

    def test_cleanup_removes_only_owned_staging_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workflows = root / "workflows"
            temp_root = root / "runtime" / "workflow_import_tmp"
            workflows.mkdir()
            temp_root.mkdir(parents=True)
            for path in (
                workflows / ".importing_old",
                temp_root / "import_old",
                temp_root / "converted",
            ):
                path.mkdir()
                (path / "temp.json").write_text("{}", encoding="utf-8")
            keep_workflow = workflows / "real_workflow"
            keep_temp = temp_root / "notes"
            keep_workflow.mkdir()
            keep_temp.mkdir()

            removed = cleanup_stale_workflow_imports(workflows, root / "runtime")

            self.assertEqual(removed, 3)
            self.assertTrue(keep_workflow.is_dir())
            self.assertTrue(keep_temp.is_dir())


if __name__ == "__main__":
    unittest.main()
