import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional


class LogManager:
    def __init__(self, logs_dir: Path):
        self.logs_dir = logs_dir
        self._safe_mkdir(self.logs_dir)
        self._setup_loggers()

    def _safe_mkdir(self, path: Path):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"Warning: Could not create directory {path}: {e}")

    def _setup_loggers(self):
        for logger_name in ["worker", "comfyui", "gpu"]:
            self._get_or_create_logger(logger_name)

    def _get_or_create_logger(self, name: str) -> logging.Logger:
        logger = logging.getLogger(name)
        if logger.handlers:
            return logger

        logger.setLevel(logging.DEBUG)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)

        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        try:
            log_file = self.logs_dir / f"{name}.log"
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            print(f"Warning: Could not create file logger for {name}: {e}")

        return logger

    def get_logger(self, name: str = "worker") -> logging.Logger:
        return self._get_or_create_logger(name)

    def write_json_log(self, name: str, data: dict):
        try:
            import json
            log_file = self.logs_dir / f"{name}.json"
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Warning: Could not write JSON log {name}: {e}")
