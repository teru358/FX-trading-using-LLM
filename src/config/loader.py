"""設定ローダー。

YAML ファイル (+ 分割 yaml) を読み込み、schema.py の dataclass に
組み立てる。公開は `src.config.load_config` 経由で行う。

_from_dict ヘルパーで dataclass のフィールド定義からデフォルト値を自動適用し、
手書きの `.get("field", default)` ボイラープレートを排除する。
YAML キー ≠ フィールド名のケース (KeywordsConfig 等) やネスト構造の
特殊ケースは明示的に手動構築する。
"""
from __future__ import annotations

import logging
import os
from dataclasses import fields
from pathlib import Path

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

from src.config.schema import (
    AnalysisConfig,
    ApiConfig,
    AppConfig,
    BASE_DIR,
    ChartPatternConfig,
    EconomicCalendarConfig,
    FeedlyConfig,
    IndicatorToggleConfig,
    InstrumentConfig,
    KeywordsConfig,
    LLM_PROVIDERS,
    LLM_PROVIDERS_REQUIRING_BASE_URL,
    LLMConfig,
    LLMRoleConfig,
    LoggingConfig,
    Mt5Config,
    Mt5FallbackConfig,
    MultiTimeframeConfig,
    NewsCollectionConfig,
    NewsSourcesConfig,
    NotifierConfig,
    OandaConfig,
    ProviderConfig,
    ProvidersConfig,
    ForecastAccuracyFeedbackConfig,
    PriceMonitorConfig,
    RagConfig,
    ScheduleConfig,
    TimeframeConfig,
    TradingConfig,
    TwelveDataConfig,
    WeeklyDiagnosisConfig,
    DataBackupConfig,
)


# embedding provider 別の default base_url (rag.embedding_base_url が空欄かつ未バリデート時のヒント表示用)
_EMBEDDING_BASE_URL_HINTS = {
    "ollama":   "http://localhost:11434",
    "llamacpp": "http://localhost:8080/v1",
}

# LLM provider 別の default base_url (provider_config.base_url が空欄時のヒント表示用)
_LLM_BASE_URL_HINTS = {
    "ollama":   "http://localhost:11434",
    "llamacpp": "http://localhost:8080/v1",
}


class ConfigError(ValueError):
    """設定の起動時バリデーションで検出された致命的エラー。"""


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


def _load_provider_yaml(name: str, config_dir: Path) -> dict | None:
    """config/providers/{name}.yaml を読み込む。存在しなければ None。"""
    fpath = config_dir / "providers" / f"{name}.yaml"
    if not fpath.exists():
        return None
    with open(fpath, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"providers/{name}.yaml must contain a YAML mapping at the top level"
        )
    return data


_VALID_MODES = ("paper", "live", "live_test")
_VALID_PAPER_PROVIDERS = ("yfinance", "twelvedata")
_VALID_LIVE_BROKERS = ("mt5", "oanda")


def _build_providers_config(
    mode: str,
    paper_provider: str,
    live_broker: str | None,
    config_dir: Path,
):
    """mode + provider 選択に応じて、必須 provider yaml を読み込む。"""
    td_cfg: TwelveDataConfig | None = None
    if paper_provider == "twelvedata":
        raw = _load_provider_yaml("twelvedata", config_dir)
        if raw is None:
            raise ConfigError(
                "providers/twelvedata.yaml not found (required when "
                "paper_provider=twelvedata). Copy providers/twelvedata.yaml.example."
            )
        td_cfg = _from_dict(TwelveDataConfig, raw)

    mt5_cfg: Mt5Config | None = None
    if live_broker == "mt5":
        raw = _load_provider_yaml("mt5", config_dir)
        if raw is None:
            raise ConfigError(
                "providers/mt5.yaml not found (required when live_broker=mt5). "
                "Copy providers/mt5.yaml.example."
            )
        mt5_raw = dict(raw)
        if "fallback" in mt5_raw and isinstance(mt5_raw["fallback"], dict):
            mt5_raw["fallback"] = _from_dict(Mt5FallbackConfig, mt5_raw["fallback"])
        mt5_cfg = _from_dict(Mt5Config, mt5_raw)
        if not mt5_cfg.bridge_url:
            raise ConfigError(
                "providers/mt5.yaml: bridge_url is required (got empty string)."
            )

    oanda_cfg: OandaConfig | None = None
    if live_broker == "oanda":
        raw = _load_provider_yaml("oanda", config_dir)
        if raw is None:
            raise ConfigError(
                "providers/oanda.yaml not found (required when live_broker=oanda). "
                "Copy providers/oanda.yaml.example."
            )
        oanda_cfg = _from_dict(OandaConfig, raw)

    return ProvidersConfig(twelvedata=td_cfg, mt5=mt5_cfg, oanda=oanda_cfg)


