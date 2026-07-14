#!/bin/bash

# AI Worker 启动脚本 (Linux)
cd "$(dirname "$0")"

echo "========================================"
echo "AI Worker - 启动"
echo "========================================"
echo ""

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "[1/4] 创建虚拟环境..."
    python3 -m venv .venv
fi

echo "[2/4] 激活虚拟环境..."
source .venv/bin/activate

echo "[3/4] 安装依赖..."
pip install -r requirements.txt

echo "[4/4] 启动服务..."
echo ""
echo "API 文档地址: http://127.0.0.1:8090/docs"
echo ""

python app/worker_service/main.py
