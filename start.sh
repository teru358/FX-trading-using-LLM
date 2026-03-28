#!/usr/bin/env bash
# FX Trading Bot — バックグラウンド起動スクリプト
set -e

cd "$(dirname "$0")"
mkdir -p logs

if [ -f .pid ] && kill -0 "$(cat .pid)" 2>/dev/null; then
    echo "Already running (PID: $(cat .pid))"
    exit 1
fi

nohup uv run python main.py --daemon >> logs/daemon.log 2>&1 &
echo $! > .pid
echo "Started (PID: $!)"
