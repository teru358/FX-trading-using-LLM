from __future__ import annotations
import pytest
from src.persistence.adaptive_params_store import AdaptiveParamsStore

@pytest.fixture
def store(tmp_path):
    return AdaptiveParamsStore(tmp_path, {"sl_atr_mult": 1.5, "tp_atr_mult": 3.0},
        {"sl_atr_mult_min": 0.5, "sl_atr_mult_max": 3.0, "tp_atr_mult_min": 1.0, "tp_atr_mult_max": 6.0})

def test_get_params_default(store):
    assert store.get_params("EURUSD=X") == {"sl_atr_mult": 1.5, "tp_atr_mult": 3.0}

def test_update_and_get(store):
    store.update_params("EURUSD=X", {"sl_atr_mult": 2.0, "tp_atr_mult": 3.5}, "test", "t1")
    assert store.get_params("EURUSD=X")["sl_atr_mult"] == 2.0

def test_clamp_to_limits(store):
    store.update_params("EURUSD=X", {"sl_atr_mult": 10.0, "tp_atr_mult": 0.1}, "extreme", None)
    p = store.get_params("EURUSD=X")
    assert p["sl_atr_mult"] == 2.0  # 1.5+0.5 (delta clamped), then 2.0 < 3.0 (max)
    assert p["tp_atr_mult"] == 2.5  # 3.0-0.5 (delta clamped), then 2.5 > 1.0 (min)

def test_delta_limit(store):
    store.update_params("USDJPY=X", {"sl_atr_mult": 1.5}, "init", None)
    store.update_params("USDJPY=X", {"sl_atr_mult": 3.0}, "big", None)
    assert store.get_params("USDJPY=X")["sl_atr_mult"] == 2.0

def test_history(store):
    store.update_params("EURUSD=X", {"sl_atr_mult": 1.8}, "t1", "t1")
    store.update_params("EURUSD=X", {"sl_atr_mult": 2.0}, "t2", "t2")
    assert len(store.get_history("EURUSD=X", limit=3)) == 2

def test_history_max_10(store):
    for i in range(15):
        store.update_params("EURUSD=X", {"sl_atr_mult": 1.5 + (i % 3) * 0.1}, f"r{i}", f"t{i}")
    assert len(store.get_history("EURUSD=X", limit=100)) <= 10

def test_persistence(tmp_path):
    defaults = {"sl_atr_mult": 1.5, "tp_atr_mult": 3.0}
    limits = {"sl_atr_mult_min": 0.5, "sl_atr_mult_max": 3.0, "tp_atr_mult_min": 1.0, "tp_atr_mult_max": 6.0}
    s1 = AdaptiveParamsStore(tmp_path, defaults, limits)
    s1.update_params("EURUSD=X", {"sl_atr_mult": 2.0}, "test", "t1")
    s2 = AdaptiveParamsStore(tmp_path, defaults, limits)
    assert s2.get_params("EURUSD=X")["sl_atr_mult"] == 2.0
