@echo off
chcp 65001 >nul
echo ========================================
echo AI Worker - 本地开发模式
echo ========================================
echo.

REM 设置本地虚拟环境路径（在 D 盘，避免 Z 盘网络问题）
set VENV_DIR=D:\ai-worker-venv

echo [1/5] 检查本地虚拟环境...
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo   创建虚拟环境到 %VENV_DIR%...
    python -m venv "%VENV_DIR%"
) else (
    echo   ✓ 虚拟环境已存在
)

echo.
echo [2/5] 激活虚拟环境...
call "%VENV_DIR%\Scripts\activate.bat"

echo.
echo [3/5] 安装依赖...
pip install -r requirements.txt

echo.
echo [4/5] 检查环境...
python -c "import fastapi; print('  ✓ FastAPI OK')" 2>nul
if errorlevel 1 (
    echo   ✗ FastAPI 未正确安装
    pause
    exit /b 1
)

echo.
echo [5/5] 启动开发服务器...
echo.
echo API 文档地址: http://127.0.0.1:8090/docs
echo.
echo ========================================
echo.

python app\worker_service\main.py
