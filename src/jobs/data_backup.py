"""data/ ディレクトリの定期バックアップジョブ。

`data/` 配下を zip にまとめ、`backup/data/data-YYYY-MM-DD.zip` として保存する。
保持世代数 (retention_count) を超えた古いファイルは自動削除。

設計:
- 同日 2 回起動された場合は上書き (冪等)
- sqlite WAL モード対応: zipfile でストリーム書き込み (一時 lock 回避)
- 失敗してもデーモンは止めない (logger.error のみ)
- バックアップ自身 (output_dir) は zip 対象から除外 (再帰防止)
"""
from __future__ import annotations

import logging
import zipfile
from datetime import datetime
from pathlib import Path

from src.config import BASE_DIR, AppConfig
from src.utils.clock import local_now

logger = logging.getLogger(__name__)


def _make_zip(src_dir: Path, dest_path: Path, exclude_dirs: list[Path]) -> tuple[int, int]:
    """src_dir を再帰的に zip 化して dest_path に書き出す。

    exclude_dirs に含まれるパス (絶対パス) 以下は対象外。
    Returns: (file_count, total_bytes)
    """
    excludes = {p.resolve() for p in exclude_dirs}
    file_count = 0
    total_bytes = 0

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    # ZIP_DEFLATED + level=6 が CPU/サイズのバランスがよい
    with zipfile.ZipFile(
        dest_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
    ) as zf:
        for path in src_dir.rglob("*"):
            if not path.is_file():
                continue
            # exclude_dirs 配下はスキップ
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if any(
                str(resolved).startswith(str(ex) + ("\\" if "\\" in str(ex) else "/"))
                or resolved == ex
                for ex in excludes
            ):
                continue
            try:
                arcname = path.relative_to(src_dir.parent)
                zf.write(path, arcname)
                file_count += 1
                total_bytes += path.stat().st_size
            except (OSError, PermissionError) as e:
                logger.warning(f"[BACKUP] skipped {path}: {e}")
    return file_count, total_bytes


def _rotate(output_dir: Path, retention_count: int) -> int:
    """data-*.zip のうち古いものを削除。Returns: 削除件数。"""
    if retention_count <= 0:
        return 0
    files = sorted(
        output_dir.glob("data-*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    to_delete = files[retention_count:]
    for f in to_delete:
        try:
            f.unlink()
            logger.info(f"[BACKUP] rotated out: {f.name}")
        except OSError as e:
            logger.warning(f"[BACKUP] rotate delete failed {f}: {e}")
    return len(to_delete)


def run_data_backup(config: AppConfig) -> None:
    """schedule ライブラリから呼ばれる同期エントリポイント。

    enabled=False ならノーオペで終了。失敗してもデーモンは止まらない。
    """
    cfg = config.data_backup
    if not cfg.enabled:
        logger.debug("[BACKUP] Disabled, skipping.")
        return

    try:
        src_dir = BASE_DIR / "data"
        if not src_dir.exists():
            logger.warning(f"[BACKUP] source not found: {src_dir}")
            return

        out_dir = BASE_DIR / cfg.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        date_str = local_now(config).strftime("%Y-%m-%d")
        dest = out_dir / f"data-{date_str}.zip"

        # backup/ 配下を再帰対象から除外 (data/ と被らない想定だが念のため)
        excludes = [out_dir]

        started = datetime.now()
        files, src_bytes = _make_zip(src_dir, dest, excludes)
        zip_bytes = dest.stat().st_size if dest.exists() else 0
        elapsed = (datetime.now() - started).total_seconds()

        ratio = (zip_bytes / src_bytes * 100) if src_bytes > 0 else 0.0
        logger.info(
            f"[BACKUP] {dest.name}: {files} files, "
            f"{src_bytes/1024/1024:.1f}MB → {zip_bytes/1024/1024:.1f}MB "
            f"({ratio:.0f}%), {elapsed:.1f}s"
        )

        deleted = _rotate(out_dir, cfg.retention_count)
        if deleted:
            logger.info(f"[BACKUP] rotated {deleted} old archive(s)")
    except Exception as e:  # noqa: BLE001 - デーモン停止防止
        logger.error(f"[BACKUP] backup failed: {e}", exc_info=True)
