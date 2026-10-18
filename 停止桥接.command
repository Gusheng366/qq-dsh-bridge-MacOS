#!/bin/bash
# QQ ↔ DSH 桥接停止器 —— 双击运行
cd "$(dirname "$0")" || exit 1

echo "=============================================="
echo "  停止 QQ ↔ DSH 桥接"
echo "=============================================="
echo ""

PID=""
if [ -f "data/bridge.pid" ]; then
    PID=$(cat "data/bridge.pid" 2>/dev/null | tr -d '[:space:]')
fi

# 校验该 PID 确实是桥接进程
if [ -n "$PID" ] && ps -p "$PID" -o command= 2>/dev/null | grep -q "bridge.py"; then
    echo "找到桥接进程 PID: $PID"
    kill "$PID" 2>/dev/null
    for i in $(seq 1 10); do
        if ! ps -p "$PID" > /dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "进程未响应，强制结束..."
        kill -9 "$PID" 2>/dev/null
        sleep 1
    fi
    echo "✅ 已停止 (PID $PID)"
else
    # 兜底：按名字查找
    PIDS=$(pgrep -f "bridge.py" 2>/dev/null)
    if [ -n "$PIDS" ]; then
        echo "按进程名找到: $PIDS"
        for p in $PIDS; do
            kill "$p" 2>/dev/null
        done
        sleep 2
        # 还有残留就强杀
        REMAIN=$(pgrep -f "bridge.py" 2>/dev/null)
        if [ -n "$REMAIN" ]; then
            echo "$REMAIN" | xargs kill -9 2>/dev/null
        fi
        echo "✅ 已停止"
    else
        echo "ℹ️  桥接没有在运行"
    fi
fi

# 清理锁与 PID 文件
rm -f "data/bridge.lock" "data/bridge.pid" 2>/dev/null
echo ""
echo "（关闭本窗口即可）"
sleep 2
