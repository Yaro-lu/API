import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.gui import main_gateway


@unittest.skipUnless(sys.platform == "win32", "Windows file-lock behavior")
class InstanceLockTests(unittest.TestCase):
    def test_second_process_cannot_acquire_same_gateway_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_base = main_gateway.BASE_DIR
            main_gateway.BASE_DIR = Path(temp_dir)
            try:
                self.assertTrue(main_gateway._acquire_instance_lock())
                script = (
                    "import sys;from pathlib import Path;"
                    f"sys.path.insert(0,{str(original_base)!r});"
                    "from app.gui import main_gateway as m;"
                    f"m.BASE_DIR=Path({temp_dir!r});"
                    "print(m._acquire_instance_lock())"
                )
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=str(original_base),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "False")
            finally:
                main_gateway._release_instance_lock()
                main_gateway.BASE_DIR = original_base


if __name__ == "__main__":
    unittest.main()
