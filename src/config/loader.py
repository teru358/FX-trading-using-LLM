"""設定ローダー。

YAML ファイル (+ 分割 yaml) を読み込み、schema.py の dataclass に
組み立てる。公開は `src.config.load_config` 経由で行う。

_from_dict ヘルパーで dataclass のフィールド定義からデフォルト値を自動適用し、
手書きの `.get("field", default)` ボイラープレートを排除する。
YAML キー ≠ フィールド名のケース (KeywordsConfig 等) やネスト構造の
特殊ケースは明示的に手動構築する。
"""
from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.config.schema import (
    AnalysisConfig,
    ApiConfig,
    AppConfig,
    BASE_DIR,
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
    MultiTimeframeConfig,
    NewsCollectionConfig,
    NewsSourcesConfig,
    NotifierConfig,
    OllamaBaseConfig,
    OpenAIConfig,
    ForecastAccuracyFeedbackConfig,
    PriceMonitorConfig,
    PriceProviderConfig,
    RagConfig,
    ScheduleConfig,
    TimeframeConfig,
    TradingConfig,
    TradingViewConfig,
    TwelveDataConfig,
)


# ── 汎用ヘルパー ────────────────────────────────────────────────

def _from_dict(cls, data: dict):
    """dataclass の field 定義から dict → instance を自動構築する。

    YAML dict のキーが dataclass フィールド名と 1:1 対応する
    フラットな config に適用する。存在しないキーは dataclass 側の
    default / default_factory で補完される。

    ネスト dataclass や YAML キー ≠ フィールド名のケースでは使えない。
    """
    kwargs = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = data[f.name]
    return cls(**kwargs)


def _merge_split_configs(base: dict, config_dir: Path) -> dict:
    """分割設定ファイルをメイン設定にマージする。"""
    for fname in ("instruments.yaml", "news_sources.yaml"):
        fpath = config_dir / fname
        if fpath.exists():
            with open(fpath, encoding="utf-8") as f:
                extra = yaml.safe_load(f)
            if extra and isinstance(extra, dict):
                for key, value in extra.items():
                    base[key] = value
    return base


# ── load_config ──────────────────────────────────────────────────

