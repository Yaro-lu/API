import subprocess
import shutil
from pathlib import Path
from typing import Optional
import logging


class RepairManager:
    def __init__(self, base_dir: Path, logger: Optional[logging.Logger] = None):
        self.base_dir = base_dir
        self.logger = logger or logging.getLogger(__name__)

    def quick_repair(self) -> bool:
        self.logger.info("Starting quick repair...")
        try:
            self._backup_logs()

            site_packages = self.base_dir / "runtime" / "python" / "Lib" / "site-packages"
            if site_packages.exists():
                pass

            return self._run_repair_script("quick")
        except Exception as e:
            self.logger.error(f"Quick repair failed: {e}")
            return False

    def full_repair(self) -> bool:
        self.logger.info("Starting full repair...")
        try:
            self._backup_logs()
            return self._run_repair_script("full")
        except Exception as e:
            self.logger.error(f"Full repair failed: {e}")
            return False

    def _backup_logs(self):
        logs_dir = self.base_dir / "logs"
        backup_dir = self.base_dir / "logs_backup"

        if logs_dir.exists():
            backup_dir.mkdir(exist_ok=True)
            timestamp = str(int(logs_dir.stat().st_mtime))
            shutil.copytree(logs_dir, backup_dir / f"logs_{timestamp}", dirs_exist_ok=True)

    def _run_repair_script(self, mode: str) -> bool:
        script_path = self.base_dir / "scripts" / "repair_runtime.ps1"
        if not script_path.exists():
            self.logger.error(f"Repair script not found: {script_path}")
            return False

        try:
            result = subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script_path), "-Mode", mode],
                capture_output=True,
                text=True
            )
            self.logger.info(result.stdout)
            if result.stderr:
                self.logger.error(result.stderr)
            return result.returncode == 0
        except Exception as e:
            self.logger.error(f"Failed to run repair script: {e}")
            return False
