import tempfile
import unittest
from pathlib import Path

from app.core.runtime_package import REQUIRED_RUNTIME_PATHS
from app.gui.dashboard_pages import runtime_package_status


class RuntimePackageStatusTests(unittest.TestCase):
    def test_runtime_status_distinguishes_missing_partial_and_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            state, missing = runtime_package_status(base)
            self.assertEqual(state, "missing")
            self.assertEqual(len(missing), len(REQUIRED_RUNTIME_PATHS))

            first = base / REQUIRED_RUNTIME_PATHS[0]
            first.parent.mkdir(parents=True, exist_ok=True)
            first.write_bytes(b"fixture")
            state, missing = runtime_package_status(base)
            self.assertEqual(state, "repair")
            self.assertEqual(len(missing), len(REQUIRED_RUNTIME_PATHS) - 1)

            for relative in REQUIRED_RUNTIME_PATHS[1:]:
                path = base / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            state, missing = runtime_package_status(base)
            self.assertEqual(state, "ready")
            self.assertEqual(missing, [])

    def test_empty_runtime_file_is_reported_as_repair_not_fresh_install(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            target = base / REQUIRED_RUNTIME_PATHS[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()

            state, missing = runtime_package_status(base)

            self.assertEqual(state, "repair")
            self.assertIn(REQUIRED_RUNTIME_PATHS[0].as_posix(), missing)


if __name__ == "__main__":
    unittest.main()