def _validate_top_level(raw: dict) -> tuple[str, str, str | None]:
    """settings.yaml トップレベルから mode/paper_provider/live_broker を検証して返す。"""
    mode = raw.get("mode", "paper")
    if mode not in _VALID_MODES:
        raise ConfigError(
            f"settings.yaml: mode={mode!r} is invalid. "
            f"Must be one of {_VALID_MODES}."
        )

    paper_provider = raw.get("paper_provider", "yfinance")
    if paper_provider not in _VALID_PAPER_PROVIDERS:
        raise ConfigError(
            f"settings.yaml: paper_provider={paper_provider!r} is invalid. "
            f"Must be one of {_VALID_PAPER_PROVIDERS}."
        )

    live_broker = raw.get("live_broker", None)
    if live_broker is not None and live_broker not in _VALID_LIVE_BROKERS:
        raise ConfigError(
            f"settings.yaml: live_broker={live_broker!r} is invalid. "
            f"Must be one of {_VALID_LIVE_BROKERS} or null."
        )

    if mode in ("live", "live_test") and live_broker is None:
        raise ConfigError(
            f"settings.yaml: mode={mode!r} requires live_broker (got null). "
            f"Set live_broker to one of {_VALID_LIVE_BROKERS}."
        )

    if mode == "live_test" and live_broker == "oanda":
        raise ConfigError(
            "settings.yaml: mode='live_test' is not supported with live_broker='oanda'. "
            "Use live_broker='mt5'."
        )

    return mode, paper_provider, live_broker


_DEPRECATED_TRADING_KEYS = {
    "max_total_positions",
    "max_positions_per_currency_group",
    "max_same_direction_per_group",
    "trading_mode",
}


def _strip_deprecated_keys(trading_dict: dict) -> dict:
    """deprecated trading キーを除去。

    Phase 3b で max_total_positions / max_positions_per_currency_group /
    max_same_direction_per_group を廃止し max_positions_per_pair に統合した。
    Config restructure で trading.trading_mode を廃止し、トップレベル
    mode フィールドに置換した。
    """
    result = dict(trading_dict)
    for key in list(result.keys()):
        if key in _DEPRECATED_TRADING_KEYS:
            hint = ""
            if key == "trading_mode":
                hint = " Use top-level 'mode: paper|live|live_test' instead."
            logger.warning(
                f"[CONFIG] 'trading.{key}' is deprecated and ignored.{hint} "
                f"Remove it from settings.yaml."
            )
            result.pop(key)
    return result


# ── LLM 設定の構築とバリデーション ──────────────────────────────

def _build_provider_config(provider: str, raw: dict) -> ProviderConfig:
    """provider_config セクションを ProviderConfig に組み立て、provider 別の補完/検証を行う。

    - ollama / llamacpp の base_url が空欄なら ConfigError (起動阻止)
    - claude-cli の command が空欄なら "claude" 自動補完 (PATH 解決前提)
    - claude (API) の max_tokens が 0 なら 4096 補完
    - timeout_seconds / max_retries / max_concurrent は dataclass デフォルトに任せる
    """
    pc = _from_dict(ProviderConfig, raw)

    if provider in LLM_PROVIDERS_REQUIRING_BASE_URL and not pc.base_url:
        hint = _LLM_BASE_URL_HINTS.get(provider, "")
        raise ConfigError(
            f"llm.provider_config.base_url is required when provider='{provider}'. "
            f"Edit config/settings.yaml and set base_url (e.g. \"{hint}\")."
        )

    if provider == "claude-cli" and not pc.command:
        pc.command = "claude"

    if provider == "claude" and pc.max_tokens <= 0:
        pc.max_tokens = 4096

    return pc


def _build_role_config(raw: dict, default_temp: float, *, provider: str, role: str) -> LLMRoleConfig:
    model = (raw or {}).get("model", "") or ""
    temperature = (raw or {}).get("temperature", default_temp)
    if not model:
        raise ConfigError(
            f"llm.{role}.model is required (provider='{provider}'). "
            f"Edit config/settings.yaml and set a model name appropriate for the provider."
        )
    return LLMRoleConfig(model=model, temperature=temperature)


