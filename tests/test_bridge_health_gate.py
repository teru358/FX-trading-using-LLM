"""BridgeHealthGate のユニットテスト。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from src.trading.bridge_health_gate import BridgeHealthGate, ProbeResult


def _make_config(*, mode="paper", live_broker=None, has_mt5=False, has_oanda=False):
    cfg = MagicMock()
    cfg.mode = mode
    cfg.live_broker = live_broker
    cfg.providers.mt5 = MagicMock(
        bridge_url="http://x:8812", api_key="",
        request_timeout_seconds=5.0,
    ) if has_mt5 else None
    cfg.providers.oanda = MagicMock() if has_oanda else None
    cfg.state_dir = Path("data/state")
    return cfg


def test_paper_mode_always_ok(tmp_path):
    cfg = _make_config(mode="paper")
    gate = BridgeHealthGate(
        config=cfg, log_path=tmp_path / "bridge_health.jsonl",
        sleep_fn=lambda _: None,
    )
    result = gate.probe(caller="tech", sync_balance=True)
    assert result.ok is True
    assert result.retried is False
