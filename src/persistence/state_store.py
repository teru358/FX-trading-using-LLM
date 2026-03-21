from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _serialize(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _deserialize_datetime(d: dict) -> dict:
    dt_fields = {"opened_at", "closed_at"}
    for key in dt_fields:
        if key in d and d[key] is not None:
            d[key] = datetime.fromisoformat(d[key])
    return d


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

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
        self._atomic_write(self._positions_path(), data)
        logger.debug(f"Saved {len(open_positions)} open positions.")

    # --- Trades ---

    def load_trades_raw(self) -> list[dict]:
        data = self._safe_load(self._trades_path())
        if data is None:
            return []
        return data

    def append_trade(self, trade_dict: dict) -> None:
        trades = self.load_trades_raw()
        trades.append(trade_dict)
        self._atomic_write(self._trades_path(), trades)
        logger.debug(f"Appended trade {trade_dict.get('order_id')} to history.")
