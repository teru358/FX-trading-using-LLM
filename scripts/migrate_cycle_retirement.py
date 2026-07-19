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

preflight: 破壊的操作の前に DB 実在・温存テーブル・integrity_check・state_dir・
RAG 疎通をまとめて検証する。1つでも失敗すれば何も変更せず終了コード 1 で中止する
(SQLite だけ drop されて RAG が残る部分適用を防ぐ)。

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

# 対象 DB が本当に finance の prices.db かを判定する目印。退役対象と無関係で、
# かつ長命なテーブルを選ぶ (1つでもあれば OK)。
_SENTINEL_TABLES = {"ohlcv", "trade_plans", "technical_snapshots"}


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


class PreflightError(RuntimeError):
    """破壊的操作に入る前の検査で不備が見つかった (何も変更していない)。"""


def _check_db(db_path) -> None:
    """DB が実在し、finance の prices.db として妥当かを read-only で検証する。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        # 通常の sqlite3.connect は不在 DB を新規作成してしまい、「drop 対象なし =
        # 移行済み」と誤認させる。ここで明示的に止める。
        raise PreflightError(
            f"DB が存在しません: {db_path} "
            "(設定ミス / 未同期の可能性。空 DB は作成しません)")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
            if not r[0].startswith("sqlite_")}
        if not (existing & _SENTINEL_TABLES):
            # 「退役対象以外のテーブルがある」だけでは不十分 (無関係な DB でも通る)。
            # finance 固有の長命テーブルが1つも無ければ別の DB を指している。
            raise PreflightError(
                f"finance の prices.db ではない可能性があります: {db_path} "
                f"(既知テーブル {sorted(_SENTINEL_TABLES)} がいずれも無い。"
                f"検出テーブル={sorted(existing) or 'なし'})")
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise PreflightError(f"integrity_check 失敗: {db_path}: {result}")
    except sqlite3.DatabaseError as exc:
        raise PreflightError(f"DB を読めません: {db_path}: {exc}") from exc
    finally:
        conn.close()


def open_vector_store(rag_db_path):
    """VectorStore を構築する (ChromaDB を実際に開く)。

    **この関数を呼ぶと ChromaDB ディレクトリが作成される。** dry-run 経路からは
    決して呼ばないこと。テストが差し替える継ぎ目でもある。
    """
    from src.rag.vector_store import VectorStore   # migrate_directional_rag.py と同パターン

    return VectorStore(Path(rag_db_path))


def preflight(*, db_path, state_dir, rag_db_path, check_rag: bool):
    """破壊的操作の前に全ての前提を検証する (spec §4)。

    ここを通過するまで DB・ファイル・ChromaDB のいずれにも触れない。RAG の検証を
    最後に回すと「SQLite とファイルだけ削除された」部分適用が起きるため、
    破壊的操作より前にまとめて確認する。

    Args:
        check_rag: True なら ChromaDB を実際に開いて疎通確認する (=副作用あり)。
            dry-run では必ず False。dry-run の副作用ゼロ保証と、`--execute` の
            部分適用防止を両立させるための分岐。

    Returns:
        check_rag=True なら構築済み VectorStore、False なら None。

    Raises:
        PreflightError: 何も変更せずに中止すべき不備。
    """
    _check_db(db_path)

    if not Path(state_dir).is_dir():
        raise PreflightError(f"state_dir が存在しません: {state_dir}")

    if not check_rag:
        return None
    try:
        return open_vector_store(rag_db_path)
    except Exception as exc:
        raise PreflightError(f"RAG (ChromaDB) を開けません: {rag_db_path}: {exc}") from exc


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

    # preflight は dry-run / --execute の両方で走らせる (実行前に不備が分かる)。
    # ただし RAG 疎通確認は ChromaDB を開く = 副作用があるため --execute 時のみ。
    try:
        store = preflight(
            db_path=config.prices_db_path,
            state_dir=config.state_dir,
            rag_db_path=config.rag_db_path,
            check_rag=args.execute,
        )
    except PreflightError as exc:
        print(f"preflight 失敗 — 何も変更していません: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if not args.execute:
        # dry-run は ChromaDB を開かないので RAG 件数は取得できない (rag_counts=None)。
        print(render_dry_run(config.prices_db_path, config.state_dir, None))
        return

    # ここから破壊的操作。preflight を通っているので RAG 不通による部分適用はない。
    print("== cycle retirement migration ==")
    dropped = drop_retired_tables(config.prices_db_path)
    print(f"dropped tables: {dropped or '(none — already migrated)'}")
    if delete_adaptive_params(config.state_dir):
        print("deleted adaptive_params.yaml")
    else:
        print("adaptive_params.yaml not present")
    counts = store.directional.delete_retired_cards()
    print(f"deleted RAG cards: {counts}")
    print("done.")


if __name__ == "__main__":
    main()
