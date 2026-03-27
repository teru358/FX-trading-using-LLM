from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent

# Ollama のデフォルトモデル（llm.<role>.model が空の場合に使用）
_DEFAULT_OLLAMA_MODEL = "llama3.1:8b"

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


@dataclass
class InstrumentConfig:
    """FX通貨ペア・株価指数など全銘柄の統一設定。

    asset_type:
      "fx"    — FX通貨ペア（pip_value / base_currency / quote_currency を使用）
      "index" — 株価指数（currency を使用）

    mode:
      "trade" — テクニカル収集 + 取引シグナル生成・発注（fx のみ有効）
      "watch" — テクニカル収集のみ（参照銘柄）
    """
    symbol: str
    display_name: str
    asset_type: str = "fx"          # "fx" | "index"
    mode: str = "trade"             # "trade" | "watch"
    enabled: bool = True
    # FX 専用
    pip_value: float = 0.0
    base_currency: str = ""
    quote_currency: str = ""
    # 非FX 用
    currency: str = ""

    @property
    def is_tradeable(self) -> bool:
        return self.mode == "trade" and self.asset_type == "fx"

    @property
    def currencies(self) -> tuple[str, str]:
        return (self.base_currency, self.quote_currency)

    @property
    def related_currencies(self) -> list[str]:
        """この銘柄に関連する通貨コード。"""
        if self.asset_type == "fx":
            return [c for c in (self.base_currency, self.quote_currency) if c]
        return [self.currency] if self.currency else []

    @property
    def news_categories(self) -> list[str]:
        """銘柄タイプに応じたニュースカテゴリを返す。"""
        cats = ["global"]
        if self.asset_type == "fx":
            cats.append("fx")
        elif self.asset_type == "index":
            cats.append("equity")
        if "JPY" in self.related_currencies:
            cats.append("japan")
        return cats


# 後方互換エイリアス
PairConfig = InstrumentConfig


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
    lookback_days: int = 90
    ohlcv_interval: str = "1h"
    news_weight: float = 0.40
    price_weight: float = 0.60
    signal_deadband: float = 0.15
    min_lot_size: float = 1000.0
    lot_unit: float = 1000.0
    trading_mode: str = "paper"
    # ポジション再評価（Phase 4a）
    position_review_enabled: bool = False
    reversal_confidence_min: float = 0.70    # Layer 1: 反転シグナルの最低信頼度
    reversal_score_threshold: float = 0.25   # Layer 1: 反転シグナルの最低スコア絶対値
    max_holding_days: int = 10               # Layer 2: 最大保有日数
    timeout_min_progress_pct: float = 0.30   # Layer 2: タイムアウト判定の最低進捗率
    profit_lock_min_progress_pct: float = 0.40  # Layer 3: 利益ロック発動の最低進捗率
    profit_lock_score_floor: float = 0.15    # Layer 3: この絶対値未満で利益ロック


@dataclass
class NewsCollectionConfig:
    interval_minutes: int = 30
    timezone: str = "Asia/Tokyo"
    inter_pair_delay_seconds: int = 60
    news_freshness_hours: float = 24.0   # この時間以上古い記事は除外
    summary_max_chars: int = 600         # RSS summary の切り捨て文字数


@dataclass
class FeedlyConfig:
    """Feedly API 設定。

    access_token は .env の FEEDLY_ACCESS_TOKEN から読み込む（load_config で解決）。

    Stream ID の確認:
      GET https://cloud.feedly.com/v3/categories
      Authorization: Bearer <access_token>
      → 各カテゴリの "id" フィールド（例: "user/xxx/category/FX"）
    """
    enabled: bool = False
    access_token: str = ""
    streams_fx: list[str] = field(default_factory=list)
    streams_global: list[str] = field(default_factory=list)
    streams_japan: list[str] = field(default_factory=list)
    count: int = 20  # 1ストリームあたりの取得件数上限

    def streams_for(self, category: str) -> list[str]:
        """カテゴリ名から対応するストリームIDリストを返す。"""
        return {
            "fx": self.streams_fx,
            "global": self.streams_global,
            "japan": self.streams_japan,
        }.get(category, [])


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
    feedly: FeedlyConfig = field(default_factory=FeedlyConfig)


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
    # トレーリングストップ
    trailing_stop_enabled: bool = False
    trailing_stop_activation_pct: float = 0.40  # TP距離の40%到達で発動
    trailing_stop_distance_ratio: float = 1.0   # 元のSL距離と同比率でSLを追従


