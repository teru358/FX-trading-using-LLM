from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent

# Ollama のデフォルトモデル（llm.<role>.model が空の場合に使用）
_DEFAULT_OLLAMA_MODEL = "llama3.1:8b"


@dataclass
class PairConfig:
    symbol: str
    display_name: str
    pip_value: float
    base_currency: str
    quote_currency: str
    enabled: bool = True

    @property
    def currencies(self) -> tuple[str, str]:
        return (self.base_currency, self.quote_currency)

    @property
    def news_categories(self) -> list[str]:
        """通貨ペアに関連するニュースカテゴリを返す。"""
        cats = ["fx", "global"]
        if "JPY" in (self.base_currency, self.quote_currency):
            cats.append("japan")
        return cats


@dataclass
class OllamaBaseConfig:
    """Ollama 接続設定（モデル指定は llm.<role>.model で行う）。"""
    base_url: str = "http://localhost:11434"
    timeout_seconds: int = 120
    max_retries: int = 2
    max_concurrent: int = 2


@dataclass
class TradingConfig:
    initial_balance: float = 10000.0
    risk_per_trade: float = 0.02
    signal_confidence_threshold: float = 0.55
    max_concurrent_positions: int = 3
    lookback_days: int = 90
    ohlcv_interval: str = "1h"
    news_weight: float = 0.40
    price_weight: float = 0.60
    signal_deadband: float = 0.15
    min_lot_size: float = 1000.0
    lot_unit: float = 1000.0
    trading_mode: str = "paper"


@dataclass
class NewsCollectionConfig:
    interval_minutes: int = 30
    timezone: str = "Asia/Tokyo"
    inter_pair_delay_seconds: int = 60
    news_freshness_hours: float = 24.0   # この時間以上古い記事は除外


@dataclass
class NewsSourcesConfig:
    feeds_fx: list[str] = field(default_factory=lambda: [
        "https://feeds.feedburner.com/forexlive/all",
        "https://www.fxstreet.com/rss/news",
        "https://www.investing.com/rss/news_285.rss",
    ])
    feeds_global: list[str] = field(default_factory=lambda: [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.apnews.com/rss/business",
        "https://www.ft.com/rss/home/uk",
    ])
    feeds_japan: list[str] = field(default_factory=lambda: [
        "https://www3.nhk.or.jp/nhkworld/en/news/rss.xml",
        "https://japantoday.com/feed",
        "https://www.japantimes.co.jp/feed/",
        "https://asia.nikkei.com/rss/feed/nar",
    ])
    jpy_keywords: list[str] = field(default_factory=lambda: [
        # 英語
        "boj", "bank of japan", "nippon ginko", "japan", "japanese",
        "yen", "jpy", "ueda",
        # 日本語
        "日銀", "円安", "円高", "金利", "為替", "利上げ", "利下げ", "植田", "円",
    ])


@dataclass
class RagConfig:
    db_path: str = "data/rag"
    embedding_model: str = "nomic-embed-text"
    news_lookback_hours: int = 24
    retrieval_top_k: int = 5
    reflection_lookback_count: int = 3
    analysis_lookback_hours: int = 8


@dataclass
class ScheduleConfig:
    run_times: list[str] = field(default_factory=lambda: ["15:00", "21:00"])
    timezone: str = "Asia/Tokyo"


@dataclass
class PriceMonitorConfig:
    """価格急変動監視の設定。"""
    enabled: bool = True
    interval_minutes: int = 5
    alert_threshold_pct: float = 0.003    # 0.3% 損失で最初の通知
    alert_step_pct: float = 0.002         # 追加 0.2% ごとに再通知（スパム防止）
    emergency_close_pct: float = 0.008    # 0.8% 損失で緊急損切り（0 = 無効）
    enable_emergency_close: bool = False  # 緊急損切り機能の有効/無効


@dataclass
class NotifierConfig:
    notifier: str = "none"
    notify_on_order_open: bool = True
    notify_on_order_close: bool = True
    notify_on_signal_skipped: bool = False
    notify_on_price_alert: bool = True    # 価格急変動通知


@dataclass
class GeminiConfig:
    model: str = "gemini-2.0-flash"
    timeout_seconds: int = 60
    max_retries: int = 2


@dataclass
class OpenAIConfig:
    model: str = "gpt-4o-mini"
    timeout_seconds: int = 60
    max_retries: int = 2


@dataclass
class ClaudeConfig:
    model: str = "claude-haiku-4-5-20251001"
    timeout_seconds: int = 60
    max_retries: int = 2
    max_tokens: int = 4096


@dataclass
class LLMRoleConfig:
    """1つの分析ロールが使用するプロバイダー・モデル・温度の設定。"""
    provider: str = "ollama"
    model: str = ""           # 空 = _DEFAULT_OLLAMA_MODEL を使用（ollama 時）
    temperature: float = 0.2  # ロール別温度


