"""data_backup ジョブのユニットテスト。

zip 化・除外パターン・世代ローテーション・無効化ノーオペ・例外飲み込みを検証。
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.config.schema import DataBackupConfig, ScheduleConfig
from src.jobs.data_backup import _make_zip, _rotate, run_data_backup


# ── _make_zip ─────────────────────────────────────────────────────


def test_make_zip_includes_all_files(tmp_path: Path):
    src = tmp_path / "data"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("world")

    dest = tmp_path / "out" / "data.zip"
    files, total = _make_zip(src, dest, exclude_dirs=[])

    assert files == 2
    assert total > 0
    assert dest.exists()
    with zipfile.ZipFile(dest) as zf:
        names = sorted(zf.namelist())
        # arcname は src.parent からの相対 → "data/a.txt", "data/sub/b.txt"
        assert any("a.txt" in n for n in names)
        assert any("b.txt" in n for n in names)


def test_make_zip_excludes_directories(tmp_path: Path):
    src = tmp_path / "data"
    src.mkdir()
    (src / "keep.txt").write_text("k")
    excluded = src / "excluded"
    excluded.mkdir()
    (excluded / "skip.txt").write_text("s")

    dest = tmp_path / "out.zip"
    files, _ = _make_zip(src, dest, exclude_dirs=[excluded])

    assert files == 1
    with zipfile.ZipFile(dest) as zf:
        assert any("keep.txt" in n for n in zf.namelist())
        assert not any("skip.txt" in n for n in zf.namelist())


# ── _rotate ───────────────────────────────────────────────────────


def test_rotate_keeps_newest(tmp_path: Path):
    """retention_count を超えた古いファイルが削除される。"""
    out = tmp_path / "backup"
    out.mkdir()
    # mtime を意図的に変えながら 5 ファイル作る
    paths = []
    for i in range(5):
        p = out / f"data-2026-01-{i+1:02d}.zip"
        p.write_bytes(b"x")
        paths.append(p)

    # 古い順に mtime を設定 (i=0 が最も古い)
    import os
    base = 1_700_000_000
    for i, p in enumerate(paths):
        os.utime(p, (base + i, base + i))

    deleted = _rotate(out, retention_count=2)
    assert deleted == 3
    remaining = sorted(p.name for p in out.glob("data-*.zip"))
    # 新しい 2 つ (i=3, i=4) が残る
    assert remaining == ["data-2026-01-04.zip", "data-2026-01-05.zip"]


def test_rotate_zero_retention_is_noop(tmp_path: Path):
    out = tmp_path / "backup"
    out.mkdir()
    (out / "data-2026-01-01.zip").write_bytes(b"x")
    deleted = _rotate(out, retention_count=0)
    assert deleted == 0
    assert (out / "data-2026-01-01.zip").exists()


def test_rotate_under_threshold_keeps_all(tmp_path: Path):
    out = tmp_path / "backup"
    out.mkdir()
    for i in range(3):
        (out / f"data-2026-01-{i+1:02d}.zip").write_bytes(b"x")
    deleted = _rotate(out, retention_count=10)
    assert deleted == 0
    assert len(list(out.glob("data-*.zip"))) == 3


# ── run_data_backup (entry point) ────────────────────────────────


def _make_cfg(*, enabled: bool, output_dir: str = "backup/data",
              retention: int = 30) -> object:
    @dataclass
    class _C:
        data_backup: DataBackupConfig
        schedule: ScheduleConfig = field(
            default_factory=lambda: ScheduleConfig(timezone="Asia/Tokyo")
        )

    return _C(
        data_backup=DataBackupConfig(
            enabled=enabled,
            output_dir=output_dir,
            retention_count=retention,
        ),
    )


def test_run_data_backup_disabled_is_noop(tmp_path: Path):
    """enabled=False ならノーオペで終了 (例外なし、副作用なし)。"""
    cfg = _make_cfg(enabled=False)
    # 例外なく完了することだけ確認
    run_data_backup(cfg)


def test_run_data_backup_swallows_exceptions(tmp_path: Path, caplog,
                                              monkeypatch):
    """ジョブ内で例外が出てもデーモンを止めない (logger.error のみ)。"""
    # BASE_DIR を tmp_path に向けて、data/ が無い状態を作る (warning のみ)
    monkeypatch.setattr("src.jobs.data_backup.BASE_DIR", tmp_path)
    cfg = _make_cfg(enabled=True)
    # data/ は存在しないので "source not found" の warning が出るが例外は伝播しない
    with caplog.at_level("WARNING"):
        run_data_backup(cfg)
    assert any("BACKUP" in r.message for r in caplog.records)


def test_run_data_backup_creates_zip_and_rotates(tmp_path: Path, monkeypatch):
    """data/ → zip 化 + 古いファイル削除のフルサイクル。"""
    # BASE_DIR を tmp_path に差し替え
    monkeypatch.setattr("src.jobs.data_backup.BASE_DIR", tmp_path)

    # data/ にダミーファイルを置く
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "trades.json").write_text('{"trades": []}')
    (data_dir / "rag").mkdir()
    (data_dir / "rag" / "chroma.sqlite3").write_bytes(b"\x00" * 1024)

    # 既存の古いバックアップを 4 つ用意 (retention=2 なので 3 つ削除される想定)
    out = tmp_path / "backup" / "data"
    out.mkdir(parents=True)
    import os
    base = 1_700_000_000
    for i in range(4):
        p = out / f"data-2026-01-{i+1:02d}.zip"
        p.write_bytes(b"x")
        os.utime(p, (base + i, base + i))

    cfg = _make_cfg(enabled=True, output_dir="backup/data", retention=2)
    run_data_backup(cfg)

    # 今日の zip が作られている
    todays = list(out.glob("data-*.zip"))
    assert any(p.stat().st_size > 0 for p in todays)
    # retention=2 なので最新 2 つだけ残る (今日のものを含む)
    assert len(list(out.glob("data-*.zip"))) == 2
