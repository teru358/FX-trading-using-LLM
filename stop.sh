#!/usr/bin/env bash
# FX Trading Bot — 停止スクリプト
cd "$(dirname "$0")"

if [ ! -f .pid ]; then
    echo "Not running (.pid not found)"
    exit 1
fi

PID=$(cat .pid)

if ! kill -0 "$PID" 2>/dev/null; then
    echo "Process $PID not found (already stopped?)"
    rm -f .pid
    exit 1
fi

kill "$PID"
rm -f .pid
echo "Stopped (PID: $PID)"
