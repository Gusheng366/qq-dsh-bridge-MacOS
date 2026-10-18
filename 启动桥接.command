#!/bin/bash
# QQ ↔ DSH 桥接启动器 —— 双击运行
cd "$(dirname "$0")" || exit 1

echo "=============================================="
echo "  QQ ↔ DeepSeek Harness 桥接"
echo "=============================================="
echo ""

# 检查 QQ 是否运行
if ! pgrep -x QQ > /dev/null; then
    echo "⚠️  QQ 未运行，正在启动..."
    open -a QQ
    sleep 5
fi

# 检查 python3
if ! command -v python3 > /dev/null; then
    echo "❌ 找不到 python3"
    read -rn1 -p "按任意键退出..."
    exit 1
fi

echo "▶ 启动桥接（按 Ctrl+C 停止）"
echo ""
exec python3 bridge.py
