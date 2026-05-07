"""新 mode/provider 構造のローダーテスト。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config.loader import ConfigError, load_config


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _minimal_settings(extra: str = "") -> str:
    """LLM 必須項目のみ埋めた最小 settings.yaml。"""
    return f"""
mode: paper
paper_provider: yfinance
live_broker: null

llm:
  provider: ollama
  provider_config:
    base_url: "http://localhost:11434"
  news_analysis: {{model: "n", temperature: 0.3}}
  price_analysis: {{model: "p", temperature: 0.1}}
  reflection: {{model: "r", temperature: 0.3}}

rag:
  embedding_provider: ollama
  embedding_base_url: "http://localhost:11434"
{extra}
"""


def test_load_config_paper_yfinance_no_provider_files(tmp_path):
    """mode=paper + paper_provider=yfinance なら provider yaml 不要で起動できる。"""
    _write(tmp_path / "settings.yaml", _minimal_settings())
    cfg = load_config(tmp_path / "settings.yaml")
    assert cfg.mode == "paper"
    assert cfg.paper_provider == "yfinance"
    assert cfg.live_broker is None
    assert cfg.providers.twelvedata is None
    assert cfg.providers.mt5 is None


def test_load_config_paper_twelvedata_requires_provider_yaml(tmp_path):
    """paper_provider=twelvedata で providers/twelvedata.yaml が無いと ConfigError。"""
    _write(tmp_path / "settings.yaml", _minimal_settings("""
mode: paper
paper_provider: twelvedata
live_broker: null
"""))
    with pytest.raises(ConfigError, match="providers/twelvedata.yaml not found"):
        load_config(tmp_path / "settings.yaml")


def test_load_config_paper_twelvedata_loads_provider_yaml(tmp_path):
    """providers/twelvedata.yaml がロードされ、indices が反映される。"""
    _write(tmp_path / "settings.yaml", _minimal_settings("""
mode: paper
paper_provider: twelvedata
live_broker: null
"""))
    _write(tmp_path / "providers" / "twelvedata.yaml", """
daily_limit: 1000
per_minute_limit: 10
use_for_monitor: false
indices:
  - GLD
  - SPY
""")
    cfg = load_config(tmp_path / "settings.yaml")
    assert cfg.providers.twelvedata is not None
    assert cfg.providers.twelvedata.daily_limit == 1000
    assert cfg.providers.twelvedata.per_minute_limit == 10
    assert cfg.providers.twelvedata.use_for_monitor is False
    assert cfg.providers.twelvedata.indices == ["GLD", "SPY"]


def test_load_config_live_mt5_requires_provider_yaml(tmp_path):
    """mode=live + live_broker=mt5 で providers/mt5.yaml が無いと ConfigError。"""
    _write(tmp_path / "settings.yaml", _minimal_settings("""
mode: live
paper_provider: yfinance
live_broker: mt5
"""))
    with pytest.raises(ConfigError, match="providers/mt5.yaml not found"):
        load_config(tmp_path / "settings.yaml")


def test_load_config_live_mt5_loads_provider_yaml(tmp_path):
    """providers/mt5.yaml がロードされ、bridge_url が反映される。"""
    _write(tmp_path / "settings.yaml", _minimal_settings("""
mode: live
paper_provider: yfinance
live_broker: mt5
"""))
    _write(tmp_path / "providers" / "mt5.yaml", """
bridge_url: "http://localhost:8812"
api_key: "secret"
health_retry_after_sec: 90.0
""")
    cfg = load_config(tmp_path / "settings.yaml")
    assert cfg.mode == "live"
    assert cfg.live_broker == "mt5"
    assert cfg.providers.mt5 is not None
    assert cfg.providers.mt5.bridge_url == "http://localhost:8812"
    assert cfg.providers.mt5.api_key == "secret"
    assert cfg.providers.mt5.health_retry_after_sec == 90.0


def test_load_config_mt5_empty_bridge_url_rejected(tmp_path):
    """providers/mt5.yaml の bridge_url が空文字なら ConfigError。"""
    _write(tmp_path / "settings.yaml", _minimal_settings("""
mode: live
paper_provider: yfinance
live_broker: mt5
"""))
    _write(tmp_path / "providers" / "mt5.yaml", """
bridge_url: ""
api_key: "secret"
""")
    with pytest.raises(ConfigError, match="bridge_url is required"):
        load_config(tmp_path / "settings.yaml")


def test_load_config_live_test_mt5_works(tmp_path):
    """mode=live_test + live_broker=mt5 が正常にロードされる。"""
    _write(tmp_path / "settings.yaml", _minimal_settings("""
mode: live_test
paper_provider: yfinance
live_broker: mt5
"""))
    _write(tmp_path / "providers" / "mt5.yaml", """
bridge_url: "http://localhost:8812"
""")
    cfg = load_config(tmp_path / "settings.yaml")
    assert cfg.mode == "live_test"
    assert cfg.live_broker == "mt5"


def test_load_config_live_without_broker_fails(tmp_path):
    """mode=live で live_broker=null は ConfigError。"""
    _write(tmp_path / "settings.yaml", _minimal_settings("""
mode: live
paper_provider: yfinance
live_broker: null
"""))
    with pytest.raises(ConfigError, match="mode='live' requires live_broker"):
        load_config(tmp_path / "settings.yaml")


def test_load_config_live_test_oanda_rejected(tmp_path):
    """mode=live_test + live_broker=oanda は ConfigError。"""
    _write(tmp_path / "settings.yaml", _minimal_settings("""
mode: live_test
paper_provider: yfinance
live_broker: oanda
"""))
    with pytest.raises(ConfigError, match="not supported with live_broker='oanda'"):
        load_config(tmp_path / "settings.yaml")


def test_load_config_invalid_mode(tmp_path):
    """未定義の mode 値は ConfigError。"""
    _write(tmp_path / "settings.yaml", _minimal_settings("""
mode: signal
"""))
    with pytest.raises(ConfigError, match="mode='signal' is invalid"):
        load_config(tmp_path / "settings.yaml")
