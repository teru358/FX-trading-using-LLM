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
