#!/usr/bin/env python3
"""ohlcv キャッシュを symbol 単位でクリアする移行スクリプト (tz 正規化後の掃除)。

旧 naive UTC bar を削除し、次回 technical collection で naive local 規約で再フェッチ
させる。既定は dry-run (件数表示のみ)。実削除は --yes が必須。

使い方:
  uv run python scripts/clear_ohlcv_cache.py USDJPY=X EURUSD=X          # dry-run
  uv run python scripts/clear_ohlcv_cache.py USDJPY=X EURUSD=X --yes    # 実削除
  uv run python scripts/clear_ohlcv_cache.py --all --yes               # 全 symbol
  uv run python scripts/clear_ohlcv_cache.py --db /path/prices.db ...  # DB 明示

本番手順: daemon 停止 → 本スクリプト --yes 実行 → daemon 再起動。
読み書きするのは ohlcv テーブルのみ。orchestrator/technical_snapshots は触らない。
"""
import argparse
import sqlite3
import sys
from pathlib import Path


def _counts(c, symbols, all_):
    if all_:
        return [("(all)", c.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0])]
    return [(s, c.execute("SELECT COUNT(*) FROM ohlcv WHERE symbol=?", (s,)).fetchone()[0])
            for s in symbols]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", help="対象 symbol (例 USDJPY=X)")
    ap.add_argument("--all", action="store_true", help="全 symbol を対象")
    ap.add_argument("--yes", action="store_true", help="実削除 (未指定は dry-run)")
    ap.add_argument("--db", default="data/prices.db", help="prices.db パス")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"DB not found: {db}")
        return 1
    if not args.symbols and not args.all:
        print("symbol を指定するか --all を付けてください")
        return 1

    c = sqlite3.connect(db)
    try:
        rows = _counts(c, args.symbols, args.all)
        total = sum(n for _, n in rows)
        for name, n in rows:
            print(f"  {name}: {n} rows")
        if not args.yes:
            print(f"[dry-run] {total} rows を削除予定。実行するには --yes を付けてください。")
            return 0
        if args.all:
            c.execute("DELETE FROM ohlcv")
        else:
            for s in args.symbols:
                c.execute("DELETE FROM ohlcv WHERE symbol=?", (s,))
        c.commit()
        print(f"deleted {total} rows. 次回 technical collection で naive local 再フェッチされます。")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
