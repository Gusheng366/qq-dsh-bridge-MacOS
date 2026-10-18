#!/bin/bash
# 在脚本所在目录启动桥接（供后台任务/定时器调用）
cd "$(dirname "$0")" || exit 1
mkdir -p data
exec python3 bridge.py
