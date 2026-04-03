"""LLMが更新するペア別動的パラメータの管理。adaptive_params.yaml を読み書きする。"""
from __future__ import annotations
import logging, os, shutil
from datetime import datetime
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)
_MAX_DELTA = 0.5
_MAX_HISTORY = 10
_FILENAME = "adaptive_params.yaml"

class AdaptiveParamsStore:
    def __init__(self, state_dir: Path, defaults: dict, limits: dict) -> None:
        self._path = Path(state_dir) / _FILENAME
        self._defaults = dict(defaults)
        self._limits = limits
        self._data = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        data.setdefault("_schema_version", 1)
        data.setdefault("defaults", dict(self._defaults))
        data.setdefault("pairs", {})
        return data

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(self._data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            if self._path.exists():
                shutil.copy2(self._path, self._path.with_suffix(".bak"))
            os.replace(tmp, self._path)
        except Exception:
            if tmp.exists(): tmp.unlink(missing_ok=True)
            raise

    def get_params(self, pair: str) -> dict:
        pair_data = self._data["pairs"].get(pair)
        if pair_data is None:
            return dict(self._defaults)
        result = dict(self._defaults)
        for key in self._defaults:
            if key in pair_data: result[key] = pair_data[key]
        return result

    def update_params(self, pair: str, new_params: dict, reason: str, trade_id: str | None) -> None:
        current = self.get_params(pair)
        pair_data = self._data["pairs"].get(pair, {})
        updated = dict(current)
        for key, new_val in new_params.items():
            if key not in self._defaults or new_val is None: continue
            old_val = current.get(key, self._defaults.get(key, new_val))
            delta = max(-_MAX_DELTA, min(_MAX_DELTA, new_val - old_val))
            clamped_val = old_val + delta
            min_key, max_key = f"{key}_min", f"{key}_max"
            if min_key in self._limits: clamped_val = max(self._limits[min_key], clamped_val)
            if max_key in self._limits: clamped_val = min(self._limits[max_key], clamped_val)
            updated[key] = round(clamped_val, 4)

        history = pair_data.get("history", [])
        history.append({
            **{k: updated[k] for k in self._defaults},
            "updated_at": datetime.now().isoformat(), "reason": reason, "trade_id": trade_id,
        })
        if len(history) > _MAX_HISTORY: history = history[-_MAX_HISTORY:]

        self._data["pairs"][pair] = {
            **{k: updated[k] for k in self._defaults},
            "updated_at": datetime.now().isoformat(), "reason": reason, "history": history,
        }
        self._save()
        logger.info(f"[ADAPTIVE] {pair}: updated {new_params} → {updated} | reason: {reason}")

    def get_history(self, pair: str, limit: int = 3) -> list[dict]:
        return self._data["pairs"].get(pair, {}).get("history", [])[-limit:]