def load_config(config_path: Path | None = None) -> AppConfig:
    load_dotenv(BASE_DIR / ".env")

    if config_path is None:
        config_path = BASE_DIR / "config" / "settings.yaml"

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = _merge_split_configs(raw, config_path.parent)

    # ── フラット configs (_from_dict で自動構築) ──────────────────

    trading_raw = raw.get("trading", {}) or {}
    # ネスト dataclass: forecast_accuracy_feedback (TradingConfig 配下)
    fa_raw = trading_raw.pop("forecast_accuracy_feedback", None)
    trading = _from_dict(TradingConfig, trading_raw)
    if isinstance(fa_raw, dict):
        trading.forecast_accuracy_feedback = _from_dict(
            ForecastAccuracyFeedbackConfig, fa_raw
        )
    schedule = _from_dict(ScheduleConfig, raw.get("schedule", {}))
    news_collection = _from_dict(NewsCollectionConfig, raw.get("news_collection", {}))
    rag = _from_dict(RagConfig, raw.get("rag", {}))
    notifier = _from_dict(NotifierConfig, raw.get("notification", {}))
    api_cfg = _from_dict(ApiConfig, raw.get("api", {}))
    price_monitor = _from_dict(PriceMonitorConfig, raw.get("price_monitor", {}))
    gemini = _from_dict(GeminiConfig, raw.get("gemini", {}))
    openai_cfg = _from_dict(OpenAIConfig, raw.get("openai", {}))
    claude_cfg = _from_dict(ClaudeConfig, raw.get("claude", {}))
    tradingview_cfg = _from_dict(TradingViewConfig, raw.get("tradingview", {}))
    economic_calendar_cfg = _from_dict(EconomicCalendarConfig, raw.get("economic_calendar", {}))

    # ── 特殊ケース: YAML キー ≠ フィールド名 ─────────────────────

    lg = raw.get("logging", {})
    log_cfg = LoggingConfig(
        level=lg.get("level", "INFO"),
        file=lg.get("file", "logs/finance.log"),
        activity_log_file=lg.get("activity_log_file", "logs/activity.log"),
        rotate_timing=str(lg.get("rotate_timing") or lg.get("max_bytes", "10MB")),
        backup_count=lg.get("backup_count", 5),
    )

    kw = raw.get("keywords", {})
    _default_kw = KeywordsConfig()
    keywords_cfg = KeywordsConfig(
        global_keywords=kw.get("global", _default_kw.global_keywords),
        japan_keywords=kw.get("japan", _default_kw.japan_keywords),
    )

    # ── Instruments (リスト + フィールド変換) ─────────────────────

    instruments = [
        InstrumentConfig(
            symbol=p["symbol"],
            display_name=p["display_name"],
            asset_type=(at := p.get("asset_type", "fx")),
            mode=p.get("mode", "trade" if at == "fx" else "watch"),
            enabled=p.get("enabled", True),
            pip_value=p.get("pip_value", 0.0),
            base_currency=p.get("base_currency", ""),
            quote_currency=p.get("quote_currency", ""),
            currency=p.get("currency", ""),
        )
        for p in raw.get("instruments", raw.get("pairs", []))
    ]

    # ── ネスト configs ────────────────────────────────────────────

    # LLM (ollama + 3 roles)
    lc = raw.get("llm", {})
    ollama_base = _from_dict(OllamaBaseConfig, lc.get("ollama", {}))

    def _role(raw_role: dict, default_temp: float) -> LLMRoleConfig:
        return LLMRoleConfig(
            provider=raw_role.get("provider", "ollama"),
            model=raw_role.get("model", ""),
            temperature=raw_role.get("temperature", default_temp),
        )

    llm_cfg = LLMConfig(
        ollama=ollama_base,
        news_analysis=_role(lc.get("news_analysis", {}), 0.3),
        price_analysis=_role(lc.get("price_analysis", {}), 0.1),
        reflection=_role(lc.get("reflection", {}), 0.3),
    )

    # NewsSourcesConfig (feeds + feedly with env token)
    ns = raw.get("news_sources", {})
    fd = ns.get("feedly", {})
    feedly_cfg = FeedlyConfig(
        enabled=fd.get("enabled", False),
        access_token=os.environ.get("FEEDLY_ACCESS_TOKEN", ""),
        streams_fx=fd.get("streams_fx", []),
        streams_global=fd.get("streams_global", []),
        streams_japan=fd.get("streams_japan", []),
        count=fd.get("count", 20),
    )
    _default_ns = NewsSourcesConfig()
    news_sources = NewsSourcesConfig(
        feeds_fx=ns.get("feeds_fx", _default_ns.feeds_fx),
        feeds_global=ns.get("feeds_global", _default_ns.feeds_global),
        feeds_japan=ns.get("feeds_japan", _default_ns.feeds_japan),
        feedly=feedly_cfg,
    )

    # PriceProviderConfig (nested TwelveDataConfig)
    pp = raw.get("price_provider", {})
    price_provider = PriceProviderConfig(
        realtime_provider=pp.get("realtime_provider", "yfinance"),
        twelvedata=_from_dict(TwelveDataConfig, pp.get("twelvedata", {})),
    )

    # AnalysisConfig (nested indicators / chart_patterns / multi_timeframe)
    an = raw.get("analysis", {})
    indicator_cfg = _from_dict(IndicatorToggleConfig, an.get("indicators", {}))
    pattern_cfg = _from_dict(ChartPatternConfig, an.get("chart_patterns", {}))

    mtf = an.get("multi_timeframe", {}) or {}
    _default_mtf = MultiTimeframeConfig()

    def _parse_tf(node: dict, default: TimeframeConfig) -> TimeframeConfig:
        return TimeframeConfig(
            lookback_days=node.get("lookback_days", default.lookback_days),
            interval=node.get("interval", default.interval),
            enabled=node.get("enabled", default.enabled),
        )

    mtf_cfg = MultiTimeframeConfig(
        enabled=mtf.get("enabled", _default_mtf.enabled),
        long=_parse_tf(mtf.get("long", {}) or {}, _default_mtf.long),
        medium=_parse_tf(mtf.get("medium", {}) or {}, _default_mtf.medium),
        short=_parse_tf(mtf.get("short", {}) or {}, _default_mtf.short),
        weights=dict(mtf.get("weights", _default_mtf.weights)),
    )

    analysis_cfg = AnalysisConfig(
        indicators=indicator_cfg,
        chart_patterns=pattern_cfg,
        multi_timeframe=mtf_cfg,
        forecast_review_interval_hours=an.get("forecast_review_interval_hours", 8),
        forecast_start_hour=an.get("forecast_start_hour", 0),
        forecast_min_combined_score=an.get("forecast_min_combined_score", 0.15),
        forecast_significance_atr_ratio=an.get("forecast_significance_atr_ratio", 0.30),
    )

    # ── バリデーション ────────────────────────────────────────────

    if abs(trading.news_weight + trading.price_weight - 1.0) > 1e-6:
        raise ValueError(
            f"news_weight + price_weight must equal 1.0, "
            f"got {trading.news_weight + trading.price_weight}"
        )

    if price_monitor.trailing_stop_enabled:
        if not (0.0 < price_monitor.trailing_stop_breakeven_pct < price_monitor.trailing_stop_activation_pct):
            raise ValueError(
                "trailing_stop_breakeven_pct must satisfy "
                "0 < breakeven_pct < activation_pct "
                f"(breakeven={price_monitor.trailing_stop_breakeven_pct}, "
                f"activation={price_monitor.trailing_stop_activation_pct})"
            )

    # ── AppConfig 組み立て ────────────────────────────────────────

    return AppConfig(
        trading=trading,
        instruments=instruments,
        schedule=schedule,
        logging=log_cfg,
        news_collection=news_collection,
        news_sources=news_sources,
        rag=rag,
        notifier=notifier,
        price_monitor=price_monitor,
        price_provider=price_provider,
        api=api_cfg,
        llm=llm_cfg,
        gemini=gemini,
        openai=openai_cfg,
        claude=claude_cfg,
        analysis=analysis_cfg,
        keywords=keywords_cfg,
        economic_calendar=economic_calendar_cfg,
        tradingview=tradingview_cfg,
    )
