import json
import tempfile
import unittest
from pathlib import Path

from app.core.runtime_state import RuntimeState


class RuntimeStateTests(unittest.TestCase):
    def test_access_key_persists_and_offline_clears_public_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            state = RuntimeState(runtime_dir)
            state.start_session()
            state.set_api_key("sk-local-test_1234567890")
            state.set_online("https://example.trycloudflare.com")
            state.set_offline()

            saved = json.loads((runtime_dir / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["api_key"], "sk-local-test_1234567890")
            self.assertEqual(saved["status"], "offline")
            self.assertEqual(saved["base_url"], "")

    def test_stale_api_instance_cannot_overwrite_new_access_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            api_state = RuntimeState(runtime_dir)
            api_state.start_session()
            api_state.set_api_key("sk-local-old_1234567890")
            gui_state = RuntimeState(runtime_dir)

            gui_state.set_api_key("sk-local-new_1234567890")
            api_state.set_offline()

            saved = json.loads((runtime_dir / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["api_key"], "sk-local-new_1234567890")
            self.assertEqual(saved["status"], "offline")


if __name__ == "__main__":
    unittest.main()
