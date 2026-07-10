"""main.py の API 起動配線の単体テスト。

main() は monolithic なので、API 用 OrchestratorStore の生成可否だけを
`_api_orchestrator_store` ヘルパーに抽出してテストする (実装後レビュー Low-Med:
orchestrator 無効時に不要な DB を作らない)。
"""
from __future__ import annotations

from types import SimpleNamespace

from main import _api_orchestrator_store
from src.data.orchestrator_store import OrchestratorStore


def _make_config(*, orchestrator_enabled: bool, prices_db_path):
    return SimpleNamespace(
        orchestrator=SimpleNamespace(enabled=orchestrator_enabled),
        prices_db_path=prices_db_path,
    )


def test_api_orchestrator_store_none_when_orchestrator_disabled(tmp_path):
    """orchestrator.enabled=False なら None を返し、DB ファイルも作らない。"""
    db_path = tmp_path / "prices.db"
    config = _make_config(orchestrator_enabled=False, prices_db_path=db_path)

    result = _api_orchestrator_store(config)

    assert result is None
    assert not db_path.exists()


def test_api_orchestrator_store_created_when_orchestrator_enabled(tmp_path):
    """orchestrator.enabled=True なら OrchestratorStore インスタンスを返す。"""
    db_path = tmp_path / "prices.db"
    config = _make_config(orchestrator_enabled=True, prices_db_path=db_path)

    result = _api_orchestrator_store(config)

    assert isinstance(result, OrchestratorStore)
