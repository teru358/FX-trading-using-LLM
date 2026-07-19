"""forecast/取引サイクル退役 migration (spec §4)。

Usage:
    uv run python scripts/migrate_cycle_retirement.py

前提: システム停止中に実行し、実行前に以下をバックアップ済みであること。
  - prices.db (DB)
  - data/ 配下の RAG 永続化先 (ChromaDB)
  - state_dir (adaptive_params.yaml 含む)

冪等: 再実行しても安全 (DROP IF EXISTS / where 削除 / 存在チェック)。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_RETIRED_TABLES = ["forecasts", "hold_decisions", "trading_sessions"]
_ADAPTIVE_FILENAME = "adaptive_params.yaml"   # adaptive_params_store.py:11 の実値


def drop_retired_tables(db_path) -> list[str]:
    conn = sqlite3.connect(db_path)
    dropped = []
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _RETIRED_TABLES:
            if table in existing:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
                dropped.append(table)
        conn.commit()
    finally:
        conn.close()
    return dropped


def delete_adaptive_params(state_dir) -> bool:
    f = Path(state_dir) / _ADAPTIVE_FILENAME
    if f.exists():
        f.unlink()
        return True
    return False


def main() -> None:
    from src.config import load_config
    from src.rag.vector_store import VectorStore   # migrate_directional_rag.py と同パターン

    config = load_config()
    print("== cycle retirement migration ==")
    dropped = drop_retired_tables(config.prices_db_path)
    print(f"dropped tables: {dropped or '(none — already migrated)'}")
    if delete_adaptive_params(config.state_dir):
        print("deleted adaptive_params.yaml")
    else:
        print("adaptive_params.yaml not present")
    store = VectorStore(config.rag_db_path)   # main.py:212 と同構築
    counts = store.directional.delete_retired_cards()
    print(f"deleted RAG cards: {counts}")
    print("done.")


if __name__ == "__main__":
    main()
