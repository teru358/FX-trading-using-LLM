"""設定パッケージ。

旧 src/config.py を分割したもの:
  - schema.py  : dataclass 定義 (InstrumentConfig / AppConfig / ...)
  - loader.py  : YAML ロード + _merge_split_configs + load_config

このファイルは旧パスとの互換のため、dataclass と load_config / BASE_DIR を再エクスポートする。
既存の `from src.config import AppConfig, load_config` などはそのまま動く。
"""
from __future__ import annotations

from src.config.loader import ConfigError, load_config
from src.config.schema import (
    BASE_DIR,
    LLM_PROVIDERS,
    LLM_PROVIDERS_REQUIRING_BASE_URL,
    AnalysisConfig,
    ApiConfig,
    AppConfig,
    ChartPatternConfig,
    EconomicCalendarConfig,
    FeedlyConfig,
    IndicatorToggleConfig,
    InstrumentConfig,
    KeywordsConfig,
    LLMConfig,
    LLMRoleConfig,
    LoggingConfig,
    Mt5Config,
    MultiTimeframeConfig,
    NewsCollectionConfig,
    NewsSourcesConfig,
    NotifierConfig,
    OandaConfig,
    PairConfig,  # 後方互換エイリアス
    PriceMonitorConfig,
    ProviderConfig,
    ProvidersConfig,
    RagConfig,
    ScheduleConfig,
    TimeframeConfig,
    TradingConfig,
    TwelveDataConfig,
)

__all__ = [
    "BASE_DIR",
    "LLM_PROVIDERS",
    "LLM_PROVIDERS_REQUIRING_BASE_URL",
    "ConfigError",
    "load_config",
    "AnalysisConfig",
    "ApiConfig",
    "AppConfig",
    "ChartPatternConfig",
    "EconomicCalendarConfig",
    "FeedlyConfig",
    "IndicatorToggleConfig",
    "InstrumentConfig",
    "KeywordsConfig",
    "LLMConfig",
    "LLMRoleConfig",
    "LoggingConfig",
    "Mt5Config",
    "MultiTimeframeConfig",
    "NewsCollectionConfig",
    "NewsSourcesConfig",
    "NotifierConfig",
    "OandaConfig",
    "PairConfig",
    "PriceMonitorConfig",
    "ProviderConfig",
    "ProvidersConfig",
    "RagConfig",
    "ScheduleConfig",
    "TimeframeConfig",
    "TradingConfig",
    "TwelveDataConfig",
]
