import subprocess
import re
import json
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass


@dataclass
class GPUCheckResult:
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    gpu_name: Optional[str] = None
    vram_gb: Optional[int] = None
    driver_version: Optional[str] = None
    cuda_driver_version: Optional[str] = None


class GPUChecker:
    def __init__(self, min_driver_version: str = "572.61", min_vram_gb: int = 8):
        self.min_driver_version = min_driver_version
        self.min_vram_gb = min_vram_gb

    def _parse_version(self, version_str: str) -> tuple:
        parts = version_str.split(".")
        return tuple(int(p) for p in parts)

    def _check_nvidia_smi(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                return None
            return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def check(self) -> GPUCheckResult:
        output = self._check_nvidia_smi()
        if output is None:
            return GPUCheckResult(
                success=False,
                error_code="NO_NVIDIA_GPU",
                error_message="未检测到 NVIDIA RTX 显卡"
            )

        lines = output.split("\n")
        if not lines:
            return GPUCheckResult(
                success=False,
                error_code="NO_NVIDIA_GPU",
                error_message="未检测到 NVIDIA RTX 显卡"
            )

        first_line = lines[0]
        parts = [p.strip() for p in first_line.split(",")]

        if len(parts) < 3:
            return GPUCheckResult(
                success=False,
                error_code="NO_NVIDIA_GPU",
                error_message="未检测到 NVIDIA RTX 显卡"
            )

        gpu_name = parts[0]
        driver_version = parts[1]
        vram_mb = int(parts[2])
        vram_gb = vram_mb // 1024

        if "RTX" not in gpu_name:
            return GPUCheckResult(
                success=False,
                error_code="UNSUPPORTED_GPU",
                error_message="当前显卡不在支持范围内，仅支持 RTX 20 系以上",
                gpu_name=gpu_name,
                vram_gb=vram_gb,
                driver_version=driver_version
            )

        match = re.search(r"RTX\s*(\d+)", gpu_name)
        if match:
            rtx_series = int(match.group(1))
            if rtx_series < 20:
                return GPUCheckResult(
                    success=False,
                    error_code="UNSUPPORTED_GPU",
                    error_message="当前显卡不在支持范围内，仅支持 RTX 20 系以上",
                    gpu_name=gpu_name,
                    vram_gb=vram_gb,
                    driver_version=driver_version
                )

        if self._parse_version(driver_version) < self._parse_version(self.min_driver_version):
            return GPUCheckResult(
                success=False,
                error_code="DRIVER_TOO_OLD",
                error_message=f"NVIDIA 驱动版本过低，请升级到 {self.min_driver_version} 或更高",
                gpu_name=gpu_name,
                vram_gb=vram_gb,
                driver_version=driver_version
            )

        if vram_gb < self.min_vram_gb:
            return GPUCheckResult(
                success=False,
                error_code="INSUFFICIENT_VRAM",
                error_message=f"显存不足，当前 {vram_gb}GB，最低需要 {self.min_vram_gb}GB",
                gpu_name=gpu_name,
                vram_gb=vram_gb,
                driver_version=driver_version
            )

        cuda_driver_version = self._get_cuda_driver_version()

        return GPUCheckResult(
            success=True,
            gpu_name=gpu_name,
            vram_gb=vram_gb,
            driver_version=driver_version,
            cuda_driver_version=cuda_driver_version
        )

    def _get_cuda_driver_version(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None