@dataclass
class LLMConfig:
    """3種類の分析ロールごとに LLM プロバイダーを個別設定する。"""
    ollama: OllamaBaseConfig = field(default_factory=OllamaBaseConfig)
    news_analysis: LLMRoleConfig = field(
        default_factory=lambda: LLMRoleConfig(temperature=0.3)
    )
    price_analysis: LLMRoleConfig = field(
        default_factory=lambda: LLMRoleConfig(temperature=0.1)
    )
    reflection: LLMRoleConfig = field(
        default_factory=lambda: LLMRoleConfig(temperature=0.3)
    )


@dataclass
class ApiConfig:
    """REST API サーバー設定。"""
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8811


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/finance.log"
    activity_log_file: str = "logs/activity.log"
    max_bytes: int = 10_485_760
    backup_count: int = 5


@dataclass
class AppConfig:
    trading: TradingConfig
    pairs: list[PairConfig]
    schedule: ScheduleConfig
    logging: LoggingConfig
    news_collection: NewsCollectionConfig
    news_sources: NewsSourcesConfig
    rag: RagConfig
    notifier: NotifierConfig
    price_monitor: PriceMonitorConfig = field(default_factory=PriceMonitorConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)

    @property
    def state_dir(self) -> Path:
        return BASE_DIR / "data" / "state"

    @property
    def rag_db_path(self) -> Path:
        return BASE_DIR / self.rag.db_path

    @property
    def prices_db_path(self) -> Path:
        return BASE_DIR / "data" / "prices.db"

    @property
    def user_notes_path(self) -> Path:
        return BASE_DIR / "config" / "user_notes.md"

    @property
    def enabled_pairs(self) -> list[PairConfig]:
        return [p for p in self.pairs if p.enabled]


def load_config(config_path: Path | None = None) -> AppConfig:
    load_dotenv(BASE_DIR / ".env")

    if config_path is None:
        config_path = BASE_DIR / "config" / "settings.yaml"

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    t = raw.get("trading", {})
    trading = TradingConfig(
        initial_balance=t.get("initial_balance", 10000.0),
        risk_per_trade=t.get("risk_per_trade", 0.02),
        signal_confidence_threshold=t.get("signal_confidence_threshold", 0.55),
        max_concurrent_positions=t.get("max_concurrent_positions", 3),
        lookback_days=t.get("lookback_days", 90),
        ohlcv_interval=t.get("ohlcv_interval", "1h"),
        news_weight=t.get("news_weight", 0.40),
        price_weight=t.get("price_weight", 0.60),
        signal_deadband=t.get("signal_deadband", 0.15),
        min_lot_size=t.get("min_lot_size", 1000.0),
        lot_unit=t.get("lot_unit", 1000.0),
        trading_mode=t.get("trading_mode", "paper"),
    )

    pairs = [
        PairConfig(
            symbol=p["symbol"],
            display_name=p["display_name"],
            pip_value=p["pip_value"],
            base_currency=p.get("base_currency", ""),
            quote_currency=p.get("quote_currency", ""),
            enabled=p.get("enabled", True),
        )
        for p in raw.get("pairs", [])
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
        max_bytes=lg.get("max_bytes", 10_485_760),
        backup_count=lg.get("backup_count", 5),
    )

    nc = raw.get("news_collection", {})
    news_collection = NewsCollectionConfig(
        interval_minutes=nc.get("interval_minutes", 30),
        timezone=nc.get("timezone", "Asia/Tokyo"),
        inter_pair_delay_seconds=nc.get("inter_pair_delay_seconds", 60),
        news_freshness_hours=nc.get("news_freshness_hours", 24.0),
    )

    _default_ns = NewsSourcesConfig()
    ns = raw.get("news_sources", {})
    news_sources = NewsSourcesConfig(
        feeds_fx=ns.get("feeds_fx", _default_ns.feeds_fx),
        feeds_global=ns.get("feeds_global", _default_ns.feeds_global),
        feeds_japan=ns.get("feeds_japan", _default_ns.feeds_japan),
        jpy_keywords=ns.get("jpy_keywords", _default_ns.jpy_keywords),
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
        host=api_raw.get("host", "0.0.0.0"),
        port=api_raw.get("port", 8811),
    )

    pm = raw.get("price_monitor", {})
    price_monitor = PriceMonitorConfig(
        enabled=pm.get("enabled", True),
        interval_minutes=pm.get("interval_minutes", 5),
        alert_threshold_pct=pm.get("alert_threshold_pct", 0.003),
        alert_step_pct=pm.get("alert_step_pct", 0.002),
        emergency_close_pct=pm.get("emergency_close_pct", 0.008),
        enable_emergency_close=pm.get("enable_emergency_close", False),
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

    return AppConfig(
        trading=trading,
        pairs=pairs,
        schedule=schedule,
        logging=log_cfg,
        news_collection=news_collection,
        news_sources=news_sources,
        rag=rag,
        notifier=notifier,
        price_monitor=price_monitor,
        api=api_cfg,
        llm=llm_cfg,
        gemini=gemini,
        openai=openai_cfg,
        claude=claude_cfg,
    )
