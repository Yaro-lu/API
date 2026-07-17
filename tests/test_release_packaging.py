import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LightweightReleaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release_script = (ROOT / "scripts" / "build_release.ps1").read_text(
            encoding="utf-8-sig"
        )
        cls.installer_script = (ROOT / "installer" / "LingJing.iss").read_text(
            encoding="utf-8-sig"
        )
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8-sig")

    def test_release_does_not_copy_the_heavy_ai_environment(self):
        forbidden_copy_commands = (
            "Copy-AllowlistedTree -Source (Join-Path $SourceRoot 'runtime\\ComfyUI')",
            "Copy-AllowlistedTree -Source (Join-Path $SourceRoot '.venv\\Lib')",
            "Copy-AllowlistedTree -Source (Join-Path $SourceRoot '.venv\\share')",
            "Copy-RequiredFile -Source (Join-Path $SourceRoot 'bin\\cloudflared.exe')",
        )
        for command in forbidden_copy_commands:
            with self.subTest(command=command):
                self.assertNotIn(command, self.release_script)

        self.assertIn("runtime/comfyui", self.release_script.lower())
        self.assertIn("cloudflared", self.release_script.lower())
        self.assertIn("site-packages/(?:torch", self.release_script.lower())

    def test_release_contains_a_self_bootstrapping_gui_and_extractor(self):
        for marker in (
            "BootstrapPythonRoot",
            "BootstrapSitePackagesRoot",
            "SevenZipRoot",
            "$BootstrapPackageEntries",
            "7z.exe",
            "7z.dll",
            "7-Zip-License.txt",
            "bootstrap-python-separate-ai-runtime",
            "environment_included = $false",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.release_script)

        self.assertIn('MyAppExe "runtime\\python\\pythonw.exe"', self.installer_script)
        self.assertIn("轻量客户端", self.installer_script)

    def test_readme_documents_separate_program_environment_and_models(self):
        for marker in ("轻量客户端", "独立运行环境包", "不包含模型"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.readme)


if __name__ == "__main__":
    unittest.main()