@dataclass
class NotifierConfig:
    notifier: str = "none"
    notify_on_order_open: bool = True
    notify_on_order_close: bool = True
    notify_on_signal_skipped: bool = True
    notify_on_price_alert: bool = True    # 価格急変動通知


@dataclass
class IndicatorToggleConfig:
    """テクニカル指標の有効/無効スイッチ。"""
    moving_averages: bool = True
    rsi: bool = True
    macd: bool = True
    bollinger_bands: bool = True
    atr: bool = True
    adx: bool = True
    ichimoku: bool = True


@dataclass
class ChartPatternConfig:
    """チャートパターン検出の有効/無効スイッチ。"""
    # ローソク足パターン
    hammer: bool = True
    shooting_star: bool = True
    engulfing: bool = True
    doji: bool = True
    morning_evening_star: bool = True
    three_soldiers_crows: bool = True
    pin_bar: bool = True
    inside_bar: bool = True
    # チャート形状
    double_top_bottom: bool = False
    head_shoulders: bool = False
    triangle: bool = False
    range_bound: bool = False
    # ブレイクアウト系
    bb_squeeze: bool = False
    atr_contraction: bool = False
    sr_breakout: bool = False


@dataclass
class AnalysisConfig:
    """分析手法の有効/無効設定。"""
    indicators: IndicatorToggleConfig = field(default_factory=IndicatorToggleConfig)
    chart_patterns: ChartPatternConfig = field(default_factory=ChartPatternConfig)
    forecast_review_interval_hours: int = 8        # B: 予測検証ウィンドウ（時間）
    forecast_start_hour: int = 0                   # 予測サイクル開始時刻オフセット（0〜23）
    forecast_min_combined_score: float = 0.25      # C: 予測生成の最低スコア閾値（±）
    forecast_significance_atr_ratio: float = 0.30  # A: 有意性判定の ATR proxy 比率


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
    port: int = 8811


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/finance.log"
    activity_log_file: str = "logs/activity.log"
    rotate_timing: str = "10MB"   # サイズ: 10MB / 時間: 6H / 1D / midnight / W0〜W6
    backup_count: int = 5


@dataclass
class AppConfig:
    trading: TradingConfig
    instruments: list[InstrumentConfig]
    schedule: ScheduleConfig
    logging: LoggingConfig
    news_collection: NewsCollectionConfig
    news_sources: NewsSourcesConfig
    rag: RagConfig
    notifier: NotifierConfig
    price_monitor: PriceMonitorConfig = field(default_factory=PriceMonitorConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
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
    def enabled_instruments(self) -> list[InstrumentConfig]:
        """有効な全銘柄。"""
        return [i for i in self.instruments if i.enabled]

    @property
    def tradeable_instruments(self) -> list[InstrumentConfig]:
        """取引対象の銘柄（mode=trade かつ asset_type=fx）。"""
        return [i for i in self.enabled_instruments if i.is_tradeable]

    @property
    def watch_only_instruments(self) -> list[InstrumentConfig]:
        """監視専用の銘柄（指数・参照FXペア等）。"""
        return [i for i in self.enabled_instruments if not i.is_tradeable]

    @property
    def enabled_pairs(self) -> list[InstrumentConfig]:
        """後方互換: tradeable_instruments のエイリアス。"""
        return self.tradeable_instruments


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
        lookback_days=t.get("lookback_days", 90),
        ohlcv_interval=t.get("ohlcv_interval", "1h"),
        news_weight=t.get("news_weight", 0.40),
        price_weight=t.get("price_weight", 0.60),
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
        timezone=nc.get("timezone", "Asia/Tokyo"),
        inter_pair_delay_seconds=nc.get("inter_pair_delay_seconds", 60),
        news_freshness_hours=nc.get("news_freshness_hours", 24.0),
        summary_max_chars=nc.get("summary_max_chars", 600),
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
        jpy_keywords=ns.get("jpy_keywords", _default_ns.jpy_keywords),
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
        trailing_stop_activation_pct=pm.get("trailing_stop_activation_pct", 0.40),
        trailing_stop_distance_ratio=pm.get("trailing_stop_distance_ratio", 1.0),
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
        forecast_min_combined_score=an.get("forecast_min_combined_score", 0.25),
        forecast_significance_atr_ratio=an.get("forecast_significance_atr_ratio", 0.30),
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
        api=api_cfg,
        llm=llm_cfg,
        gemini=gemini,
        openai=openai_cfg,
        claude=claude_cfg,
        analysis=analysis_cfg,
    )
