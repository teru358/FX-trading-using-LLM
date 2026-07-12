"""設定用 dataclass 定義。

load_config (loader.py) がここで定義された dataclass を YAML から組み立てる。
外部公開は `src.config` パッケージ (__init__.py) から再エクスポートされる。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

BASE_DIR = Path(__file__).parent.parent.parent

# 利用可能な LLM プロバイダー名 (loader バリデーションで使用)
LLM_PROVIDERS = ("ollama", "llamacpp", "claude-cli", "gemini", "openai", "claude")

# base_url が必須な provider (空欄なら起動時 ConfigError)
LLM_PROVIDERS_REQUIRING_BASE_URL = ("ollama", "llamacpp")

# tick migration の段階 (off→producer→protect_shadow→protect_live の単調列、順序維持)
VALID_TICK_MIGRATION_STAGES = ("off", "producer", "protect_shadow", "protect_live")


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
class ProviderConfig:
    """全プロバイダー共通の接続設定。

    意味のあるフィールドは provider に依存する (使われない値は無視):
      base_url        — ollama / llamacpp で必須 (空欄は ConfigError)
      command         — claude-cli の実行コマンド (空欄なら "claude" を補完)
      isolated_cwd    — claude-cli の隔離 cwd (CLAUDE.md/skills 汚染回避用、空欄なら警告)
      extra_args      — claude-cli の追加引数
      max_tokens      — claude (API) の応答最大トークン (0 なら 4096 補完)
      timeout_seconds — 共通: HTTP/プロセスタイムアウト
      max_retries     — 共通: リトライ回数
      max_concurrent  — ollama 等の同時呼び出し上限 (Semaphore に渡す)
    """
    base_url: str = ""
    command: str = ""
    isolated_cwd: str = ""
    extra_args: list[str] = field(default_factory=list)
    max_tokens: int = 0
    timeout_seconds: int = 120
    max_retries: int = 2
    max_concurrent: int = 2


@dataclass
class TradingConfig:
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
    # ポジション再評価（Phase 4a）
    position_review_enabled: bool = False
    reversal_confidence_min: float = 0.70    # Layer 1: 反転シグナルの最低信頼度
    reversal_score_threshold: float = 0.25   # Layer 1: 反転シグナルの最低スコア絶対値
    reversal_min_holding_minutes: int = 240  # Layer 1: 反転発火の最低保有時間 (分)
    remote_sl_sync_enabled: bool = False     # Layer 4: trailing SL を MT5 server-side に同期
    # ポジション管理 v2 (2026-06-13 layer rework)
    reversal_guard_enabled: bool = True
    reversal_close_enabled: bool = False
    reversal_consecutive_required: int = 2
    reversal_raise_sl_to_breakeven: bool = True
    time_stop_enabled: bool = True
    no_progress_enabled: bool = True
    no_progress_watch_hours: int = 6
    no_progress_exit_hours: int = 12
    no_progress_min_mfe_r: float = 0.1
    no_progress_requires_signal_weakness: bool = True
    stale_position_review_hours: int = 24
    timeout_cooldown_hours: int = 4
    stale_signal_hours: int = 8
    session_end_flatten_enabled: bool = False
    profit_protection_enabled: bool = True
    protect_half_r: float = 0.3
    protect_breakeven_r: float = 0.5
    protect_lock_r: float = 1.0
    giveback_close_r: float = 0.4
    giveback_close_min_mfe_r: float = 0.8
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
    # Scale-in (順張り増し玉): 既存ポジと同方向で強い signal が来た時に追加発注
    scale_in_enabled: bool = False
    scale_in_conf_margin: float = 0.05    # confidence 上回り幅
    scale_in_score_margin: float = 0.05   # |combined_score| 上回り幅
    # ペアごとのポジション数上限 (旧 max_total_positions / per_currency_group / same_direction を統合)
    max_positions_per_pair: int = 2
    # Drawdown kill switch (新規エントリーのみ停止、既存ポジションは保持)
    drawdown_kill_switch_enabled: bool = False
    drawdown_kill_switch_max_pct: float = 0.10   # peak からこの割合以上落ちたら新規停止 (例: 0.10 = 10%)
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
    # Deep fetch (新規記事のみ trafilatura で本文取得)
    deep_fetch_enabled: bool = True
    deep_fetch_timeout_seconds: float = 8.0     # 1 記事あたりの HTTP タイムアウト
    deep_fetch_max_chars: int = 3000            # 本文の切詰上限
    deep_fetch_max_concurrent: int = 3          # 同時 HTTP fetch 上限
    deep_fetch_user_agent: str = "finance-news-collector/1.0"


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
    # embedding プロバイダーの接続先。空欄なら起動時 ConfigError。
    # LLM 用 provider_config.base_url とは独立 (LLM が claude-cli でも embedding は ollama を使えるため)。
    embedding_base_url: str = ""
    news_lookback_hours: int = 24
    retrieval_top_k: int = 5
    reflection_lookback_count: int = 3
    analysis_lookback_hours: int = 8


@dataclass
class ScheduleConfig:
    run_times: list[str] = field(default_factory=lambda: ["15:00", "21:00"])
    timezone: str = "Asia/Tokyo"
    # technical 収集の間隔 (時間)。既定は現状維持 = 毎時 (1h)。
    # trade は cadence resolver で boost される土台、watch は低頻度固定用。
    technical_trade_interval_hours: int = 1
    technical_watch_interval_hours: int = 1
    # technical trade 収集の分粒度 interval。設定時は technical_trade_interval_hours
    # より優先。None (既定) なら従来の hours を使う = 挙動不変 (spec 2026-07-05 S-4c)。
    technical_trade_interval_minutes: int | None = None
    # cadence resolver による可変 interval 収集 (§5.3/§5.6, Phase1 Task B)。既定 false で
    # 現行の union-time dispatch を維持 (後方互換)。true で cadence_driver に切替。
    cadence_enabled: bool = False
    # boost 中の収集間隔 (分)。econ/state 経路が窓内でこの間隔まで頻度を上げる。
    cadence_boost_interval_minutes: int = 5
    # FX technical 鮮度閾値 (分)。既定 360 = 従来の 6h 定数と等価 (挙動不変)。
    # day horizon では 90 に短縮する (spec 2026-07-05 S-3)。watch 側 (120h) は定数のまま。
    technical_max_staleness_fx_minutes: int = 360
    # technical collector の pair 間待機 (秒)。LLM ペーシングは廃止され provider
    # (yfinance 等) レート緩和のみが目的のため news 側 (60s) と分離し既定 5s。
    technical_inter_pair_delay_seconds: int = 5

    def __post_init__(self) -> None:
        if not (0 <= self.technical_inter_pair_delay_seconds <= 60):
            raise ValueError(
                f"schedule.technical_inter_pair_delay_seconds must be in [0, 60], "
                f"got {self.technical_inter_pair_delay_seconds!r}"
            )

    def effective_trade_interval_seconds(self) -> int:
        """cadence base 用の有効 trade 収集間隔 (秒)。minutes 優先。"""
        if self.technical_trade_interval_minutes:
            return self.technical_trade_interval_minutes * 60
        return self.technical_trade_interval_hours * 3600


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
class TwelveDataConfig:
    """Twelve Data API 設定 (paper_provider=twelvedata のとき必須)。"""
    daily_limit: int = 800
    per_minute_limit: int = 8
    use_for_monitor: bool = True
    indices: list[str] = field(default_factory=list)  # TD で取得検証済 ETF / index


@dataclass
class NotifierConfig:
    enabled: bool = False                 # true で Discord 通知を有効化
    notify_on_order_open: bool = True
    notify_on_order_close: bool = True
    notify_on_signal_skipped: bool = True
    notify_on_price_alert: bool = True    # 価格急変動通知
    notify_on_cycle_summary: bool = True  # 取引サイクル結果を1通に集約 (false で旧 per-event 通知)


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
class LLMRoleConfig:
    """1 つの分析ロールが使用するモデル名と温度。

    provider は LLMConfig.provider に統一される (役割ごとの provider 切替は廃止)。
    model 空欄は loader でバリデートされ、provider と整合しないモデル名は実行時に検出される。
    """
    model: str = ""
    temperature: float = 0.2


@dataclass
class AgentLlmConfig:
    """1 agent の LLM 設定 (config/agents.yaml の agents.<name>)。

    provider 空欄なら fallback (既存役割 model)。provider 指定時は
    LLMConfig.provider_configs[provider] から接続設定を引く。
    """
    provider: str = ""        # 空欄 = fallback
    model: str = ""
    temperature: float = 0.2


@dataclass
class AgentLlm:
    """agent に注入する LLM ハンドル (client + 解決済 temperature)。

    temperature は agent の chat() 呼出ごとの引数なので、client 単体では
    agent 別 temperature が届かない。factory が解決済 temperature を束ねて渡す。
    client の型は llm パッケージ依存を避けるため文字列注釈 ("LLMClient")。
    """
    client: "LLMClient"        # noqa: F821  (実体は src.llm.client.LLMClient)
    temperature: float


@dataclass
class OrchestratorAgentsLlmConfig:
    """5 agent の LLM 設定 (config/agents.yaml の agents:)。

    未指定 agent は AgentLlmConfig 既定 (provider 空 = fallback)。
    既存 OrchestratorAgentsConfig (*_enabled = 「動かすか」) とは別物で、
    こちらは「どの LLM を使うか」を表す。
    """
    planner: AgentLlmConfig = field(default_factory=AgentLlmConfig)
    news: AgentLlmConfig = field(default_factory=AgentLlmConfig)
    technical: AgentLlmConfig = field(default_factory=AgentLlmConfig)
    execution_opinion: AgentLlmConfig = field(default_factory=AgentLlmConfig)
    context_summary: AgentLlmConfig = field(default_factory=AgentLlmConfig)


@dataclass
class LLMConfig:
    """LLM 設定の単一エントリポイント。

    provider と provider_config を 1 つだけ持ち、3 役割すべてが同じ provider を使う。
    各役割は model と temperature だけを個別指定する。
    role_overrides 等の特殊機構は意図的に持たない (シンプル化方針)。
    """
    provider: str = "claude-cli"
    provider_config: ProviderConfig = field(default_factory=ProviderConfig)
    news_analysis: LLMRoleConfig = field(
        default_factory=lambda: LLMRoleConfig(temperature=0.3)
    )
    price_analysis: LLMRoleConfig = field(
        default_factory=lambda: LLMRoleConfig(temperature=0.1)
    )
    reflection: LLMRoleConfig = field(
        default_factory=lambda: LLMRoleConfig(temperature=0.3)
    )
    # provider 名 → 接続設定 (orchestrator agent 用)。既存 provider_config (単一) は
    # 後方互換 fallback として維持。agents.yaml で別 provider を使う agent がこれを参照。
    provider_configs: dict[str, ProviderConfig] = field(default_factory=dict)


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
class Mt5Config:
    """MT5 bridge 設定 (live_broker=mt5 のとき必須)。

    旧 Mt5BridgeConfig を providers/mt5.yaml に再構成。
    enabled フラグは廃止 (live_broker=mt5 が明示的な enable)。

    shadow_log_path / shadow_observer_state_dir は live_test モード
    (旧 shadow) で使う。フィールド名は内部 ShadowBrokerAdapter との
    対応で "shadow_" プレフィックスを維持。
    """
    bridge_url: str = ""
    api_key: str = ""                         # X-Bridge-Api-Key (空ならヘッダー送信なし)
    request_timeout_seconds: float = 5.0
    # 発注関連
    order_request_timeout_seconds: float = 10.0
    lot_size_units: int = 100_000
    magic_number: int = 12345
    # auto soft halt 閾値 (発注経路 REJECT のみ。bridge 不通検出は BridgeHealthGate)
    consecutive_reject_threshold: int = 3
    # BridgeHealthGate のリトライ間隔
    health_retry_after_sec: float = 60.0
    # live_test mode (旧 shadow) で使用
    shadow_log_path: str = "data/state/shadow_trades.jsonl"
    shadow_observer_state_dir: str = "data/shadow_state"


@dataclass
class OandaConfig:
    """OANDA REST API 設定 (live_broker=oanda のとき必須、未実装 placeholder)。"""
    account_id: str = ""
    environment: Literal["practice", "live"] = "practice"
    # api_key は環境変数 OANDA_API_KEY から


@dataclass
class ProvidersConfig:
    """プロバイダー別の詳細設定コンテナ。

    各フィールドは対応するプロバイダーが選択されている場合のみ非 None。
    例: paper_provider=twelvedata なら twelvedata は非 None、
        live_broker=mt5 なら mt5 は非 None。
    yfinance は設定不要 (デフォルト動作のみ)。
    """
    twelvedata: TwelveDataConfig | None = None
    mt5: Mt5Config | None = None
    oanda: OandaConfig | None = None


@dataclass
class OrchestratorPolicyConfig:
    """運用方針 (spec §4.6)。trade_horizon は構造化・advice_memo は参考程度。"""
    trade_horizon: str = "swing"   # day | swing
    advice_memo: str = ""          # 自然文助言メモ (hard gate 不可侵)


@dataclass
class OrchestratorMarketStateConfig:
    """market state 別の処理周期 + 遷移閾値 (spec §5.2 / §5.2.1)。"""
    # state 別の処理周期 (秒)。
    calm_seconds: int = 300
    normal_seconds: int = 60
    active_seconds: int = 30
    critical_seconds: int = 60
    # 遷移閾値 (§5.2.1, Phase1 Task C)。horizon 連動は below の overlay で上書き。
    # active_move_pct は **パーセント値** (0.15 = 0.15%)。runtime._move_pct も同じ % 単位で
    # 算出する (両者の単位を揃える — code review High#1)。
    active_move_pct: float = 0.15        # この変動率(%)超で → active (price_move_window 内)
    price_move_window_seconds: int = 300  # 変動率を測る窓
    spread_spike_pips: float = 4.0       # spread がこの pips 超で → critical
    sl_tp_near_pct: float = 0.1          # ポジションが SL/TP までこの割合以内で → critical
    # ヒステリシス (下げは安定継続を要求、§5.2.1)。
    calm_after_seconds: int = 600        # normal が継続したら calm へ落とす
    normal_after_seconds: int = 120      # active/critical 解消後 normal へ落とす猶予


@dataclass
class OrchestratorLlmConfig:
    """LLM 逐次実行・timeout (spec §4.2 / §5.1.1)。"""
    max_concurrent_jobs: int = 1   # 固定。2 以上にしない (sequential-by-design)
    planning_timeout_seconds: int = 180
    execution_recheck_timeout_seconds: int = 30


@dataclass
class OrchestratorLocksConfig:
    """lock TTL (spec §6 / §8.8)。"""
    pair_lock_ttl_seconds: int = 300
    decision_lock_ttl_seconds: int = 600
    order_lock_ttl_seconds: int = 120


@dataclass
class OrchestratorEntryConfig:
    """entry 感度・gate 閾値 (spec §12)。"""
    price_move_pct: float = 0.15
    spread_max_pips: float = 2.0
    news_impact_min: float = 0.5
    require_fresh_technical: bool = True
    max_quote_age_seconds: int = 10
    max_technical_age_seconds: int = 1800


@dataclass
class OrchestratorFiringConfig:
    """PlannerAgent 発火条件 (spec §5.4)。material landing + debounce + periodic floor。"""
    material_news_impact_min: float = 0.5
    material_bias_delta_min: float = 0.20
    material_direction_flip_min: float = 0.10  # direction 反転を material とみなす |bias| 閾値
    debounce_window_seconds: int = 180
    min_planning_interval_seconds: int = 1800

    def __post_init__(self) -> None:
        for name in ("material_news_impact_min", "material_bias_delta_min",
                     "material_direction_flip_min"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"orchestrator.firing.{name} must be in [0,1], got {v!r}")


@dataclass
class OrchestratorHindsightConfig:
    """trigger 後の判断品質後追い計測の設定 (spec §8.2 / plan Phase 4)。"""
    horizon_seconds: int = 86400        # 24h。audit_post_hoc の post_close_hours=24 と整合
    poll_interval_seconds: int = 600    # hindsight poll loop の評価間隔 (秒)


@dataclass
class OrchestratorNotificationsConfig:
    """shadow 専用 Discord 通知のフラグ (Phase 2 design §11/§12)。

    既存 cycle summary と分離した shadow 専用 channel への通知。`shadow_enabled` が
    マスタースイッチで、各イベント (plan lifecycle / triggered / hindsight / daily_summary)
    は個別 on/off できる。webhook は DISCORD_SHADOW_WEBHOOK_URL を使い、未設定なら
    既存の DISCORD_WEBHOOK_URL にフォールバックする (shadow_notifier 側で解決)。

    NOTE: `shadow_plan_created` は plan ライフサイクル通知 (created / rejected /
    superseded) を**まとめて** gate する。design §12 の config キーが `shadow_plan_created`
    1 本のため、3 種を 1 フラグに束ねる (個別 gate が要れば後でフラグを足す)。
    """
    shadow_enabled: bool = True
    shadow_plan_created: bool = True       # plan lifecycle: created / rejected / superseded
    shadow_triggered: bool = True
    shadow_hindsight: bool = True
    shadow_daily_summary: bool = True
    # daily summary を送る時刻 (HH:MM, schedule.timezone に従う)。1 日 1 回・この時刻を
    # 跨いだ最初の cycle で送る (§11 / Phase1 Task A-1)。
    daily_summary_time: str = "07:00"


@dataclass
class OrchestratorAgentsConfig:
    """各 agent の有効化フラグ (spec §12)。"""
    news_enabled: bool = True
    technical_enabled: bool = True
    execution_opinion_enabled: bool = True
    risk_enabled: bool = True
    audit_enabled: bool = True


@dataclass
class OrchestratorConfig:
    """orchestrator agent loop の設定 (spec §12)。既定は安全側 (enabled=false)。"""
    enabled: bool = False
    mode: str = "shadow"   # observe | shadow | live
    # approval gate (spec 2026-07-05): ON のとき plan の publish が pending_approval
    # になり、人間の承認 (API approve) を経てから active 化する。既定 OFF = 挙動不変。
    approval_gate: bool = False
    pairs: list[str] = field(default_factory=list)  # 空なら tradeable instruments を使う
    # market state 検知 (§4.8/§5.2, Phase1 Task C)。既定 false。enabled 時のみ state ループ
    # 起動 + cadence②/regime 接続。orchestrator.enabled とは独立に on/off できる。
    market_state_enabled: bool = False
    # Phase 2/D: tick migration 段階導入。off→producer→protect_shadow→protect_live の単調列。
    # producer 以上で quote-stream producer 起動 + watch 直読。protect_shadow 以上で保護 worker 起動。
    tick_migration_stage: str = "off"
    quote_stream_poll_seconds: int = 2
    # Task F: material change 時の ExecutionOpinionAgent 再点火 (発注直前)。既定 OFF
    # (まず決定的・高速執行で live 検証、spec §2 step2)。
    execution_opinion_recheck_enabled: bool = False
    # plan expires_at の上限クランプ (時間)。0 = 無効 (従来挙動)。day 運用では 8 を
    # 設定し、LLM が長すぎる TTL を出しても決定的に切り詰める (spec 2026-07-05 S-1)。
    plan_ttl_max_hours: int = 0
    policy: OrchestratorPolicyConfig = field(default_factory=OrchestratorPolicyConfig)
    market_state: OrchestratorMarketStateConfig = field(
        default_factory=OrchestratorMarketStateConfig
    )
    llm: OrchestratorLlmConfig = field(default_factory=OrchestratorLlmConfig)
    locks: OrchestratorLocksConfig = field(default_factory=OrchestratorLocksConfig)
    entry: OrchestratorEntryConfig = field(default_factory=OrchestratorEntryConfig)
    firing: OrchestratorFiringConfig = field(default_factory=OrchestratorFiringConfig)
    hindsight: OrchestratorHindsightConfig = field(
        default_factory=OrchestratorHindsightConfig
    )
    notifications: OrchestratorNotificationsConfig = field(
        default_factory=OrchestratorNotificationsConfig
    )
    agents: OrchestratorAgentsConfig = field(default_factory=OrchestratorAgentsConfig)

    def __post_init__(self) -> None:
        if self.tick_migration_stage not in VALID_TICK_MIGRATION_STAGES:
            raise ValueError(
                f"tick_migration_stage must be one of {VALID_TICK_MIGRATION_STAGES}, "
                f"got {self.tick_migration_stage!r}"
            )
        protect_stages = ("protect_shadow", "protect_live")
        if self.tick_migration_stage in protect_stages and not self.enabled:
            raise ValueError(
                f"tick_migration_stage={self.tick_migration_stage!r} requires "
                f"orchestrator.enabled=True (protection worker is built only when "
                f"the orchestrator runtime is enabled; otherwise SL protection would "
                f"be silently disabled)"
            )
        if self.quote_stream_poll_seconds < 1:
            raise ValueError(
                f"quote_stream_poll_seconds must be >= 1, "
                f"got {self.quote_stream_poll_seconds!r}"
            )
        # Task F (spec §5): 執行段の発注主体モード。observe は現状未使用で執行段を
        # 起動しないため F では shadow/live のみ許容。
        valid_modes = ("shadow", "live")
        if self.mode not in valid_modes:
            raise ValueError(
                f"OrchestratorConfig.mode must be one of {valid_modes}, got {self.mode!r}"
            )


@dataclass
class AppConfig:
    # ── トップレベル mode + provider 選択 (旧 trading.trading_mode + 暗黙判定を置換) ──
    mode: str = "paper"                           # "paper" | "live" | "live_test"
    paper_provider: str = "yfinance"              # "yfinance" | "twelvedata"
    live_broker: str | None = None                # "mt5" | "oanda" | None

    # ── 既存フィールド (順序維持) ──
    trading: TradingConfig = field(default_factory=TradingConfig)
    instruments: list[InstrumentConfig] = field(default_factory=list)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    news_collection: NewsCollectionConfig = field(default_factory=NewsCollectionConfig)
    news_sources: NewsSourcesConfig = field(default_factory=NewsSourcesConfig)
    rag: RagConfig = field(default_factory=RagConfig)
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    price_monitor: PriceMonitorConfig = field(default_factory=PriceMonitorConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    keywords: KeywordsConfig = field(default_factory=KeywordsConfig)
    economic_calendar: EconomicCalendarConfig = field(default_factory=EconomicCalendarConfig)
    weekly_diagnosis: WeeklyDiagnosisConfig = field(default_factory=WeeklyDiagnosisConfig)
    data_backup: DataBackupConfig = field(default_factory=DataBackupConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    # 5-agent 個別 LLM 設定 (config/agents.yaml)。別ファイル top-level merge のため
    # instruments / news_sources と同じく AppConfig top-level に持つ。
    agent_llms: OrchestratorAgentsLlmConfig = field(
        default_factory=OrchestratorAgentsLlmConfig
    )

    # ── プロバイダー別設定 (旧 price_provider + mt5_bridge を統合) ──
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)

    def __post_init__(self) -> None:
        # Task F (spec §5, 2026-06-25 改訂): orchestrator が発注主体 (mode=live) のとき、
        # 発注先 broker を選ぶ top-level mode との整合を検証する。
        #   - AppConfig.mode=paper / live_test → 許可 (paper_broker で動作確認。本番資金を
        #     動かさない段階検証。spec §0 確定判断7)。live_test の live_broker=mt5 必須は
        #     create_broker 側 (live_broker.py) が課すのでここで二重に課さない。
        #   - AppConfig.mode=live → live_broker 必須 (未設定なら「発注すると言いながら
        #     broker 未設定」の取り違え事故になるため弾く)。
        # 旧版の「orchestrator.mode=live なら AppConfig.mode も必ず live」要求は、
        # paper での段階検証を弾くため撤回 (spec §5 改訂)。
        if getattr(self.orchestrator, "mode", "shadow") == "live":
            if self.mode == "live" and self.live_broker is None:
                raise ValueError(
                    "orchestrator.mode=live with AppConfig.mode=live requires a "
                    "configured live_broker (mt5/oanda); got live_broker=None. "
                    "For dry-run validation use AppConfig.mode=paper or live_test."
                )

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
