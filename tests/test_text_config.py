import json
import tempfile
import unittest
from pathlib import Path

from app.config import Config


class TextConfigTests(unittest.TestCase):
    def test_default_file_is_created_only_when_professional_settings_are_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = Config(base)

            text_path = base / "runtime" / "config.local.txt"
            self.assertFalse(text_path.exists())

            self.assertEqual(config.ensure_file(), text_path)
            self.assertTrue(text_path.is_file())

    def test_professional_settings_are_saved_as_readable_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = Config(base)
            config.set("server.port", 19001)
            config.set("comfyui.vram_mode", "low")
            config.set("comfyui.launch_args", ["--extra-model-paths-config", "E:\\中文 模型\\路径.txt"])
            config.save()

            text_path = base / "runtime" / "config.local.txt"
            self.assertTrue(text_path.is_file())
            self.assertFalse((base / "runtime" / "config.local.json").exists())
            content = text_path.read_text(encoding="utf-8")
            self.assertIn("[server]", content)
            self.assertIn("port = 19001", content)
            self.assertIn("[comfyui]", content)
            self.assertIn("vram_mode = low", content)
            self.assertIn(
                'launch_args = ["--extra-model-paths-config", "E:\\\\中文 模型\\\\路径.txt"]',
                content,
            )

            loaded = Config(base)
            self.assertEqual(loaded.server_port, 19001)
            self.assertEqual(loaded.get("comfyui.vram_mode"), "low")
            self.assertEqual(
                loaded.get("comfyui.launch_args"),
                ["--extra-model-paths-config", "E:\\中文 模型\\路径.txt"],
            )

    def test_legacy_json_is_migrated_once_to_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            runtime.mkdir()
            legacy = runtime / "config.local.json"
            legacy.write_text(
                json.dumps({"server": {"port": 19002}, "comfyui": {"vram_mode": "high"}}),
                encoding="utf-8",
            )

            config = Config(base)

            self.assertEqual(config.server_port, 19002)
            self.assertEqual(config.get("comfyui.vram_mode"), "high")
            self.assertTrue((runtime / "config.local.txt").is_file())


if __name__ == "__main__":
    unittest.main()
