"""設定ローダー。

YAML ファイル (+ 分割 yaml) を読み込み、schema.py の dataclass に
組み立てる。公開は `src.config.load_config` 経由で行う。
"""
from __future__ import annotations

import os
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
    NewsCollectionConfig,
    NewsSourcesConfig,
    NotifierConfig,
    OllamaBaseConfig,
    OpenAIConfig,
    PriceMonitorConfig,
    PriceProviderConfig,
    RagConfig,
    ScheduleConfig,
    TradingConfig,
    TradingViewConfig,
    TwelveDataConfig,
)

_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3}


def _parse_bytes(value: int | str) -> int:
    """'10MB' / '512kb' などの文字列、または整数をバイト数に変換する。"""
    if isinstance(value, int):
        return value
    s = str(value).strip()
    for suffix, mult in sorted(_SIZE_UNITS.items(), key=lambda x: -len(x[0])):
        if s.lower().endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(s)


def _merge_split_configs(base: dict, config_dir: Path) -> dict:
    """分割設定ファイルをメイン設定にマージする。

    分割ファイルが存在すれば読み込み、同一キーは分割ファイル側が優先。
    存在しなければスキップ（schema.py のデフォルト値が使われる）。
    """
    split_files = ["instruments.yaml", "news_sources.yaml"]
    for fname in split_files:
        fpath = config_dir / fname
        if fpath.exists():
            with open(fpath, encoding="utf-8") as f:
                extra = yaml.safe_load(f)
            if extra and isinstance(extra, dict):
                for key, value in extra.items():
                    base[key] = value
    return base


