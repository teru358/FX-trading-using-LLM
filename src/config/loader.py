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
    EMBEDDING_PROVIDERS,
    EmbeddingConfig,
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
    MultiTimeframeConfig,
    NewsCollectionConfig,
    NewsSourcesConfig,
    NotifierConfig,
    OandaConfig,
    ProviderConfig,
    ProvidersConfig,
    PriceMonitorConfig,
    RagConfig,
    ScheduleConfig,
    TimeframeConfig,
    TradingConfig,
    TwelveDataConfig,
    WeeklyDiagnosisConfig,
    DataBackupConfig,
    OrchestratorConfig,
    OrchestratorPolicyConfig,
    OrchestratorMarketStateConfig,
    OrchestratorLlmConfig,
    OrchestratorLocksConfig,
    OrchestratorEntryConfig,
    OrchestratorFiringConfig,
    OrchestratorHindsightConfig,
    OrchestratorNotificationsConfig,
    OrchestratorAgentsConfig,
    AgentLlmConfig,
    OrchestratorAgentsLlmConfig,
)


# embedding provider 別の default base_url (embedding.base_url が空欄時のヒント表示用)
_EMBEDDING_BASE_URL_HINTS = {
    "ollama":   "http://localhost:11434",
    "llamacpp": "http://localhost:8080/v1",
}


# LLM provider 別の default base_url (provider_config.base_url が空欄時のヒント表示用)
_LLM_BASE_URL_HINTS = {
    "ollama":   "http://localhost:11434",
    "llamacpp": "http://localhost:8080/v1",
}


from src.config.errors import ConfigError
from src.config.migration import check_migration, check_required_files


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


def _build_embedding_config(raw: dict) -> EmbeddingConfig:
    """llm.yaml の embedding: ブロックから EmbeddingConfig を構築する。

    provider の値検証と base_url 必須チェックを行う。未知の provider は
    embedder.py で「llamacpp 以外 = Ollama」と扱われ、誤ったプロトコルへ
    黙って接続してしまうため fail-fast にする。
    """
    cfg = _from_dict(EmbeddingConfig, raw or {})

    if cfg.provider not in EMBEDDING_PROVIDERS:
        raise ConfigError(
            f"embedding.provider must be one of {EMBEDDING_PROVIDERS}, "
            f"got '{cfg.provider}'. Edit config/llm.yaml."
        )

    if not cfg.base_url:
        hint = _EMBEDDING_BASE_URL_HINTS.get(cfg.provider, "")
        raise ConfigError(
            f"embedding.base_url is required when provider='{cfg.provider}'. "
            f"Edit config/llm.yaml and set base_url (e.g. \"{hint}\")."
        )

    return cfg


def _build_orchestrator_config(data: dict) -> OrchestratorConfig:
    """orchestrator YAML ブロックを OrchestratorConfig に組み立てる (spec §12)。

    各ネスト dict は _from_dict でフラット構築する (キーはフィールド名と 1:1)。
    欠落キーは dataclass デフォルトで補完される。
    """
    return OrchestratorConfig(
        enabled=data.get("enabled", False),
        mode=data.get("mode", "shadow"),
        approval_gate=data.get("approval_gate", False),
        pairs=list(data.get("pairs", []) or []),
        policy=_from_dict(OrchestratorPolicyConfig, data.get("policy", {}) or {}),
        market_state=_from_dict(OrchestratorMarketStateConfig, data.get("market_state", {}) or {}),
        llm=_from_dict(OrchestratorLlmConfig, data.get("llm", {}) or {}),
        locks=_from_dict(OrchestratorLocksConfig, data.get("locks", {}) or {}),
        entry=_from_dict(OrchestratorEntryConfig, data.get("entry", {}) or {}),
        firing=_from_dict(OrchestratorFiringConfig, data.get("firing", {}) or {}),
        hindsight=_from_dict(
            OrchestratorHindsightConfig, data.get("hindsight", {}) or {}
        ),
        notifications=_from_dict(
            OrchestratorNotificationsConfig, data.get("notifications", {}) or {}
        ),
        agents=_from_dict(OrchestratorAgentsConfig, data.get("agents", {}) or {}),
        tick_migration_stage=data.get("tick_migration_stage", "off"),
        quote_stream_poll_seconds=data.get("quote_stream_poll_seconds", 2),
        execution_opinion_recheck_enabled=data.get(
            "execution_opinion_recheck_enabled", False
        ),
        plan_ttl_max_hours=data.get("plan_ttl_max_hours", 0),
    )


