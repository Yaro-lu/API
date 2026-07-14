@echo off
REM 环境检查脚本
chcp 65001
cd /d "%~dp0"
echo ========================================
echo AI Worker - 环境检查
echo ========================================
echo.

echo [1/5] 检查目录结构...
if exist "runtime\" (echo   ✓ runtime 目录存在) else (echo   ✗ runtime 目录不存在)
if exist "models\" (echo   ✓ models 目录存在) else (echo   ✗ models 目录不存在)
if exist "logs\" (echo   ✓ logs 目录存在) else (echo   ✗ logs 目录不存在)
if exist "outputs\" (echo   ✓ outputs 目录存在) else (echo   ✗ outputs 目录不存在)
echo.

echo [2/5] 检查 Python...
if exist "runtime\python\python.exe" (
    echo   ✓ 嵌入式 Python 存在
) else (
    echo   ✗ 嵌入式 Python 不存在
    echo   ℹ  请查看 DOWNLOAD_GUIDE.md 下载 Python
)
echo.

echo [3/5] 检查 ComfyUI...
if exist "runtime\ComfyUI\main.py" (
    echo   ✓ ComfyUI 存在
) else (
    echo   ✗ ComfyUI 不存在
    echo   ℹ  请查看 DOWNLOAD_GUIDE.md 下载 ComfyUI
)
echo.

echo [4/5] 检查 FFmpeg...
if exist "runtime\ffmpeg\bin\ffmpeg.exe" (
    echo   ✓ FFmpeg 存在
) else (
    echo   ✗ FFmpeg 不存在
    echo   ℹ  请查看 DOWNLOAD_GUIDE.md 下载 FFmpeg
)
echo.

echo [5/5] 检查模型文件...
set models_ok=1

echo   Flux.2 (文生图):
if not exist "models\diffusion_models\flux-2-klein-9b-fp8.safetensors" (
    echo     ✗ 主模型缺失
    set models_ok=0
) else (
    echo     ✓ 主模型就绪
)
if not exist "models\text_encoders\qwen_3_8b_fp8mixed.safetensors" (
    echo     ✗ Qwen文本编码器缺失
    set models_ok=0
) else (
    echo     ✓ Qwen文本编码器就绪
)
if not exist "models\vae\full_encoder_small_decoder.safetensors" (
    echo     ✗ VAE缺失
    set models_ok=0
) else (
    echo     ✓ VAE就绪
)

echo.
echo   Wan 2.1 (视频):
if not exist "models\diffusion_models\wan2.1_flf2v_14b_fp8.safetensors" (
    echo     ✗ 主模型缺失
) else (
    echo     ✓ 主模型就绪
)
if not exist "models\vae\wan_2.1_vae.safetensors" (
    echo     ✗ VAE缺失
) else (
    echo     ✓ VAE就绪
)

if %models_ok%==1 (echo.
echo   ✓ Flux.2 模型已就绪)
echo.

echo ========================================
echo 检查完成！
echo.
echo 详细下载和安装指南请查看：DOWNLOAD_GUIDE.md
echo.
pause
