#!/bin/bash
# Work Reporter 启动脚本
# 用法：./run.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 检查 API Key（支持 ANTHROPIC_AUTH_TOKEN 或 ANTHROPIC_API_KEY）
if [ -z "$ANTHROPIC_AUTH_TOKEN" ] && [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "错误：请先设置 ANTHROPIC_AUTH_TOKEN"
    echo "    export ANTHROPIC_AUTH_TOKEN=sk-..."
    exit 1
fi

# 检查并安装依赖
if ! python3 -c "import anthropic" 2>/dev/null; then
    echo "正在安装依赖..."
    pip3 install -r requirements.txt
fi

echo "启动 Work Reporter..."
python3 work_reporter.py
