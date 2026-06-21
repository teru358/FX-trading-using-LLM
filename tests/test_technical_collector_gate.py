"""technical_collector の gate 注入テスト。"""
from unittest.mock import MagicMock, patch

from src.jobs.technical_collector import run_technical_collection


def test_run_technical_collection_calls_gate_probe(tmp_path):
    """tech cycle 冒頭で gate.probe(caller='tech', sync_balance=True) を呼ぶ。"""
    config = MagicMock()
    store = MagicMock()
    price_store = MagicMock()
    analysis_store = MagicMock()
    gate = MagicMock()
    gate.probe.return_value = MagicMock(ok=True)

    with patch("src.jobs.technical_collector.collect_all_technical") as mock_collect:
        mock_collect.return_value = None
        run_technical_collection(
            config, store, price_store, analysis_store,
            price_provider=MagicMock(), gate=gate,
        )
    gate.probe.assert_called_once_with(caller="tech", sync_balance=True)


def test_run_technical_collection_no_gate_skips_probe(tmp_path):
    """gate=None なら probe は呼ばれない。"""
    config = MagicMock()

    with patch("src.jobs.technical_collector.collect_all_technical") as mock_collect:
        mock_collect.return_value = None
        # gate 省略 — 例外なく完了
        run_technical_collection(
            config, MagicMock(), MagicMock(), MagicMock(),
            price_provider=MagicMock(),
        )


def test_run_trade_technical_probes_gate(monkeypatch):
    """run_trade_technical_collection は gate.probe を呼ぶ (balance 同期は trade 文脈)。"""
    import src.jobs.technical_collector as tc
    from unittest.mock import MagicMock

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(tc, "collect_trade_technical", _noop)

    gate = MagicMock()
    tc.run_trade_technical_collection(
        config=MagicMock(), store=MagicMock(), price_store=MagicMock(),
        analysis_store=MagicMock(), force=True, gate=gate,
    )
    gate.probe.assert_called_once_with(caller="tech", sync_balance=True)


def test_run_watch_technical_does_not_probe_gate(monkeypatch):
    """run_watch_technical_collection は gate.probe を呼ばない (発注非関与)。"""
    import src.jobs.technical_collector as tc
    from unittest.mock import MagicMock

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(tc, "collect_watch_technical", _noop)

    gate = MagicMock()
    tc.run_watch_technical_collection(
        config=MagicMock(), store=MagicMock(), price_store=MagicMock(),
        analysis_store=MagicMock(), force=True, gate=gate,
    )
    gate.probe.assert_not_called()
