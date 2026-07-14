import subprocess
import json
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass


@dataclass
class TorchCheckResult:
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    torch_version: Optional[str] = None
    cuda_version: Optional[str] = None
    cuda_available: bool = False
    gpu_name: Optional[str] = None
    gpu_memory: Optional[int] = None


class TorchChecker:
    def __init__(self, python_path: Path):
        self.python_path = python_path

    def check(self) -> TorchCheckResult:
        check_script = """
import json
import sys
try:
    import torch
    result = {
        "torch_version": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "cuda_version": getattr(torch.version, 'cuda', None)
    }
    if torch.cuda.is_available():
        result["gpu_name"] = torch.cuda.get_device_name(0)
        result["gpu_memory"] = torch.cuda.get_device_properties(0).total_memory
    print(json.dumps(result))
    sys.exit(0)
except ImportError:
    print(json.dumps({"error": "TORCH_IMPORT_FAILED"}))
    sys.exit(1)
except Exception as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(1)
"""
        try:
            result = subprocess.run(
                [str(self.python_path), "-c", check_script],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                return TorchCheckResult(
                    success=False,
                    error_code="TORCH_IMPORT_FAILED",
                    error_message="PyTorch 无法调用 CUDA，请点击“一键修复环境”"
                )

            data = json.loads(result.stdout)

            if not data.get("cuda_available"):
                return TorchCheckResult(
                    success=False,
                    error_code="CUDA_NOT_AVAILABLE",
                    error_message="PyTorch 无法调用 CUDA，请点击“一键修复环境”",
                    torch_version=data.get("torch_version")
                )

            gpu_memory = data.get("gpu_memory", 0)
            gpu_memory_gb = gpu_memory // (1024 ** 3) if gpu_memory else 0

            return TorchCheckResult(
                success=True,
                torch_version=data.get("torch_version"),
                cuda_version=data.get("cuda_version"),
                cuda_available=True,
                gpu_name=data.get("gpu_name"),
                gpu_memory=gpu_memory_gb
            )

        except subprocess.TimeoutExpired:
            return TorchCheckResult(
                success=False,
                error_code="TORCH_CHECK_TIMEOUT",
                error_message="PyTorch 检测超时"
            )
        except json.JSONDecodeError:
            return TorchCheckResult(
                success=False,
                error_code="TORCH_CHECK_ERROR",
                error_message="PyTorch 检测失败"
            )
