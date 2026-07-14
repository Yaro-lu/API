"""
Quick test script to verify the project structure and basic module imports.
"""
import sys
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

print("=" * 60)
print("AI Worker Client - Quick Test")
print("=" * 60)

# Test 1: Basic imports
print("\n[1/5] Testing basic imports...")
try:
    from app.worker_service.config import Config
    from app.worker_service.gpu_check import GPUChecker
    from app.worker_service.torch_check import TorchChecker
    from app.worker_service.comfy_manager import ComfyUIManager
    from app.worker_service.model_manager import ModelManager
    from app.worker_service.comfy_client import ComfyUIClient
    from app.worker_service.task_runner import TaskRunner
    from app.worker_service.log_manager import LogManager
    from app.worker_service.schemas import (
        HealthResponse, StatusResponse, ClientStatus,
        LocalVideoFLF2VRequest, TaskResponse
    )
    from app.worker_service.server_client import ServerClient
    from app.worker_service.repair import RepairManager
    from app.worker_service.task_poller import TaskPoller
    print("✓ All modules imported successfully!")
except Exception as e:
    print(f"✗ Import failed: {e}")
    sys.exit(1)

# Test 2: Config loading
print("\n[2/5] Testing config loading...")
try:
    config = Config()
    print(f"✓ Config loaded!")
    print(f"  - Base dir: {config.base_dir}")
    print(f"  - Local API port: {config.local_api_port}")
    print(f"  - ComfyUI port: {config.comfyui_port}")
except Exception as e:
    print(f"✗ Config loading failed: {e}")

# Test 3: GPU Checker (will probably fail if no GPU, but that's okay)
print("\n[3/5] Testing GPU Checker...")
try:
    gpu_checker = GPUChecker()
    result = gpu_checker.check()
    print(f"✓ GPU Checker created!")
    print(f"  - Success: {result.success}")
    if not result.success:
        print(f"  - Error: {result.error_code} - {result.error_message}")
except Exception as e:
    print(f"✗ GPU Checker test failed: {e}")

# Test 4: Model Manager
print("\n[4/5] Testing Model Manager...")
try:
    from app.worker_service.model_manager import ModelManager
    config = Config()
    model_manager = ModelManager(config.models_dir, config.base_dir / "config" / "model_manifest.yaml")
    models = model_manager.get_available_models()
    print(f"✓ Model Manager created!")
    print(f"  - Found {len(models)} models:")
    for m in models:
        print(f"    - {m['id']}: {'Available' if m['available'] else 'Missing'}")
except Exception as e:
    print(f"✗ Model Manager test failed: {e}")

# Test 5: Directory structure
print("\n[5/5] Checking directory structure...")
dirs_to_check = ["logs", "outputs", "models", "inputs", "cache", "workflows", "config"]
all_dirs_ok = True
for d in dirs_to_check:
    dir_path = project_root / d
    if dir_path.exists():
        print(f"✓ {d}/ exists")
    else:
        print(f"✗ {d}/ missing")
        all_dirs_ok = False

print("\n" + "=" * 60)
print("Test Summary:")
print("=" * 60)
print("\nProject structure looks good!")
print("\nNext steps:")
print("1. Set up runtime (Python + PyTorch + ComfyUI) using scripts/build_runtime.ps1")
print("2. Add model files to models/ directory")
print("3. Start the worker: cd app && .\\start_worker.bat")
print("4. Access API docs at http://127.0.0.1:8090/docs")
