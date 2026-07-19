"""forecast/取引サイクル退役 migration (spec §4)。

Usage:
    uv run python scripts/migrate_cycle_retirement.py            # dry-run (既定)
    uv run python scripts/migrate_cycle_retirement.py --execute  # 実際に実行

既定は **dry-run**。何が失われるかを表示するだけで、DB・ファイル・ChromaDB を
一切変更しない。`--execute` を明示したときのみ破壊的操作を行う。

dry-run の副作用ゼロ保証:
  - DB は read-only URI (`file:...?mode=ro`) でのみ開く
  - ChromaDB は **開かない**。chromadb 1.5.5 の `PersistentClient` は指定 path を
    無条件に mkdir し `chroma.sqlite3` を作成するため、「読むだけ」の構築手段が
    存在しない (`get_collection` を使っても client 構築時点で作られる)。
    よって RAG 退役カード件数は dry-run では取得せず、`--execute` 時に判明する。

前提: システム停止中に実行し、実行前に以下をバックアップ済みであること。
  - prices.db (DB)
  - data/ 配下の RAG 永続化先 (ChromaDB)
  - state_dir (adaptive_params.yaml 含む)

冪等: 再実行しても安全 (DROP IF EXISTS / where 削除 / 存在チェック)。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_RETIRED_TABLES = ["forecasts", "hold_decisions", "trading_sessions"]
_ADAPTIVE_FILENAME = "adaptive_params.yaml"   # adaptive_params_store.py:11 の実値


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="forecast/取引サイクル退役 migration (既定は dry-run)")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="実際に drop/削除を実行する (未指定なら dry-run)",
    )
    return parser


def inspect_retired_tables(db_path) -> dict:
    """drop 対象・温存対象の状況を **読み取り専用** で調べる。

    read-only URI (`file:...?mode=ro`) で接続するため、実装ミスがあっても
    dry-run 経路から DB を変更することは原理的に不可能。

    Returns:
        {"present": {table: 行数}, "missing": [drop 済 table], "preserved": {table: 行数}}
    """
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        present, missing = {}, []
        for table in _RETIRED_TABLES:
            if table in existing:
                present[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            else:
                missing.append(table)
        preserved = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in sorted(existing - set(_RETIRED_TABLES))
            if not t.startswith("sqlite_")
        }
        return {"present": present, "missing": missing, "preserved": preserved}
    finally:
        conn.close()


def count_retired_cards(store) -> dict[str, int]:
    """RAG 退役カードの削除**予定**件数を数える (削除はしない)。

    `delete_retired_cards` と同じ where 条件で `get` するだけ。
    """
    from src.rag.directional_store import RETIRED_CARDS_WHERE

    return {
        d: len(store.directional._collection(d).get(
            where=RETIRED_CARDS_WHERE, include=[])["ids"])
        for d in ("bullish", "bearish")
    }


def render_dry_run(db_path, state_dir, rag_counts: dict[str, int] | None) -> str:
    """dry-run のレポート文字列を組み立てる (副作用なし)。"""
    report = inspect_retired_tables(db_path)
    lines = ["== cycle retirement migration (DRY-RUN — 何も変更しません) =="]

    lines.append("")
    lines.append("[drop 対象テーブル]")
    if report["present"]:
        for table, rows in report["present"].items():
            lines.append(f"  - {table}: {rows} 行が失われます")
    else:
        lines.append("  (なし — 移行済み)")
    for table in report["missing"]:
        lines.append(f"  - {table}: なし (移行済み)")

    lines.append("")
    preserved = report["preserved"]
    lines.append(f"[温存テーブル] {len(preserved)} テーブルは無傷")
    for table, rows in preserved.items():
        lines.append(f"  - {table}: {rows} 行")

    lines.append("")
    lines.append("[adaptive_params.yaml]")
    if (Path(state_dir) / _ADAPTIVE_FILENAME).exists():
        lines.append(f"  - {Path(state_dir) / _ADAPTIVE_FILENAME} を削除します")
    else:
        lines.append("  (なし — 移行済み or 未生成)")

    lines.append("")
    lines.append("[RAG 退役カード]")
    if rag_counts is None:
        # dry-run では ChromaDB を開かない (副作用ゼロ保証)。chromadb 1.5.5 の
        # PersistentClient は path を無条件に mkdir し chroma.sqlite3 を作るため、
        # 「読むだけ」の構築手段が存在しない (get_collection でも client 構築時点で作られる)。
        lines.append("  - 件数は --execute 時に判明します "
                     "(dry-run では ChromaDB を開かないため未取得)")
    else:
        total = sum(rag_counts.values())
        detail = ", ".join(f"{d}={n}" for d, n in rag_counts.items())
        lines.append(f"  - 削除予定 {total} 件 ({detail})")

    lines.append("")
    lines.append("実行するには `--execute` を付けてください。")
    lines.append("(バックアップは実行者の責任です — DB / data/ / state_dir)")
    return "\n".join(lines)


def open_vector_store(rag_db_path):
    """VectorStore を構築する (ChromaDB を実際に開く)。

    **この関数を呼ぶと ChromaDB ディレクトリが作成される。** dry-run 経路からは
    決して呼ばないこと。テストが差し替える継ぎ目でもある。
    """
    from src.rag.vector_store import VectorStore   # migrate_directional_rag.py と同パターン

    return VectorStore(Path(rag_db_path))


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


def main(argv=None) -> None:
    from src.config import load_config

    args = build_parser().parse_args(argv)
    config = load_config()

    if not args.execute:
        # dry-run は ChromaDB を開かないので RAG 件数は取得できない (rag_counts=None)。
        print(render_dry_run(config.prices_db_path, config.state_dir, None))
        return

    print("== cycle retirement migration ==")
    dropped = drop_retired_tables(config.prices_db_path)
    print(f"dropped tables: {dropped or '(none — already migrated)'}")
    if delete_adaptive_params(config.state_dir):
        print("deleted adaptive_params.yaml")
    else:
        print("adaptive_params.yaml not present")
    store = open_vector_store(config.rag_db_path)
    counts = store.directional.delete_retired_cards()
    print(f"deleted RAG cards: {counts}")
    print("done.")


if __name__ == "__main__":
    main()
