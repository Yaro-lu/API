@echo off
chcp 65001 >nul
title 灵镜造片厂

echo ========================================
echo 灵镜造片厂 - 启动
echo ========================================
echo.

cd /d "%~dp0"

echo [1/3] 检查环境...
if not exist "runtime\python\python.exe" (
    echo   错误：未找到 runtime\python\python.exe
    echo   请确保 runtime-nvidia-rtx30plus-cu130-v1.0.0.7z 已解压
    pause
    exit /b 1
)
echo   环境检查通过
echo.

echo [2/3] 检查模型...
if not exist "models\diffusion_models\flux-2-klein-9b-fp8.safetensors" (
    echo   警告：未找到 Flux.2 主模型
)
if not exist "models\text_encoders\qwen_3_8b_fp8mixed.safetensors" (
    echo   警告：未找到 Qwen 文本编码器
)
if not exist "models\vae\full_encoder_small_decoder.safetensors" (
    echo   警告：未找到 VAE 模型
)
echo.

echo [3/3] 启动灵镜造片厂桌面程序...
echo.

set PYTHONPATH=%~dp0
".\runtime\python\python.exe" -u -B "app\gui\main_gateway.py"

if errorlevel 1 (
    echo.
    echo 启动失败！请查看 runtime\logs\ 下的日志文件。
    pause
)