# 分割設定ファイル。settings.yaml にマージされる top-level ブロックを持つ。
# llm.yaml / strategy.yaml は必須 (migration.REQUIRED_CONFIG_FILES)。
SPLIT_CONFIG_FILES = (
    "instruments.yaml", "news_sources.yaml", "llm.yaml", "strategy.yaml",
)


class _DuplicateKeyError(yaml.YAMLError):
    """YAML mapping 内にキーが重複していた。"""


class StrictLoader(yaml.SafeLoader):
    """重複した mapping キーを拒否する SafeLoader。

    yaml.safe_load は重複キーを無警告で後勝ち上書きする。config を複数ファイルへ
    分割する作業ではブロックの二重記載が起きやすく、そのまま通すとリスク
    パラメータを含む設定が黙って消える。migration._check_duplicate_blocks は
    読み込み後の dict を見るためファイル間の重複しか検出できず、この経路は
    ここで塞ぐ必要がある。
    """


def _construct_mapping_no_duplicates(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKeyError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_duplicates,
)


def _load_split_yaml(fpath: Path) -> dict | None:
    """分割設定ファイルを読み、mapping であることを検証する。

    存在しない / 空ファイルなら None。非 mapping・構文エラー・重複キーは ConfigError。
    """
    if not fpath.exists():
        return None
    try:
        with open(fpath, encoding="utf-8") as f:
            data = yaml.load(f, Loader=StrictLoader)
    except yaml.YAMLError as e:
        raise ConfigError(f"{fpath.name}: YAML syntax error: {e}") from e
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError(
            f"{fpath.name} must contain a YAML mapping at the top level "
            f"(got {type(data).__name__})."
        )
    return data


def _merge_split_configs(base: dict, config_dir: Path) -> dict:
    """分割設定ファイルをメイン設定にマージする。

    マージは top-level キー粒度の上書きであり、サブキーは合成されない
    (同名ブロックがあれば丸ごと置換)。重複の検出は _check_migration が担当する。
    """
    for fname in SPLIT_CONFIG_FILES:
        extra = _load_split_yaml(config_dir / fname)
        if extra:
            for key, value in extra.items():
                base[key] = value
    return base


def _load_provider_yaml(name: str, config_dir: Path) -> dict | None:
    """config/providers/{name}.yaml を読み込む。存在しなければ None。"""
    fpath = config_dir / "providers" / f"{name}.yaml"
    if not fpath.exists():
        return None
    with open(fpath, encoding="utf-8") as f:
        data = yaml.load(f, Loader=StrictLoader)
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
        mt5_cfg = _from_dict(Mt5Config, raw)
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


def _validate_top_level(raw: dict) -> tuple[str, str, str | None, str]:
    """settings.yaml トップレベルから mode/paper_provider/live_broker/timezone を検証して返す。"""
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

    # 移行リリース限定 (§3.5): 既定値への静かな転落を防ぐ。
    # 移行完了後は既定値ありキーに戻してよい。
    timezone = raw.get("timezone")
    if not timezone:
        raise ConfigError(
            "settings.yaml: timezone is required at the top level. "
            "It replaces schedule.timezone / news_collection.timezone / "
            "economic_calendar.fetch_timezone. "
            "(config migration 2026-07-20)"
        )

    return mode, paper_provider, live_broker, timezone


_DEPRECATED_TRADING_KEYS = {
    "max_total_positions",
    "max_positions_per_currency_group",
    "max_same_direction_per_group",
    "trading_mode",
    "initial_balance",
    "drawdown_kill_switch_lookback_days",
}


def _strip_deprecated_keys(trading_dict: dict) -> dict:
    """deprecated trading キーを除去。

    Phase 3b で max_total_positions / max_positions_per_currency_group /
    max_same_direction_per_group を廃止し max_positions_per_pair に統合した。
    Config restructure で trading.trading_mode を廃止し、トップレベル
    mode フィールドに置換した。
    Config restructure (Task 9) で trading.initial_balance を廃止し
    balance.json (balance_snapshot) に一本化、trading.drawdown_kill_switch_lookback_days
    を廃止し balance_snapshot.peak_balance を直接参照するようにした。
    """
    result = dict(trading_dict)
    for key in list(result.keys()):
        if key in _DEPRECATED_TRADING_KEYS:
            hint = ""
            if key == "trading_mode":
                hint = " Use top-level 'mode: paper|live|live_test' instead."
            elif key == "initial_balance":
                hint = " Balance is now read from state/balance.json (balance_snapshot)."
            elif key == "drawdown_kill_switch_lookback_days":
                hint = " DD now reads balance_snapshot.peak_balance directly."
            logger.warning(
                f"[CONFIG] 'trading.{key}' is deprecated and ignored.{hint} "
                f"Remove it from settings.yaml."
            )
            result.pop(key)
    return result


