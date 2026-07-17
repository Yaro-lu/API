import json
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
        cls.runtime_script = (ROOT / "scripts" / "build_runtime_release.ps1").read_text(
            encoding="utf-8-sig"
        )
        cls.runtime_release = json.loads(
            (ROOT / "app" / "runtime_release.json").read_text(encoding="utf-8-sig")
        )
        cls.guide_path = ROOT / "docs" / "灵境造片厂使用教学.pdf"
        cls.example_path = ROOT / "examples" / "灵境造片厂示例页.html"

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

    def test_release_secret_scan_includes_fine_grained_github_tokens(self):
        self.assertIn("github_pat_", self.release_script)

    def test_readme_documents_separate_program_environment_and_models(self):
        for marker in ("轻量客户端", "独立运行环境包", "不包含模型"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.readme)

    def test_runtime_builder_supports_external_source_and_emits_release_manifest(self):
        for marker in (
            "$RuntimeSourceRoot",
            "runtime-nvidia-rtx20plus-cu130",
            "$ReleaseManifestPath",
            "schema_version = 1",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.runtime_script)

    def test_runtime_builder_uses_and_enforces_project_version(self):
        self.assertIn('$ProjectVersionPath = Join-Path $ProjectDir "VERSION"', self.runtime_script)
        self.assertIn("does not match project VERSION", self.runtime_script)

    def test_runtime_release_manifest_pins_one_exact_asset(self):
        package_name = self.runtime_release["package_name"]
        self.assertTrue(package_name.startswith("runtime-nvidia-"))
        self.assertTrue(package_name.endswith(".7z"))
        self.assertEqual(len(self.runtime_release["sha256"]), 64)
        self.assertTrue(self.runtime_release["download_url"].endswith(f"/{package_name}"))

    def test_installer_creates_a_branded_root_launcher(self):
        self.assertIn('#define MyAppName "灵境造片厂"', self.installer_script)
        self.assertIn('Name: "{app}\\{#MyAppName}"', self.installer_script)
        self.assertIn(
            'IconFilename: "{app}\\app\\gui\\assets\\app.ico"',
            self.installer_script,
        )

    def test_release_stages_the_tutorial_pdf_at_the_install_root(self):
        self.assertTrue(self.guide_path.is_file())
        self.assertGreater(self.guide_path.stat().st_size, 10_000)
        self.assertEqual(self.guide_path.read_bytes()[:5], b"%PDF-")
        self.assertIn("docs\\灵境造片厂使用教学.pdf", self.release_script)
        self.assertIn("灵境造片厂使用教学.pdf", self.release_script)

    def test_readme_describes_manual_download_and_installed_tutorial(self):
        for marker in ("https://github.com/Yaro-lu/API", "一键修复", "拉取失败", "灵境造片厂使用教学.pdf"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.readme)

    def test_release_stages_local_example_page_at_install_root(self):
        self.assertTrue(self.example_path.is_file())
        self.assertIn("examples\\灵境造片厂示例页.html", self.release_script)
        self.assertIn("灵境造片厂示例页.html", self.release_script)

    def test_installer_creates_same_logo_desktop_example_shortcut(self):
        self.assertIn('Name: "{autodesktop}\\灵境造片厂示例页"', self.installer_script)
        self.assertIn('Filename: "{app}\\灵境造片厂示例页.html"', self.installer_script)
        self.assertIn(
            'IconFilename: "{app}\\app\\gui\\assets\\app.ico"',
            self.installer_script,
        )
        desktop_task = next(
            line
            for line in self.installer_script.splitlines()
            if line.startswith('Name: "desktopicon";')
        )
        self.assertNotIn("unchecked", desktop_task)


if __name__ == "__main__":
    unittest.main()
