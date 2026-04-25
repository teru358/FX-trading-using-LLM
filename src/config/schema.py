"""設定用 dataclass 定義。

load_config (loader.py) がここで定義された dataclass を YAML から組み立てる。
外部公開は `src.config` パッケージ (__init__.py) から再エクスポートされる。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent.parent

# Ollama のデフォルトモデル（llm.<role>.model が空の場合に使用）
_DEFAULT_OLLAMA_MODEL = "llama3.1:8b"


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
class LlamaCppBaseConfig:
    """llama.cpp (llama-swap) 接続設定。OpenAI 互換 /v1 エンドポイントを前提。

    timeout_seconds はモデル初回ロード時間も含めるため長めに設定 (既定 300 秒)。
    llama-swap がリクエスト時に対象モデルをロードするため、初回は数十秒かかる。
    """
    base_url: str = "http://localhost:8080/v1"
    timeout_seconds: int = 300
    max_retries: int = 2


@dataclass
class ClaudeCliBaseConfig:
    """Claude Code CLI (`claude -p`) 接続設定。サブスクプラン利用時の subprocess 呼び出し。

    前提: `claude` CLI が PATH に存在し、`claude login` で認証済み。
    isolated_cwd: CLAUDE.md / skills 汚染を避けるための隔離ディレクトリ (空ディレクトリ推奨)。
    """
    command: str = "claude"
    isolated_cwd: str = ""
    timeout_seconds: int = 120
    max_retries: int = 2
    extra_args: list[str] = field(default_factory=list)


@dataclass
class TradingConfig:
    initial_balance: float = 10000.0
    risk_per_trade: float = 0.02
    signal_confidence_threshold: float = 0.55
    min_rr_ratio: float = 0.0           # >0 で有効化: planned R:R がこの値未満なら hold
    lookback_days: int = 90
    ohlcv_interval: str = "1h"
    news_weight: float = 0.20
    price_weight: float = 0.80
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
    # RAG方向別スコア補正
    rag_adjustment_enabled: bool = True
    rag_adjustment_max: float = 0.15
    rag_adjustment_min_hits: int = 2
    rag_adjustment_search_top_n: int = 5
    rag_adjustment_same_weight: float = 0.10
    rag_adjustment_opposite_weight: float = 0.10
    rag_adjustment_trade_multiplier: float = 1.0
    rag_adjustment_forecast_multiplier: float = 0.5
    rag_adjustment_hold_multiplier: float = 0.3
    # ATRベースSL/TP
    atr_timeframe: str = "4h"            # ATR 計算用の足種 ("1h" / "4h" / "1d")。テクニカル分析の ohlcv_interval とは独立
    sl_atr_mult_default: float = 3.0
    tp_atr_mult_default: float = 6.0
    sl_atr_mult_min: float = 1.0
    sl_atr_mult_max: float = 5.0
    tp_atr_mult_min: float = 2.0
    tp_atr_mult_max: float = 10.0
    # TradingView テクニカルサマリー (矛盾検出)
    tv_summary_enabled: bool = False
    tv_conflict_dampen: float = 0.7    # TV判定と方向が矛盾時のconfidence倍率
    # ポートフォリオリスク管理
    max_total_positions: int = 4                 # 全体の最大同時ポジション数
    max_positions_per_currency_group: int = 2    # 通貨グループ別の最大ポジション数
    max_same_direction_per_group: int = 2        # グループ内同方向の最大ポジション数
    # Drawdown kill switch (新規エントリーのみ停止、既存ポジションは保持)
    drawdown_kill_switch_enabled: bool = False
    drawdown_kill_switch_max_pct: float = 0.10   # peak からこの割合以上落ちたら新規停止 (例: 0.10 = 10%)
    drawdown_kill_switch_lookback_days: int = 0  # 0 = 全期間、>0 = 直近 N 日のクローズトレードのみ参照
    # ボラレジーム (EWMA ベース position sizing)
    vol_regime_enabled: bool = False
    vol_regime_ewma_span: int = 20           # ATR の EWMA 期間 (bar 数)
    vol_regime_high_threshold: float = 1.3   # ATR/EWMA > この値 → high vol
    vol_regime_low_threshold: float = 0.7    # ATR/EWMA < この値 → low vol
    vol_regime_high_risk_scale: float = 0.5  # high vol 時の risk_per_trade 倍率
    vol_regime_low_risk_scale: float = 1.0   # low vol 時 (デフォルト = 等倍、拡大はリスク増なので慎重に)

    # Forecast accuracy auto-feedback (直近サイクルの予測精度を signal に反映)
    forecast_accuracy_feedback: "ForecastAccuracyFeedbackConfig" = field(
        default_factory=lambda: ForecastAccuracyFeedbackConfig()
    )


@dataclass
class ForecastAccuracyFeedbackConfig:
    """ForecastStore の直近予測精度を signal の confidence/action に反映する設定。

    予測 hit 率 (predicted_direction × latest_price_delta の符号一致率) が
    soft_threshold 未満なら confidence × confidence_penalty、
    hard_threshold 未満なら action="hold" を強制する。
    サンプル数が min_samples 未満の場合は適用しない (false positive 防止)。
    """
    enabled: bool = False
    lookback_hours: int = 24
    min_samples: int = 4              # 適用に必要な reviewed forecast 数の下限
    soft_threshold: float = 0.50      # この値未満で confidence ペナルティ
    hard_threshold: float = 0.33      # この値未満で action=hold 強制
    confidence_penalty: float = 0.7   # soft 適用時の confidence 乗数


@dataclass
class NewsCollectionConfig:
    interval_minutes: int = 30
    offset_minutes: int = 0              # 開始オフセット（15 → :15, :45）
    timezone: str = "Asia/Tokyo"
    inter_pair_delay_seconds: int = 60
    news_freshness_hours: float = 24.0   # この時間以上古い記事は除外
    summary_max_chars: int = 600         # RSS summary の切り捨て文字数


@dataclass
class EconomicCalendarConfig:
    """経済指標カレンダー設定。"""
    enabled: bool = False
    fetch_time: str = "06:00"
    fetch_timezone: str = "Asia/Tokyo"
    lookahead_hours: int = 48
    currencies: list[str] = field(default_factory=lambda: ["USD", "JPY", "EUR", "GBP"])
    min_importance: int = 0
    post_event_window_min: int = 30
    post_event_impact_min: int = 1
    refresh_lookback_min: int = 60


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
class KeywordsConfig:
    """カテゴリ別ニュースフィルタキーワード。"""
    global_keywords: list[str] = field(default_factory=lambda: [
        "forex", "fx", "currency", "central bank", "economy", "inflation",
        "rate", "geopolit", "oil", "gold", "bond", "yield", "trade war",
        "tariff", "dollar", "euro", "yen", "sterling", "fed", "ecb", "boe", "boj",
    ])
    japan_keywords: list[str] = field(default_factory=lambda: [
        "boj", "bank of japan", "nippon ginko", "japan", "japanese",
        "yen", "jpy", "ueda",
        "日銀", "円安", "円高", "金利", "為替", "利上げ", "利下げ", "植田", "円",
    ])


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
    feedly: FeedlyConfig = field(default_factory=FeedlyConfig)


@dataclass
class RagConfig:
    db_path: str = "data/rag"
    embedding_model: str = "nomic-embed-text"
    # embedding プロバイダー: "ollama" | "llamacpp"
    # "ollama"   — Ollama の /api/embeddings を使用 (従来互換)
    # "llamacpp" — llama-swap の /v1/embeddings (OpenAI 互換) を使用
    embedding_provider: str = "ollama"
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
    trailing_stop_breakeven_pct: float = 0.20   # TP距離のこの割合でSL=entry。半分(pct/2)でSL=中間
    trailing_stop_activation_pct: float = 0.40  # TP距離のこの割合以降は動的追従（current - 元SL距離×ratio）
    trailing_stop_distance_ratio: float = 1.0   # 動的追従時のSL距離倍率(元SL距離×この値)


@dataclass
class TwelveDataConfig:
    """Twelve Data API 設定。"""
    daily_limit: int = 800
    per_minute_limit: int = 8
    watch_symbols: list[str] = field(default_factory=list)  # Twelve Dataで取得するwatch銘柄
    use_for_monitor: bool = True  # price_monitorでもTwelve Dataを使うか


@dataclass
class PriceProviderConfig:
    """価格データプロバイダー設定。"""
    realtime_provider: str = "yfinance"  # "yfinance" | "twelvedata"
    twelvedata: TwelveDataConfig = field(default_factory=TwelveDataConfig)


@dataclass
class NotifierConfig:
    enabled: bool = False                 # true で Discord 通知を有効化
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
class TimeframeConfig:
    """MTF の 1 つのタイムフレーム設定。"""
    lookback_days: int
    interval: str                  # "1h" | "4h" | "8h" | "1d" 等
    enabled: bool = True


@dataclass
class MultiTimeframeConfig:
    """選択的 MTF 分析の設定。

    各 TF は compute_indicators() をそれぞれ呼び出し、対応する subset
    (long=regime / medium=structure / short=full) で user 設定を
    絞り込んで計算する。weights は rule-based tech_score 合成時に使用
    (欠落 TF は自動的に除外して再正規化)。
    """
    enabled: bool = True             # 全体有効/無効スイッチ
    long: TimeframeConfig = field(default_factory=lambda: TimeframeConfig(
        lookback_days=90, interval="1d", enabled=True,
    ))
    medium: TimeframeConfig = field(default_factory=lambda: TimeframeConfig(
        lookback_days=14, interval="4h", enabled=True,
    ))
    short: TimeframeConfig = field(default_factory=lambda: TimeframeConfig(
        lookback_days=2, interval="1h", enabled=True,
    ))
    weights: dict[str, float] = field(default_factory=lambda: {
        "long": 0.40, "medium": 0.35, "short": 0.25,
    })


@dataclass
class AnalysisConfig:
    """分析手法の有効/無効設定。"""
    indicators: IndicatorToggleConfig = field(default_factory=IndicatorToggleConfig)
    chart_patterns: ChartPatternConfig = field(default_factory=ChartPatternConfig)
    # 選択的 MTF 分析設定 (enabled=False で従来の単一 TF 動作に fallback)
    multi_timeframe: MultiTimeframeConfig = field(default_factory=MultiTimeframeConfig)
    forecast_review_interval_hours: int = 8        # B: 予測検証ウィンドウ（時間）
    forecast_start_hour: int = 0                   # 予測サイクル開始時刻オフセット（0〜23）
    forecast_min_combined_score: float = 0.15      # C: 予測生成の最低スコア閾値（±）
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
    llamacpp: LlamaCppBaseConfig = field(default_factory=LlamaCppBaseConfig)
    claude_cli: ClaudeCliBaseConfig = field(default_factory=ClaudeCliBaseConfig)
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
    run_trade_soft_timeout_sec: float = 10.0  # POST /run/trade: この秒数内に完了したら同期返答、超過で非同期化
    ask_soft_timeout_sec: float = 60.0         # POST /ask: 同上


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/finance.log"
    activity_log_file: str = "logs/activity.log"
    rotate_timing: str = "10MB"   # サイズ: 10MB / 時間: 6H / 1D / midnight / W0〜W6
    backup_count: int = 5


@dataclass
class TradingViewConfig:
    """TradingView Desktop 連携設定。"""
    enabled: bool = False
    cdp_host: str = "localhost"
    cdp_port: int = 9222


@dataclass
class WeeklyDiagnosisConfig:
    """週次自己診断レポート設定。

    FX 市場の休場 (土日) に合わせ、過去 N 日のパフォーマンス・予測精度・
    LLM 使用量・現状設定を集約して Claude に診断させ、
    Markdown レポート + Discord embed で配信する。
    """
    enabled: bool = False
    # schedule (UTC ではなく news_timezone を使う = 設定の他箇所と統一)
    weekday: str = "saturday"             # monday / ... / sunday
    at_time: str = "09:00"                # HH:MM (news_timezone)
    lookback_days: int = 7                # 集計対象期間
    # role: 既存 LLM client ロールから流用 (claude-cli を選んでおけば自動で claude)
    llm_role: str = "reflection"
    output_dir: str = "reports"           # プロジェクトルート相対 / 自動 mkdir
    notify_discord: bool = True           # 完了時に Discord embed 通知


@dataclass
class DataBackupConfig:
    """data/ ディレクトリの定期バックアップ設定。

    sync 失敗・破損から復旧できるよう zip 化して世代保存する。
    保存先はプロジェクトルート相対のディレクトリ。
    """
    enabled: bool = True
    at_time: str = "03:10"                # HH:MM (news_timezone)
    output_dir: str = "backup/data"       # プロジェクトルート相対 / 自動 mkdir
    retention_count: int = 30             # 保持世代数 (古いものから削除)


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
    price_provider: PriceProviderConfig = field(default_factory=PriceProviderConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    keywords: KeywordsConfig = field(default_factory=KeywordsConfig)
    economic_calendar: EconomicCalendarConfig = field(default_factory=EconomicCalendarConfig)
    tradingview: TradingViewConfig = field(default_factory=TradingViewConfig)
    weekly_diagnosis: "WeeklyDiagnosisConfig" = field(
        default_factory=lambda: WeeklyDiagnosisConfig()
    )
    data_backup: "DataBackupConfig" = field(
        default_factory=lambda: DataBackupConfig()
    )

    @property
    def state_dir(self) -> Path:
        return BASE_DIR / "data" / "state"

    @property
    def manual_state_dir(self) -> Path:
        return BASE_DIR / "data" / "manual_state"

    @property
    def rag_db_path(self) -> Path:
        return BASE_DIR / self.rag.db_path

    @property
    def prices_db_path(self) -> Path:
        return BASE_DIR / "data" / "prices.db"

    @property
    def econ_db_path(self) -> Path:
        return BASE_DIR / "data" / "econ_events.db"

    @property
    def user_notes_path(self) -> Path:
        return BASE_DIR / "config" / "user_notes.md"

    @property
    def config_dir(self) -> Path:
        return BASE_DIR / "config"

    @property
    def audit_output_dir(self) -> Path:
        return BASE_DIR / "docs" / "audit"

    @property
    def audit_lessons_path(self) -> Path:
        return self.config_dir / "audit_lessons.md"

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