# ── LLM 設定の構築とバリデーション ──────────────────────────────

# 5 agent 名。authoritative source は schema.OrchestratorAgentsLlmConfig の field 集合。
# 同名の tuple が src/llm/factory.py:AGENT_NAMES にもある (層が違う)。両方+schema を同期。
_AGENT_LLM_NAMES = (
    "planner", "news", "technical", "execution_opinion", "context_summary",
)


def _build_agent_llms(raw_agents: dict) -> OrchestratorAgentsLlmConfig:
    """config/llm.yaml の agents: ブロックを OrchestratorAgentsLlmConfig に組み立てる。

    起動時に検証する (typo/欠落を silent fallback させない):
      - 未知 agent キー (typo) → ConfigError
      - agents.<name> が mapping でない → ConfigError
      - agents.<name> 内の未知キー (例: provder) → ConfigError
        (_from_dict が黙って捨てると provider="" となり意図しない fallback で起動するため)
      - 各 agent: provider 指定時は provider ∈ LLM_PROVIDERS、かつ model 必須
    provider != top-level provider のときの provider_configs 必須チェックは
    provider_configs 構築後 (load_config で _validate_agent_provider_configs) に行う。
    """
    raw_agents = raw_agents or {}
    for key in raw_agents:
        if key not in _AGENT_LLM_NAMES:
            raise ConfigError(
                f"config/llm.yaml: unknown agent key {key!r} "
                f"(expected one of {_AGENT_LLM_NAMES}). "
                f"Fix the typo — an unknown key would silently fall back to the "
                f"default role LLM instead of using the intended per-agent config."
            )
    allowed_keys = {f.name for f in fields(AgentLlmConfig)}  # provider / model / temperature
    kwargs = {}
    for name in _AGENT_LLM_NAMES:
        if name not in raw_agents:
            continue  # 未指定 agent は fallback (既定 AgentLlmConfig)
        entry = raw_agents[name]
        if not isinstance(entry, dict):
            raise ConfigError(
                f"config/llm.yaml: agents.{name} must be a mapping "
                f"(provider/model/temperature), got {type(entry).__name__}."
            )
        unknown = set(entry) - allowed_keys
        if unknown:
            raise ConfigError(
                f"config/llm.yaml: agents.{name} has unknown key(s) {sorted(unknown)} "
                f"(allowed: {sorted(allowed_keys)}). Fix the typo — an unknown key would "
                f"be silently dropped, causing an unintended fallback instead of the "
                f"intended per-agent LLM config."
            )
        agent_cfg = _from_dict(AgentLlmConfig, entry)
        if agent_cfg.provider:
            if agent_cfg.provider not in LLM_PROVIDERS:
                raise ConfigError(
                    f"Unknown provider {agent_cfg.provider!r} for agent {name!r} "
                    f"in config/llm.yaml. Must be one of {LLM_PROVIDERS}."
                )
            if not agent_cfg.model:
                raise ConfigError(
                    f"agents.{name}.model is required when a provider is set "
                    f"(provider={agent_cfg.provider!r}). "
                    f"Set a model name appropriate for the provider in config/llm.yaml."
                )
        kwargs[name] = agent_cfg
    return OrchestratorAgentsLlmConfig(**kwargs)


