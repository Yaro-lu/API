@echo off
REM 开发模式启动脚本 - 使用系统 Python
chcp 65001
cd /d "%~dp0"
echo ========================================
echo AI Worker - 开发模式启动
echo ========================================
echo.

REM 设置 Python 路径
set PYTHONPATH=%CD%

REM 检查 Python 是否可用
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 或使用 start_worker.bat
    pause
    exit /b 1
)

echo [信息] 使用系统 Python 启动...
echo.

REM 启动服务
python -m app.worker_service.main

pause
