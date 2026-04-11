"""設定パッケージ。

旧 src/config.py を分割したもの:
  - schema.py  : dataclass 定義 (InstrumentConfig / AppConfig / ...)
  - loader.py  : YAML ロード + _merge_split_configs + load_config

このファイルは旧パスとの互換のため、すべての dataclass と
load_config / BASE_DIR / _DEFAULT_OLLAMA_MODEL を再エクスポートする。
既存の `from src.config import AppConfig, load_config` などはそのまま動く。
"""
from __future__ import annotations

from src.config.loader import load_config
from src.config.schema import (
    BASE_DIR,
    _DEFAULT_OLLAMA_MODEL,
    AnalysisConfig,
    ApiConfig,
    AppConfig,
    ChartPatternConfig,
    ClaudeConfig,
    EconomicCalendarConfig,
    FeedlyConfig,
    GeminiConfig,
    IndicatorToggleConfig,
    InstrumentConfig,
    KeywordsConfig,
    LLMConfig,
    LLMRoleConfig,
    LoggingConfig,
    NewsCollectionConfig,
    NewsSourcesConfig,
    NotifierConfig,
    OllamaBaseConfig,
    OpenAIConfig,
    PairConfig,  # 後方互換エイリアス
    PriceMonitorConfig,
    PriceProviderConfig,
    RagConfig,
    ScheduleConfig,
    TradingConfig,
    TradingViewConfig,
    TwelveDataConfig,
)

__all__ = [
    "BASE_DIR",
    "_DEFAULT_OLLAMA_MODEL",
    "load_config",
    "AnalysisConfig",
    "ApiConfig",
    "AppConfig",
    "ChartPatternConfig",
    "ClaudeConfig",
    "EconomicCalendarConfig",
    "FeedlyConfig",
    "GeminiConfig",
    "IndicatorToggleConfig",
    "InstrumentConfig",
    "KeywordsConfig",
    "LLMConfig",
    "LLMRoleConfig",
    "LoggingConfig",
    "NewsCollectionConfig",
    "NewsSourcesConfig",
    "NotifierConfig",
    "OllamaBaseConfig",
    "OpenAIConfig",
    "PairConfig",
    "PriceMonitorConfig",
    "PriceProviderConfig",
    "RagConfig",
    "ScheduleConfig",
    "TradingConfig",
    "TradingViewConfig",
    "TwelveDataConfig",
]