def _validate_agent_provider_configs(
    agent_llms: OrchestratorAgentsLlmConfig, llm_cfg: LLMConfig
) -> None:
    """各 agent の provider が top-level と異なる場合、provider_configs に接続設定が必須。

    factory.create_agent_llm と同じ規則を load 時に全 agent へ適用する
    (未配線 News/Technical/ContextSummary も含め起動時に弾く)。
    """
    for name in _AGENT_LLM_NAMES:
        agent_cfg = getattr(agent_llms, name)
        if not agent_cfg.provider:
            continue  # fallback (役割 LLM) — provider_configs 不要
        if agent_cfg.provider == llm_cfg.provider:
            continue  # top-level と同一 provider — 既存 provider_config 流用
        if agent_cfg.provider not in llm_cfg.provider_configs:
            raise ConfigError(
                f"agent {name!r} provider {agent_cfg.provider!r} is not defined in "
                f"llm.provider_configs (top-level provider is {llm_cfg.provider!r}). "
                f"Add a connection config under llm.provider_configs.{agent_cfg.provider}."
            )


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
            f"Edit config/llm.yaml and set base_url (e.g. \"{hint}\")."
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
            f"Edit config/llm.yaml and set a model name appropriate for the provider."
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
            "See config/llm.yaml.example for the new layout."
        )
    # 旧形式の検出: llm.<role>.provider が指定されている
    for role in ("news_analysis", "price_analysis", "reflection"):
        if isinstance(lc.get(role), dict) and "provider" in lc[role]:
            raise ConfigError(
                f"Legacy per-role 'provider' detected at llm.{role}.provider. "
                "Use a single top-level 'llm.provider' instead. "
                "See config/llm.yaml.example for the new layout."
            )

    provider = lc.get("provider", "")
    if not provider:
        raise ConfigError(
            "llm.provider is required. "
            f"Choose one of {LLM_PROVIDERS} in config/llm.yaml."
        )
    if provider not in LLM_PROVIDERS:
        raise ConfigError(
            f"Unknown llm.provider '{provider}'. Must be one of {LLM_PROVIDERS}."
        )

    provider_config = _build_provider_config(provider, lc.get("provider_config", {}) or {})

    # provider 別接続設定 (orchestrator agent 用)。各エントリを provider 名で検証付き構築。
    raw_provider_configs = lc.get("provider_configs", {}) or {}
    provider_configs: dict[str, ProviderConfig] = {}
    for pname, praw in raw_provider_configs.items():
        if pname not in LLM_PROVIDERS:
            raise ConfigError(
                f"Unknown provider '{pname}' in llm.provider_configs. "
                f"Must be one of {LLM_PROVIDERS}."
            )
        provider_configs[pname] = _build_provider_config(pname, praw or {})

    return LLMConfig(
        provider=provider,
        provider_config=provider_config,
        provider_configs=provider_configs,
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

    config_dir = config_path.parent

    check_required_files(config_dir)

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.load(f, Loader=StrictLoader)
    if raw is None:
        raise ConfigError(f"{config_path.name} is empty.")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{config_path.name} must contain a YAML mapping at the top level."
        )

    # 移行検出はマージ前に行う (§3.6)。マージ後ではブロック全置換により
    # 旧キーが他ファイルに覆い隠されて消え、検出できない。
    per_file = {config_path.name: raw}
    for fname in SPLIT_CONFIG_FILES:
        data = _load_split_yaml(config_dir / fname)
        if data:
            per_file[fname] = data
    check_migration(per_file)

    raw = _merge_split_configs(raw, config_dir)

    # ── トップレベル mode + provider 選択 ──
    mode, paper_provider, live_broker, timezone = _validate_top_level(raw)
    providers_cfg = _build_providers_config(
        mode, paper_provider, live_broker, config_path.parent,
    )

    # ── フラット configs (_from_dict で自動構築) ──────────────────

    trading_raw = _strip_deprecated_keys(raw.get("trading", {}) or {})
    trading = _from_dict(TradingConfig, trading_raw)
    schedule = _from_dict(ScheduleConfig, raw.get("schedule", {}))
    news_collection = _from_dict(NewsCollectionConfig, raw.get("news_collection", {}))
    rag = _from_dict(RagConfig, raw.get("rag", {}))
    embedding = _build_embedding_config(raw.get("embedding", {}))
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
        # 旧名 "pairs" の後方互換は廃止 (2026-07-20)。top-level typo 検出
        # (migration.KNOWN_TOP_LEVEL_KEYS) が "pairs" を未知キーとして弾くため、
        # この fallback は到達不能だった。意図を明確にするため経路ごと削除する。
        for p in raw.get("instruments", [])
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
    )

    # ── バリデーション ────────────────────────────────────────────

    if abs(trading.news_weight + trading.price_weight - 1.0) > 1e-6:
        raise ValueError(
            f"news_weight + price_weight must equal 1.0, "
            f"got {trading.news_weight + trading.price_weight}"
        )

    # ── AppConfig 組み立て ────────────────────────────────────────

    # agent_llms を先に構築・検証 (provider_configs クロスチェックは llm_cfg が必要)
    agent_llms_cfg = _build_agent_llms(raw.get("agents", {}) or {})
    _validate_agent_provider_configs(agent_llms_cfg, llm_cfg)

    return AppConfig(
        mode=mode,
        paper_provider=paper_provider,
        live_broker=live_broker,
        timezone=timezone,
        providers=providers_cfg,
        trading=trading,
        instruments=instruments,
        schedule=schedule,
        logging=log_cfg,
        news_collection=news_collection,
        news_sources=news_sources,
        rag=rag,
        embedding=embedding,
        notifier=notifier,
        price_monitor=price_monitor,
        api=api_cfg,
        llm=llm_cfg,
        analysis=analysis_cfg,
        keywords=keywords_cfg,
        economic_calendar=economic_calendar_cfg,
        weekly_diagnosis=weekly_diagnosis_cfg,
        data_backup=data_backup_cfg,
        orchestrator=_build_orchestrator_config(raw.get("orchestrator", {}) or {}),
        agent_llms=agent_llms_cfg,
    )
