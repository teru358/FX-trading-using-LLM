from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# state_dir パスごとの書き込みロック。同一プロセス内で複数の StateStore インスタンスが
# 作られても、同じディレクトリを指すものは同じロックを共有し、read-modify-write の
# レースを防ぐ。
_STATE_DIR_LOCKS: dict[Path, threading.RLock] = {}
_STATE_DIR_LOCKS_GUARD = threading.Lock()


def _get_state_lock(state_dir: Path) -> threading.RLock:
    key = state_dir.resolve()
    with _STATE_DIR_LOCKS_GUARD:
        lock = _STATE_DIR_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STATE_DIR_LOCKS[key] = lock
        return lock


def _serialize(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # 同じ state_dir を指す全インスタンスで共有されるロック
        self._lock = _get_state_lock(state_dir)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """state_dir 単位で排他ロックを取得する context manager。

        read → modify → write の複数ステップを原子的に実行したい呼び出し側で
        使用する (例: PositionManager.close_position)。RLock なのでネストしても
        デッドロックしない。
        """
        with self._lock:
            yield

    def _positions_path(self) -> Path:
        return self.state_dir / "positions.json"

    def _trades_path(self) -> Path:
        return self.state_dir / "trades.json"

    def _atomic_write(self, path: Path, data: Any) -> None:
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, default=_serialize, ensure_ascii=False, indent=2)
            # Backup existing file
            if path.exists():
                shutil.copy2(path, path.with_suffix(".bak"))
            os.replace(tmp, path)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise

    def _safe_load(self, path: Path) -> Any:
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load {path}: {e}. Trying backup.")
            bak = path.with_suffix(".bak")
            if bak.exists():
                try:
                    with open(bak, encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e2:
                    logger.critical(f"Backup also failed: {e2}")
            return None

    # --- Positions ---

    def load_positions_raw(self) -> dict:
        data = self._safe_load(self._positions_path())
        if data is None:
            return {"account_balance": None, "open_positions": []}
        return data

    def save_positions(self, account_balance: float, open_positions: list[dict]) -> None:
        data = {
            "account_balance": account_balance,
            "last_updated": datetime.now().isoformat(),
            "open_positions": open_positions,
        }
        with self._lock:
            self._atomic_write(self._positions_path(), data)
        logger.debug(f"Saved {len(open_positions)} open positions.")

    # --- Trades ---

    def load_trades_raw(self) -> list[dict]:
        data = self._safe_load(self._trades_path())
        if data is None:
            return []
        return data

    def append_trade(self, trade_dict: dict) -> None:
        with self._lock:
            trades = self.load_trades_raw()
            trades.append(trade_dict)
            self._atomic_write(self._trades_path(), trades)
        logger.debug(f"Appended trade {trade_dict.get('order_id')} to history.")
