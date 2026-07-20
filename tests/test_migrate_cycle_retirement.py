"""migration の冪等性 + dry-run 既定テスト (spec §4)。"""
import sqlite3
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.migrate_cycle_retirement import (
    PreflightError,
    build_parser,
    delete_adaptive_params,
    drop_retired_tables,
    inspect_retired_tables,
    main,
    preflight,
    render_dry_run,
)


def _make_db(tmp_path):
    db = tmp_path / "prices.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE hold_decisions (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE trading_sessions (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE technical_snapshots (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO forecasts (id) VALUES (?)", [(1,), (2,), (3,)])
    conn.executemany("INSERT INTO hold_decisions (id) VALUES (?)", [(1,), (2,)])
    conn.execute("INSERT INTO technical_snapshots (id) VALUES (9)")
    conn.commit()
    conn.close()
    return db


def _tables(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_drops_only_retired_tables(tmp_path):
    db = _make_db(tmp_path)
    dropped = drop_retired_tables(db)
    assert dropped == ["forecasts", "hold_decisions", "trading_sessions"]
    assert _tables(db) == {"technical_snapshots"}


def test_drop_idempotent(tmp_path):
    db = _make_db(tmp_path)
    drop_retired_tables(db)
    assert drop_retired_tables(db) == []


def test_delete_adaptive_params(tmp_path):
    # 実ファイル名は adaptive_params.yaml (adaptive_params_store.py:11 _FILENAME)
    f = tmp_path / "adaptive_params.yaml"
    f.write_text("{}")
    assert delete_adaptive_params(tmp_path) is True
    assert not f.exists()
    assert delete_adaptive_params(tmp_path) is False   # 冪等


# --- dry-run 既定 (plan からの意図的逸脱: 破壊的操作の可視化) ---


def _snapshot(db):
    """テーブル集合 + 各テーブル全行を取る (副作用検知用)。"""
    conn = sqlite3.connect(db)
    try:
        names = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        return {n: conn.execute(f"SELECT * FROM {n}").fetchall() for n in names}
    finally:
        conn.close()


def test_inspect_reports_row_counts_without_dropping(tmp_path):
    db = _make_db(tmp_path)
    before = _snapshot(db)

    report = inspect_retired_tables(db)

    assert report["present"] == {
        "forecasts": 3, "hold_decisions": 2, "trading_sessions": 0}
    assert report["missing"] == []
    assert report["preserved"] == {"technical_snapshots": 1}
    assert _snapshot(db) == before   # 一切の副作用なし


def test_inspect_reports_already_migrated_tables(tmp_path):
    db = _make_db(tmp_path)
    drop_retired_tables(db)

    report = inspect_retired_tables(db)

    assert report["present"] == {}
    assert report["missing"] == ["forecasts", "hold_decisions", "trading_sessions"]


def test_inspect_uses_readonly_connection(tmp_path):
    """read-only URI 接続なので、書き込みを試みても DB は変更できない。"""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DROP TABLE forecasts")
    finally:
        conn.close()
    assert "forecasts" in _tables(db)


def test_dry_run_does_not_modify_db_or_files(tmp_path):
    db = _make_db(tmp_path)
    params = tmp_path / "adaptive_params.yaml"
    params.write_text("{}")
    before = _snapshot(db)

    render_dry_run(db_path=db, state_dir=tmp_path, rag_counts=None)

    assert _snapshot(db) == before
    assert params.exists()


def test_dry_run_output_lists_tables_with_row_counts(tmp_path):
    db = _make_db(tmp_path)
    (tmp_path / "adaptive_params.yaml").write_text("{}")

    out = render_dry_run(db_path=db, state_dir=tmp_path, rag_counts=None)

    assert "forecasts" in out and "3" in out
    assert "hold_decisions" in out and "2" in out
    assert "trading_sessions" in out
    assert "technical_snapshots" in out          # 温存テーブルの提示
    assert "adaptive_params.yaml" in out
    assert "--execute" in out                    # 実行方法の案内


def test_dry_run_output_reports_rag_counts_when_available(tmp_path):
    db = _make_db(tmp_path)
    out = render_dry_run(
        db_path=db, state_dir=tmp_path, rag_counts={"bullish": 7, "bearish": 4})
    assert "7" in out and "4" in out


def test_dry_run_output_notes_rag_deferred(tmp_path):
    """dry-run は ChromaDB を開かないので、件数が未取得である理由を明示する。"""
    db = _make_db(tmp_path)
    out = render_dry_run(db_path=db, state_dir=tmp_path, rag_counts=None)
    assert "--execute 時に判明" in out
    assert "ChromaDB" in out


def test_parser_defaults_to_dry_run():
    assert build_parser().parse_args([]).execute is False


def test_parser_execute_flag():
    assert build_parser().parse_args(["--execute"]).execute is True


# --- 外部レビュー対応: main() 経由の副作用ゼロ + preflight ---------------------
#
# これまでのテストは render_dry_run() だけを叩いており、main() が dry-run 経路で
# VectorStore を構築して ChromaDB (chroma.sqlite3) を作ってしまう乖離を見逃した。
# 以下は必ず main() を通す。


@dataclass
class _StubConfig:
    prices_db_path: Path
    state_dir: Path
    rag_db_path: Path


@pytest.fixture
def stub_config(tmp_path, monkeypatch):
    """load_config() を差し替え、main() 全体を tmp_path 内に閉じ込める。"""
    def _make(*, db=None, state_dir=None, rag=None):
        cfg = _StubConfig(
            prices_db_path=db if db is not None else _make_db(tmp_path),
            state_dir=state_dir if state_dir is not None else tmp_path,
            rag_db_path=rag if rag is not None else tmp_path / "rag",
        )
        module = types.ModuleType("src.config")
        module.load_config = lambda: cfg
        monkeypatch.setitem(sys.modules, "src.config", module)
        return cfg

    return _make


def _tree(root: Path) -> set:
    """root 配下の全エントリ (相対パス)。存在しなければ空集合。"""
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def test_main_dry_run_creates_nothing_under_rag_path(stub_config, tmp_path, capsys):
    """存在しない RAG パスに対する dry-run が、そのパスに一切書き込まないこと。

    VectorStore を構築する実装に戻すと chroma.sqlite3 が生えてここが落ちる
    (chromadb 1.5.5 の PersistentClient は path を無条件に mkdir + 作成する)。
    """
    rag = tmp_path / "never_created"
    stub_config(rag=rag)

    main([])

    assert not rag.exists(), f"dry-run が RAG パスを作成した: {_tree(rag)}"
    assert "--execute" in capsys.readouterr().out


def test_main_dry_run_does_not_touch_db_or_state(stub_config, tmp_path):
    db = _make_db(tmp_path)
    params = tmp_path / "adaptive_params.yaml"
    params.write_text("{}")
    stub_config(db=db, rag=tmp_path / "never_created")
    before = _snapshot(db)

    main([])

    assert _snapshot(db) == before
    assert params.exists()


def test_main_dry_run_explains_rag_counts_are_deferred(stub_config, tmp_path, capsys):
    """ChromaDB を開かない以上、件数不明の理由が出力から読み取れること。"""
    stub_config(rag=tmp_path / "never_created")

    main([])
    out = capsys.readouterr().out

    assert "--execute" in out
    assert "ChromaDB" in out


# --- 修正3: DB 不在で空 DB を作らない --------------------------------------


def test_main_execute_aborts_when_db_missing(stub_config, tmp_path):
    missing = tmp_path / "absent" / "prices.db"
    stub_config(db=missing)

    with pytest.raises(SystemExit) as exc:
        main(["--execute"])

    assert exc.value.code != 0
    assert not missing.exists(), "存在しない DB が新規作成された"


def test_preflight_rejects_missing_db(tmp_path):
    with pytest.raises(PreflightError, match="DB"):
        preflight(
            db_path=tmp_path / "nope.db",
            state_dir=tmp_path,
            rag_db_path=tmp_path / "rag",
            check_rag=False,
        )
    assert not (tmp_path / "nope.db").exists()


def test_preflight_rejects_db_without_preserved_tables(tmp_path):
    """温存テーブルが1つも無い = finance の prices.db ではない可能性。"""
    db = tmp_path / "other.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(PreflightError):
        preflight(db_path=db, state_dir=tmp_path,
                  rag_db_path=tmp_path / "rag", check_rag=False)


def test_preflight_accepts_valid_db(tmp_path):
    db = _make_db(tmp_path)
    preflight(db_path=db, state_dir=tmp_path,
              rag_db_path=tmp_path / "rag", check_rag=False)   # 例外なし


def test_preflight_rejects_missing_state_dir(tmp_path):
    db = _make_db(tmp_path)
    with pytest.raises(PreflightError, match="state_dir"):
        preflight(db_path=db, state_dir=tmp_path / "absent",
                  rag_db_path=tmp_path / "rag", check_rag=False)


def test_preflight_does_not_open_rag_when_check_rag_false(tmp_path):
    db = _make_db(tmp_path)
    rag = tmp_path / "never_created"

    preflight(db_path=db, state_dir=tmp_path, rag_db_path=rag, check_rag=False)

    assert not rag.exists()


# --- 修正2: RAG 不通なら破壊的操作へ進まない ---------------------------------


def test_main_execute_aborts_before_destructive_ops_when_rag_fails(
    stub_config, tmp_path, monkeypatch
):
    """RAG が開けないとき、SQLite も adaptive ファイルも変更されないこと。"""
    db = _make_db(tmp_path)
    params = tmp_path / "adaptive_params.yaml"
    params.write_text("{}")
    stub_config(db=db, rag=tmp_path / "rag")
    before = _snapshot(db)

    import scripts.migrate_cycle_retirement as mod

    def _boom(rag_db_path):
        raise RuntimeError("chroma unavailable")

    monkeypatch.setattr(mod, "open_vector_store", _boom)

    with pytest.raises(SystemExit) as exc:
        main(["--execute"])

    assert exc.value.code != 0
    assert _snapshot(db) == before, "RAG 失敗後に SQLite が変更された"
    assert params.exists(), "RAG 失敗後に adaptive_params.yaml が削除された"


# --- 外部レビュー Medium (修正1): RAG パス / collection の実在確認 --------------
#
# PersistentClient は存在しないパスを自動作成する (実測: mkdir + chroma.sqlite3 生成、
# collection は 0 件)。したがって「開けた」ことは「正しい RAG を指している」証拠に
# ならず、config のパス設定ミスがあっても preflight を通過して SQLite だけが移行され、
# 部分適用の防止という preflight の目的が成立しない。
# よって開く前にパスの実在を確認し、開いた後に既定 collection の実在も確認する。


_EXPECTED_COLLECTIONS = ("fx_reflections_bullish", "fx_reflections_bearish")


def _make_rag(path, *, collections=_EXPECTED_COLLECTIONS):
    """実際の chromadb で collection 付きの永続化先を作る。"""
    import chromadb

    client = chromadb.PersistentClient(path=str(path))
    for name in collections:
        client.get_or_create_collection(name=name)
    return path


def test_preflight_rejects_nonexistent_rag_path(tmp_path):
    """RAG パスが不在なら中止し、そのパスが作られないこと。"""
    db = _make_db(tmp_path)
    rag = tmp_path / "absent_rag"

    with pytest.raises(PreflightError, match="RAG"):
        preflight(db_path=db, state_dir=tmp_path, rag_db_path=rag, check_rag=True)

    assert not rag.exists(), f"preflight が RAG パスを作成した: {_tree(rag)}"


def test_preflight_rejects_rag_path_without_collections(tmp_path):
    """パスは在るが既定 collection が無い (= 別物を指している) なら中止すること。"""
    db = _make_db(tmp_path)
    rag = _make_rag(tmp_path / "empty_rag", collections=())

    with pytest.raises(PreflightError, match="collection"):
        preflight(db_path=db, state_dir=tmp_path, rag_db_path=rag, check_rag=True)


def test_preflight_accepts_rag_path_with_expected_collections(tmp_path):
    db = _make_db(tmp_path)
    rag = _make_rag(tmp_path / "real_rag")

    store = preflight(db_path=db, state_dir=tmp_path, rag_db_path=rag, check_rag=True)

    assert store is not None


def test_main_execute_aborts_when_rag_path_absent(stub_config, tmp_path):
    """end-to-end: RAG パス不在の --execute が DB も adaptive も RAG も触らないこと。"""
    db = _make_db(tmp_path)
    params = tmp_path / "adaptive_params.yaml"
    params.write_text("{}")
    rag = tmp_path / "absent_rag"
    stub_config(db=db, rag=rag)
    before = _snapshot(db)

    with pytest.raises(SystemExit) as exc:
        main(["--execute"])

    assert exc.value.code != 0
    assert _snapshot(db) == before, "RAG パス不在なのに SQLite が変更された"
    assert params.exists(), "RAG パス不在なのに adaptive_params.yaml が削除された"
    assert not rag.exists(), f"RAG パスが新規作成された: {_tree(rag)}"