def load_config(config_path: Path | None = None) -> AppConfig:
    load_dotenv(BASE_DIR / ".env")

    if config_path is None:
        config_path = BASE_DIR / "config" / "settings.yaml"

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # 分割設定ファイルをマージ（分割ファイル優先）
    raw = _merge_split_configs(raw, config_path.parent)

    t = raw.get("trading", {})
    trading = TradingConfig(
        initial_balance=t.get("initial_balance", 10000.0),
        risk_per_trade=t.get("risk_per_trade", 0.02),
        signal_confidence_threshold=t.get("signal_confidence_threshold", 0.55),
        lookback_days=t.get("lookback_days", 90),
        ohlcv_interval=t.get("ohlcv_interval", "1h"),
        news_weight=t.get("news_weight", 0.20),
        price_weight=t.get("price_weight", 0.80),
        signal_deadband=t.get("signal_deadband", 0.15),
        min_lot_size=t.get("min_lot_size", 1000.0),
        lot_unit=t.get("lot_unit", 1000.0),
        trading_mode=t.get("trading_mode", "paper"),
        position_review_enabled=t.get("position_review_enabled", False),
        reversal_confidence_min=t.get("reversal_confidence_min", 0.70),
        reversal_score_threshold=t.get("reversal_score_threshold", 0.25),
        max_holding_days=t.get("max_holding_days", 10),
        timeout_min_progress_pct=t.get("timeout_min_progress_pct", 0.30),
        profit_lock_min_progress_pct=t.get("profit_lock_min_progress_pct", 0.40),
        profit_lock_score_floor=t.get("profit_lock_score_floor", 0.15),
        rag_adjustment_enabled=t.get("rag_adjustment_enabled", True),
        rag_adjustment_max=t.get("rag_adjustment_max", 0.15),
        rag_adjustment_min_hits=t.get("rag_adjustment_min_hits", 2),
        rag_adjustment_search_top_n=t.get("rag_adjustment_search_top_n", 5),
        rag_adjustment_same_weight=t.get("rag_adjustment_same_weight", 0.10),
        rag_adjustment_opposite_weight=t.get("rag_adjustment_opposite_weight", 0.10),
        rag_adjustment_trade_multiplier=t.get("rag_adjustment_trade_multiplier", 1.0),
        rag_adjustment_forecast_multiplier=t.get("rag_adjustment_forecast_multiplier", 0.5),
        rag_adjustment_hold_multiplier=t.get("rag_adjustment_hold_multiplier", 0.3),
        sl_atr_mult_default=t.get("sl_atr_mult_default", 1.5),
        tp_atr_mult_default=t.get("tp_atr_mult_default", 3.0),
        sl_atr_mult_min=t.get("sl_atr_mult_min", 0.5),
        sl_atr_mult_max=t.get("sl_atr_mult_max", 3.0),
        tp_atr_mult_min=t.get("tp_atr_mult_min", 1.0),
        tp_atr_mult_max=t.get("tp_atr_mult_max", 6.0),
        tv_summary_enabled=t.get("tv_summary_enabled", False),
        tv_conflict_dampen=t.get("tv_conflict_dampen", 0.7),
    )

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

    s = raw.get("schedule", {})
    schedule = ScheduleConfig(
        run_times=s.get("run_times", ["07:00", "20:30"]),
        timezone=s.get("timezone", "Asia/Tokyo"),
    )

    lg = raw.get("logging", {})
    log_cfg = LoggingConfig(
        level=lg.get("level", "INFO"),
        file=lg.get("file", "logs/finance.log"),
        activity_log_file=lg.get("activity_log_file", "logs/activity.log"),
        rotate_timing=str(lg.get("rotate_timing") or lg.get("max_bytes", "10MB")),
        backup_count=lg.get("backup_count", 5),
    )

    nc = raw.get("news_collection", {})
    news_collection = NewsCollectionConfig(
        interval_minutes=nc.get("interval_minutes", 30),
        offset_minutes=nc.get("offset_minutes", 0),
        timezone=nc.get("timezone", "Asia/Tokyo"),
        inter_pair_delay_seconds=nc.get("inter_pair_delay_seconds", 60),
        news_freshness_hours=nc.get("news_freshness_hours", 24.0),
        summary_max_chars=nc.get("summary_max_chars", 600),
    )

    kw = raw.get("keywords", {})
    _default_kw = KeywordsConfig()
    keywords_cfg = KeywordsConfig(
        global_keywords=kw.get("global", _default_kw.global_keywords),
        japan_keywords=kw.get("japan", _default_kw.japan_keywords),
    )

    ec = raw.get("economic_calendar", {})
    economic_calendar_cfg = EconomicCalendarConfig(
        enabled=ec.get("enabled", False),
        fetch_time=ec.get("fetch_time", "06:00"),
        fetch_timezone=ec.get("fetch_timezone", "Asia/Tokyo"),
        lookahead_hours=ec.get("lookahead_hours", 48),
        currencies=ec.get("currencies", ["USD", "JPY", "EUR", "GBP"]),
        min_importance=ec.get("min_importance", 0),
        post_event_window_min=ec.get("post_event_window_min", 30),
        post_event_impact_min=ec.get("post_event_impact_min", 1),
        refresh_lookback_min=ec.get("refresh_lookback_min", 60),
    )

    _default_ns = NewsSourcesConfig()
    ns = raw.get("news_sources", {})
    fd = ns.get("feedly", {})
    # access_token: .env の FEEDLY_ACCESS_TOKEN から読み込む
    feedly_token = os.environ.get("FEEDLY_ACCESS_TOKEN", "")
    feedly_cfg = FeedlyConfig(
        enabled=fd.get("enabled", False),
        access_token=feedly_token,
        streams_fx=fd.get("streams_fx", []),
        streams_global=fd.get("streams_global", []),
        streams_japan=fd.get("streams_japan", []),
        count=fd.get("count", 20),
    )
    news_sources = NewsSourcesConfig(
        feeds_fx=ns.get("feeds_fx", _default_ns.feeds_fx),
        feeds_global=ns.get("feeds_global", _default_ns.feeds_global),
        feeds_japan=ns.get("feeds_japan", _default_ns.feeds_japan),
        feedly=feedly_cfg,
    )

    r = raw.get("rag", {})
    rag = RagConfig(
        db_path=r.get("db_path", "data/rag"),
        embedding_model=r.get("embedding_model", "nomic-embed-text"),
        news_lookback_hours=r.get("news_lookback_hours", 24),
        retrieval_top_k=r.get("retrieval_top_k", 5),
        reflection_lookback_count=r.get("reflection_lookback_count", 3),
        analysis_lookback_hours=r.get("analysis_lookback_hours", 8),
    )

    if abs(trading.news_weight + trading.price_weight - 1.0) > 1e-6:
        raise ValueError(
            f"news_weight + price_weight must equal 1.0, "
            f"got {trading.news_weight + trading.price_weight}"
        )

    n = raw.get("notification", {})
    notifier = NotifierConfig(
        notifier=n.get("notifier", "none"),
        notify_on_order_open=n.get("notify_on_order_open", True),
        notify_on_order_close=n.get("notify_on_order_close", True),
        notify_on_signal_skipped=n.get("notify_on_signal_skipped", False),
        notify_on_price_alert=n.get("notify_on_price_alert", True),
    )

    api_raw = raw.get("api", {})
    api_cfg = ApiConfig(
        enabled=api_raw.get("enabled", False),
        port=api_raw.get("port", 8811),
        run_trade_soft_timeout_sec=api_raw.get("run_trade_soft_timeout_sec", 10.0),
        ask_soft_timeout_sec=api_raw.get("ask_soft_timeout_sec", 60.0),
    )

    pm = raw.get("price_monitor", {})
    price_monitor = PriceMonitorConfig(
        enabled=pm.get("enabled", True),
        interval_minutes=pm.get("interval_minutes", 5),
        alert_threshold_pct=pm.get("alert_threshold_pct", 0.003),
        alert_step_pct=pm.get("alert_step_pct", 0.002),
        emergency_close_pct=pm.get("emergency_close_pct", 0.008),
        enable_emergency_close=pm.get("enable_emergency_close", False),
        trailing_stop_enabled=pm.get("trailing_stop_enabled", False),
        trailing_stop_breakeven_pct=pm.get("trailing_stop_breakeven_pct", 0.20),
        trailing_stop_activation_pct=pm.get("trailing_stop_activation_pct", 0.40),
        trailing_stop_distance_ratio=pm.get("trailing_stop_distance_ratio", 1.0),
    )
    if price_monitor.trailing_stop_enabled:
        if not (0.0 < price_monitor.trailing_stop_breakeven_pct < price_monitor.trailing_stop_activation_pct):
            raise ValueError(
                "trailing_stop_breakeven_pct must satisfy "
                "0 < breakeven_pct < activation_pct "
                f"(breakeven={price_monitor.trailing_stop_breakeven_pct}, "
                f"activation={price_monitor.trailing_stop_activation_pct})"
            )

    pp = raw.get("price_provider", {})
    td = pp.get("twelvedata", {})
    price_provider = PriceProviderConfig(
        realtime_provider=pp.get("realtime_provider", "yfinance"),
        twelvedata=TwelveDataConfig(
            daily_limit=td.get("daily_limit", 800),
            per_minute_limit=td.get("per_minute_limit", 8),
            watch_symbols=td.get("watch_symbols", []),
            use_for_monitor=td.get("use_for_monitor", True),
        ),
    )

    g = raw.get("gemini", {})
    gemini = GeminiConfig(
        model=g.get("model", "gemini-2.0-flash"),
        timeout_seconds=g.get("timeout_seconds", 60),
        max_retries=g.get("max_retries", 2),
    )

    oa = raw.get("openai", {})
    openai_cfg = OpenAIConfig(
        model=oa.get("model", "gpt-4o-mini"),
        timeout_seconds=oa.get("timeout_seconds", 60),
        max_retries=oa.get("max_retries", 2),
    )

    cl = raw.get("claude", {})
    claude_cfg = ClaudeConfig(
        model=cl.get("model", "claude-haiku-4-5-20251001"),
        timeout_seconds=cl.get("timeout_seconds", 60),
        max_retries=cl.get("max_retries", 2),
        max_tokens=cl.get("max_tokens", 4096),
    )

    lc = raw.get("llm", {})

    ollama_raw = lc.get("ollama", {})
    ollama_base = OllamaBaseConfig(
        base_url=ollama_raw.get("base_url", "http://localhost:11434"),
        timeout_seconds=ollama_raw.get("timeout_seconds", 120),
        max_retries=ollama_raw.get("max_retries", 2),
        max_concurrent=ollama_raw.get("max_concurrent", 2),
    )

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

    an = raw.get("analysis", {})
    ind = an.get("indicators", {})
    indicator_cfg = IndicatorToggleConfig(
        moving_averages=ind.get("moving_averages", True),
        rsi=ind.get("rsi", True),
        macd=ind.get("macd", True),
        bollinger_bands=ind.get("bollinger_bands", True),
        atr=ind.get("atr", True),
        adx=ind.get("adx", True),
        ichimoku=ind.get("ichimoku", True),
    )
    cp = an.get("chart_patterns", {})
    pattern_cfg = ChartPatternConfig(
        hammer=cp.get("hammer", True),
        shooting_star=cp.get("shooting_star", True),
        engulfing=cp.get("engulfing", True),
        doji=cp.get("doji", True),
        morning_evening_star=cp.get("morning_evening_star", True),
        three_soldiers_crows=cp.get("three_soldiers_crows", True),
        pin_bar=cp.get("pin_bar", True),
        inside_bar=cp.get("inside_bar", True),
        double_top_bottom=cp.get("double_top_bottom", False),
        head_shoulders=cp.get("head_shoulders", False),
        triangle=cp.get("triangle", False),
        range_bound=cp.get("range_bound", False),
        bb_squeeze=cp.get("bb_squeeze", False),
        atr_contraction=cp.get("atr_contraction", False),
        sr_breakout=cp.get("sr_breakout", False),
    )
    analysis_cfg = AnalysisConfig(
        indicators=indicator_cfg,
        chart_patterns=pattern_cfg,
        forecast_review_interval_hours=an.get("forecast_review_interval_hours", 8),
        forecast_start_hour=an.get("forecast_start_hour", 0),
        forecast_min_combined_score=an.get("forecast_min_combined_score", 0.15),
        forecast_significance_atr_ratio=an.get("forecast_significance_atr_ratio", 0.30),
    )

    tv = raw.get("tradingview", {})
    tradingview_cfg = TradingViewConfig(
        enabled=tv.get("enabled", False),
        cdp_host=tv.get("cdp_host", "localhost"),
        cdp_port=tv.get("cdp_port", 9222),
    )

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