def _build_llm_config(lc: dict) -> LLMConfig:
    """yaml の llm: セクションから LLMConfig を構築する。

    旧形式 (llm.ollama / llm.llamacpp / llm.claude_cli, llm.<role>.provider) は
    Phase 3c でサポートを廃止。検出時は ConfigError を投げ、移行を促す。
    """
    # 旧形式の検出: llm.ollama / llm.llamacpp / llm.claude_cli が yaml に残っている
    legacy_keys = [k for k in ("ollama", "llamacpp", "claude_cli") if k in lc]
    if legacy_keys:
        raise ConfigError(
            f"Legacy LLM config detected (keys: {legacy_keys}). "
            "The schema was consolidated to a single 'provider' + 'provider_config'. "
            "See config/settings.yaml.example for the new layout."
        )
    # 旧形式の検出: llm.<role>.provider が指定されている
    for role in ("news_analysis", "price_analysis", "reflection"):
        if isinstance(lc.get(role), dict) and "provider" in lc[role]:
            raise ConfigError(
                f"Legacy per-role 'provider' detected at llm.{role}.provider. "
                "Use a single top-level 'llm.provider' instead. "
                "See config/settings.yaml.example for the new layout."
            )

    provider = lc.get("provider", "")
    if not provider:
        raise ConfigError(
            "llm.provider is required. "
            f"Choose one of {LLM_PROVIDERS} in config/settings.yaml."
        )
    if provider not in LLM_PROVIDERS:
        raise ConfigError(
            f"Unknown llm.provider '{provider}'. Must be one of {LLM_PROVIDERS}."
        )

    provider_config = _build_provider_config(provider, lc.get("provider_config", {}) or {})

    return LLMConfig(
        provider=provider,
        provider_config=provider_config,
        news_analysis=_build_role_config(
            lc.get("news_analysis", {}), 0.3, provider=provider, role="news_analysis",
        ),
        price_analysis=_build_role_config(
            lc.get("price_analysis", {}), 0.1, provider=provider, role="price_analysis",
        ),
        reflection=_build_role_config(
            lc.get("reflection", {}), 0.3, provider=provider, role="reflection",
        ),
    )


# ── load_config ──────────────────────────────────────────────────

def load_config(config_path: Path | None = None) -> AppConfig:
    load_dotenv(BASE_DIR / ".env")

    if config_path is None:
        config_path = BASE_DIR / "config" / "settings.yaml"

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = _merge_split_configs(raw, config_path.parent)

    # ── トップレベル mode + provider 選択 ──
    mode, paper_provider, live_broker = _validate_top_level(raw)
    providers_cfg = _build_providers_config(
        mode, paper_provider, live_broker, config_path.parent,
    )

    # ── フラット configs (_from_dict で自動構築) ──────────────────

    trading_raw = _strip_deprecated_keys(raw.get("trading", {}) or {})
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
    economic_calendar_cfg = _from_dict(EconomicCalendarConfig, raw.get("economic_calendar", {}))
    weekly_diagnosis_cfg = _from_dict(WeeklyDiagnosisConfig, raw.get("weekly_diagnosis", {}))
    data_backup_cfg = _from_dict(DataBackupConfig, raw.get("data_backup", {}))

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

    # LLM: provider + provider_config + 3 roles の単一エントリ
    lc = raw.get("llm", {}) or {}
    llm_cfg = _build_llm_config(lc)

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

    # rag.embedding_base_url: 空欄なら起動阻止 (provider 別 default を提示)
    if rag.embedding_provider in LLM_PROVIDERS_REQUIRING_BASE_URL and not rag.embedding_base_url:
        hint = _EMBEDDING_BASE_URL_HINTS.get(rag.embedding_provider, "")
        raise ConfigError(
            f"rag.embedding_base_url is required when embedding_provider='{rag.embedding_provider}'. "
            f"Edit config/settings.yaml and set embedding_base_url (e.g. \"{hint}\")."
        )

    # ── AppConfig 組み立て ────────────────────────────────────────

    return AppConfig(
        mode=mode,
        paper_provider=paper_provider,
        live_broker=live_broker,
        providers=providers_cfg,
        trading=trading,
        instruments=instruments,
        schedule=schedule,
        logging=log_cfg,
        news_collection=news_collection,
        news_sources=news_sources,
        rag=rag,
        notifier=notifier,
        price_monitor=price_monitor,
        api=api_cfg,
        llm=llm_cfg,
        analysis=analysis_cfg,
        keywords=keywords_cfg,
        economic_calendar=economic_calendar_cfg,
        weekly_diagnosis=weekly_diagnosis_cfg,
        data_backup=data_backup_cfg,
    )
