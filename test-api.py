"""
简单的 API 测试脚本
使用前请先启动服务：python -m app.worker_service.main
或运行：start-dev.bat
"""
import requests
import json
import time

BASE_URL = "http://127.0.0.1:8090"

def print_response(response):
    print(f"状态码: {response.status_code}")
    try:
        print(f"响应: {json.dumps(response.json(), ensure_ascii=False, indent=2)}")
    except:
        print(f"响应: {response.text}")
    print()

print("=" * 60)
print("AI Worker API 测试")
print("=" * 60)
print()

# 测试 1: 健康检查
print("[测试 1] 健康检查 /health")
try:
    response = requests.get(f"{BASE_URL}/health")
    print_response(response)
except Exception as e:
    print(f"错误: {e}")
    print("提示: 请先启动服务！")
    exit(1)

# 测试 2: 状态检查
print("[测试 2] 状态检查 /status")
try:
    response = requests.get(f"{BASE_URL}/status")
    print_response(response)
except Exception as e:
    print(f"错误: {e}")

# 测试 3: 列出可用的 API 文档
print("[提示]")
print(f"API 文档地址: {BASE_URL}/docs")
print(f"交互式文档: {BASE_URL}/redoc")
print()
print("=" * 60)
print("基础测试完成！")
print("=" * 60)
