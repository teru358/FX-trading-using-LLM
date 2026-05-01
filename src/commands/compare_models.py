"""モデル比較分析コマンド。

ファンダメンタル(A) / テクニカル(B) / 総合(C) の3フェーズを
ユーザーが選択した異なるLLMモデルで実行し、結果を並べて表示する。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from rich import box
from rich.console import Console
from rich.table import Table

from src.analysis.news_analyzer import NewsSentiment, analyze_category_sentiment
from src.analysis.price_analyzer import analyze_price_action, load_user_notes
from src.analysis.rss_fetcher import fetch_category_news
from src.config import AppConfig
from src.data.analysis_store import AnalysisStore
from src.data.indicators import compute_indicators
from src.data.price_fetcher import fetch_ohlcv
from src.llm.client import LLMClient
from src.llm.claude_client import ClaudeClient
from src.llm.gemini_client import GeminiClient
from src.llm.ollama_client import OllamaClient
from src.llm.openai_client import OpenAIClient
from src.llm.response_parser import extract_json

logger = logging.getLogger(__name__)
_console = Console()

# オンラインプロバイダーの既知モデル一覧
_GEMINI_MODELS = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-pro-preview"]
_OPENAI_MODELS = ["gpt-4o-mini", "gpt-4o", "o3-mini"]
_CLAUDE_MODELS = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"]


# ── データクラス ────────────────────────────────────────────────────────────

@dataclass
class ModelOption:
    provider: str   # "ollama" | "gemini" | "openai" | "claude"
    model: str

    @property
    def display(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class CompareResult:
    pair: str
    analyzed_at: datetime
    news_model: ModelOption
    tech_model: ModelOption
    summary_model: ModelOption
    # [A] ニュース
    news_fx: NewsSentiment
    news_global: NewsSentiment
    news_japan: NewsSentiment
    news_combined_score: float
    # [B] テクニカル
    tech_direction: str
    tech_bias_score: float
    tech_confidence: float
    tech_entry_zone: tuple[float, float]
    tech_stop_loss: float
    tech_take_profit: float
    tech_risk_reward: float
    tech_reasoning: str
    # [C] 総合
    summary_direction: str
    summary_confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward_ratio: float
    agreement: str
    key_alignment: str
    key_risk: str
    reasoning: str


# ── モデル探索 ──────────────────────────────────────────────────────────────

async def _probe_ollama(base_url: str, timeout: int = 5) -> list[ModelOption]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return [ModelOption("ollama", m["name"]) for m in data.get("models", [])]
    except Exception as e:
        logger.debug(f"Ollama probe failed: {e}")
        return []


async def _probe_gemini(api_key: str, timeout: int = 5) -> list[ModelOption]:
    if not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            )
            resp.raise_for_status()
            return [ModelOption("gemini", m) for m in _GEMINI_MODELS]
    except Exception as e:
        logger.debug(f"Gemini probe failed: {e}")
        return []


async def _probe_openai(api_key: str, timeout: int = 5) -> list[ModelOption]:
    if not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            return [ModelOption("openai", m) for m in _OPENAI_MODELS]
    except Exception as e:
        logger.debug(f"OpenAI probe failed: {e}")
        return []


async def _probe_claude(api_key: str, timeout: int = 5) -> list[ModelOption]:
    if not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
            resp.raise_for_status()
            return [ModelOption("claude", m) for m in _CLAUDE_MODELS]
    except Exception as e:
        logger.debug(f"Claude probe failed: {e}")
        return []


async def discover_models(config: AppConfig) -> list[ModelOption]:
    """利用可能なモデルを全プロバイダーで並行探索する。

    compare_models は単一 active provider に縛られず、API キー/ローカル疎通が
    成立する provider を全部並べる比較ツール。Ollama base_url は active provider が
    ollama の時は config から、それ以外はデフォルト値を使う。
    """
    _console.print("[dim]利用可能なモデルを確認中...[/dim]")
    if config.llm.provider == "ollama":
        ollama_base_url = config.llm.provider_config.base_url
    else:
        ollama_base_url = "http://localhost:11434"
    results = await asyncio.gather(
        _probe_ollama(ollama_base_url),
        _probe_gemini(os.environ.get("GEMINI_API_KEY", "")),
        _probe_openai(os.environ.get("OPENAI_API_KEY", "")),
        _probe_claude(os.environ.get("ANTHROPIC_API_KEY", "")),
        return_exceptions=True,
    )
    models: list[ModelOption] = []
    for r in results:
        if isinstance(r, list):
            models.extend(r)
    return models


# ── LLMクライアント生成 ────────────────────────────────────────────────────

def _make_llm(option: ModelOption, config: AppConfig) -> LLMClient:
    """compare 用 LLMClient を生成。

    比較ツールの性質上 provider 横断で生成するため、active provider の
    provider_config に縛られず固定デフォルト値を使う。
    """
    if option.provider == "gemini":
        return GeminiClient(
            model=option.model,
            timeout_seconds=60,
            max_retries=2,
        )
    if option.provider == "openai":
        return OpenAIClient(
            model=option.model,
            timeout_seconds=60,
            max_retries=2,
        )
    if option.provider == "claude":
        return ClaudeClient(
            model=option.model,
            timeout_seconds=60,
            max_retries=2,
            max_tokens=4096,
        )
    # default: ollama
    if config.llm.provider == "ollama":
        ollama_base_url = config.llm.provider_config.base_url
        ollama_timeout = config.llm.provider_config.timeout_seconds
        ollama_retries = config.llm.provider_config.max_retries
    else:
        ollama_base_url = "http://localhost:11434"
        ollama_timeout = 120
        ollama_retries = 2
    return OllamaClient(
        base_url=ollama_base_url,
        model=option.model,
        timeout_seconds=ollama_timeout,
        max_retries=ollama_retries,
    )


# ── モデル選択UI ───────────────────────────────────────────────────────────

def _show_model_table(models: list[ModelOption]) -> None:
    tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    tbl.add_column("No.", justify="right")
    tbl.add_column("Provider")
    tbl.add_column("Model")
    for i, m in enumerate(models, 1):
        tbl.add_row(str(i), m.provider, m.model)
    _console.print(tbl)


def _select_model(models: list[ModelOption], label: str) -> ModelOption:
    from prompt_toolkit import prompt as pt_prompt
    while True:
        raw = pt_prompt(f"{label} [1-{len(models)}]: ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(models):
                return models[idx]
        except ValueError:
            pass
        _console.print(f"[red]1〜{len(models)} の番号を入力してください[/red]")


# ── [A] ニュース分析 ────────────────────────────────────────────────────────

async def _run_news_analysis(
    config: AppConfig,
    llm: LLMClient,
) -> tuple[NewsSentiment, NewsSentiment, NewsSentiment]:
    """RSS 1回取得 → 選択モデルで3カテゴリ分析。"""
    user_notes = load_user_notes(config.user_notes_path, "news")
    global_kw = frozenset(kw.lower() for kw in config.keywords.global_keywords)
    japan_kw = frozenset(kw.lower() for kw in config.keywords.japan_keywords)

    _console.print("[dim]  RSS取得中 (fx/global/japan)...[/dim]")
    fetch_fx = fetch_category_news(
        feeds=config.news_sources.feeds_fx,
        keywords=None,
        freshness_hours=config.news_collection.news_freshness_hours,
        summary_max_chars=config.news_collection.summary_max_chars,
    )
    fetch_global = fetch_category_news(
        feeds=config.news_sources.feeds_global,
        keywords=global_kw,
        freshness_hours=config.news_collection.news_freshness_hours,
        summary_max_chars=config.news_collection.summary_max_chars,
    )
    fetch_japan = fetch_category_news(
        feeds=config.news_sources.feeds_japan,
        keywords=japan_kw,
        freshness_hours=config.news_collection.news_freshness_hours,
        summary_max_chars=config.news_collection.summary_max_chars,
    )

    _console.print("[dim]  ニュースセンチメント分析中...[/dim]")
    temp = config.llm.news_analysis.temperature
    news_fx, news_global, news_japan = await asyncio.gather(
        analyze_category_sentiment("fx", fetch_fx, llm, temperature=temp, user_notes=user_notes),
        analyze_category_sentiment("global", fetch_global, llm, temperature=temp, user_notes=user_notes),
        analyze_category_sentiment("japan", fetch_japan, llm, temperature=temp, user_notes=user_notes),
    )
    return news_fx, news_global, news_japan


# ── [C] 総合分析プロンプト ───────────────────────────────────────────────────

_SUMMARY_SYSTEM = """You are an expert FX trading analyst.
You will receive two independent analyses of the same currency pair:
[A] Fundamental (news sentiment) and [B] Technical (price action).
Synthesize them into a final actionable trading decision.
Always respond with JSON only — no markdown, no explanation outside the JSON block.
All text fields (key_alignment, key_risk, reasoning) must be written in Japanese.
"""

_SUMMARY_USER_TEMPLATE = """\
=== Pair: {pair} ===

[A] Fundamental Analysis  (model: {news_model})
  fx_sentiment:      score={fx_score:+.2f}  conf={fx_conf:.2f}
                     {fx_summary}
  global_sentiment:  score={global_score:+.2f}  conf={global_conf:.2f}
                     {global_summary}
  japan_sentiment:   score={japan_score:+.2f}  conf={japan_conf:.2f}
                     {japan_summary}
  combined_score:    {combined_score:+.2f}

[B] Technical Analysis  (model: {tech_model})
  direction:    {tech_direction}
  bias_score:   {tech_bias_score:+.2f}
  confidence:   {tech_confidence:.2f}
  entry_zone:   [{entry_low:.5f}, {entry_high:.5f}]
  stop_loss:    {stop_loss:.5f}
  take_profit:  {take_profit:.5f}
  risk_reward:  {risk_reward:.1f}x
  reasoning:    {tech_reasoning}

{macro_context}

=== Synthesis Rules ===
- If [A] and [B] directional signals AGREE → agreement="agree", higher confidence allowed
- If [A] and [B] DISAGREE significantly → agreement="disagree", prefer direction="hold"
- If partial overlap → agreement="partial", be conservative
- entry_price must be within entry_zone from [B]
- For BUY:  stop_loss < entry_price, take_profit > entry_price
- For SELL: stop_loss > entry_price, take_profit < entry_price

Output ONLY this JSON (no markdown fences):
{{
  "direction": "buy|sell|hold",
  "confidence": <float 0.0-1.0>,
  "entry_price": <float>,
  "stop_loss": <float>,
  "take_profit": <float>,
  "risk_reward_ratio": <float>,
  "agreement": "agree|partial|disagree",
  "key_alignment": "<両分析が一致している点（日本語）>",
  "key_risk": "<主要なリスクまたは乖離ポイント（日本語）>",
  "reasoning": "<2〜3文の総合判断根拠（日本語）>"
}}"""


async def _run_summary_analysis(
    pair_cfg,
    news_fx: NewsSentiment,
    news_global: NewsSentiment,
    news_japan: NewsSentiment,
    tech,
    macro_context: str,
    news_model: ModelOption,
    tech_model: ModelOption,
    llm: LLMClient,
) -> dict:
    combined_score = (
        news_fx.sentiment_score + news_global.sentiment_score + news_japan.sentiment_score
    ) / 3

    user_prompt = _SUMMARY_USER_TEMPLATE.format(
        pair=pair_cfg.display_name,
        news_model=news_model.display,
        tech_model=tech_model.display,
        fx_score=news_fx.sentiment_score,
        fx_conf=news_fx.confidence,
        fx_summary=news_fx.summary[:120],
        global_score=news_global.sentiment_score,
        global_conf=news_global.confidence,
        global_summary=news_global.summary[:120],
        japan_score=news_japan.sentiment_score,
        japan_conf=news_japan.confidence,
        japan_summary=news_japan.summary[:120],
        combined_score=combined_score,
        tech_direction=tech.direction_bias,
        tech_bias_score=tech.bias_score,
        tech_confidence=tech.confidence,
        entry_low=tech.entry_zone[0],
        entry_high=tech.entry_zone[1],
        stop_loss=tech.stop_loss,
        take_profit=tech.take_profit,
        risk_reward=tech.risk_reward_ratio,
        tech_reasoning=tech.reasoning_summary[:200],
        macro_context=macro_context or "",
    )
    messages = [
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]
    resp = await llm.chat(messages, temperature=0.1)
    return extract_json(resp)


# ── 結果表示 ───────────────────────────────────────────────────────────────

def _dir_color(d: str) -> str:
    return {"buy": "green", "long": "green", "sell": "red", "short": "red"}.get(d.lower(), "yellow")


def print_compare_result(result: CompareResult) -> None:
    _console.print()
    _console.rule(
        f"[bold]FX モデル比較分析  {result.pair}  "
        f"{result.analyzed_at.strftime('%Y-%m-%d %H:%M')}[/bold]"
    )

    # [A]
    _console.print(f"\n[bold cyan][A] ファンダメンタル分析[/bold cyan]  ({result.news_model.display})")
    tbl_a = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    tbl_a.add_column("", style="dim", min_width=10)
    tbl_a.add_column("")
    tbl_a.add_row("FX", f"score={result.news_fx.sentiment_score:+.2f}  conf={result.news_fx.confidence:.2f}  {result.news_fx.summary[:80]}")
    tbl_a.add_row("Global", f"score={result.news_global.sentiment_score:+.2f}  conf={result.news_global.confidence:.2f}  {result.news_global.summary[:80]}")
    tbl_a.add_row("Japan", f"score={result.news_japan.sentiment_score:+.2f}  conf={result.news_japan.confidence:.2f}  {result.news_japan.summary[:80]}")
    tbl_a.add_row("総合スコア", f"{result.news_combined_score:+.2f}")
    _console.print(tbl_a)

    # [B]
    dc = _dir_color(result.tech_direction)
    _console.print(f"[bold cyan][B] テクニカル分析[/bold cyan]  ({result.tech_model.display})")
    tbl_b = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    tbl_b.add_column("", style="dim", min_width=10)
    tbl_b.add_column("")
    tbl_b.add_row("方向", f"[{dc}]{result.tech_direction.upper()}[/{dc}]  bias={result.tech_bias_score:+.2f}  conf={result.tech_confidence:.2f}")
    tbl_b.add_row("エントリー", f"{result.tech_entry_zone[0]:.5f} - {result.tech_entry_zone[1]:.5f}")
    tbl_b.add_row("SL / TP", f"{result.tech_stop_loss:.5f} / {result.tech_take_profit:.5f}  R/R={result.tech_risk_reward:.1f}x")
    tbl_b.add_row("根拠", result.tech_reasoning[:100])
    _console.print(tbl_b)

    # [C]
    sc = _dir_color(result.summary_direction)
    ac = {"agree": "green", "partial": "yellow", "disagree": "red"}.get(result.agreement, "white")
    _console.print(f"[bold cyan][C] 総合分析[/bold cyan]  ({result.summary_model.display})")
    tbl_c = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    tbl_c.add_column("", style="dim", min_width=10)
    tbl_c.add_column("")
    tbl_c.add_row("発注方向", f"[bold {sc}]{result.summary_direction.upper()}[/bold {sc}]  信頼度={result.summary_confidence:.2f}")
    tbl_c.add_row("エントリー", f"{result.entry_price:.5f}")
    tbl_c.add_row("SL / TP", f"{result.stop_loss:.5f} / {result.take_profit:.5f}  R/R={result.risk_reward_ratio:.1f}x")
    tbl_c.add_row("一致度", f"[{ac}]{result.agreement}[/{ac}]")
    tbl_c.add_row("一致点", result.key_alignment[:100])
    tbl_c.add_row("リスク", result.key_risk[:100])
    tbl_c.add_row("理由", result.reasoning[:150])
    _console.print(tbl_c)
    _console.print()


# ── TXT保存 ────────────────────────────────────────────────────────────────

def save_compare_txt(result: CompareResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_pair = result.pair.replace("=", "").replace("/", "")
    fname = f"{result.analyzed_at.strftime('%Y%m%d_%H%M%S')}_{safe_pair}.txt"
    path = output_dir / fname

    lines = [
        "FX モデル比較分析",
        f"Pair        : {result.pair}",
        f"Analyzed at : {result.analyzed_at.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"[A] ファンダメンタル分析  ({result.news_model.display})",
        f"  FX       : score={result.news_fx.sentiment_score:+.2f}  conf={result.news_fx.confidence:.2f}",
        f"             {result.news_fx.summary}",
        f"  Global   : score={result.news_global.sentiment_score:+.2f}  conf={result.news_global.confidence:.2f}",
        f"             {result.news_global.summary}",
        f"  Japan    : score={result.news_japan.sentiment_score:+.2f}  conf={result.news_japan.confidence:.2f}",
        f"             {result.news_japan.summary}",
        f"  総合スコア: {result.news_combined_score:+.2f}",
        "",
        f"[B] テクニカル分析  ({result.tech_model.display})",
        f"  方向      : {result.tech_direction.upper()}  bias={result.tech_bias_score:+.2f}  conf={result.tech_confidence:.2f}",
        f"  エントリー: {result.tech_entry_zone[0]:.5f} - {result.tech_entry_zone[1]:.5f}",
        f"  SL / TP  : {result.tech_stop_loss:.5f} / {result.tech_take_profit:.5f}  R/R={result.tech_risk_reward:.1f}x",
        f"  根拠      : {result.tech_reasoning}",
        "",
        f"[C] 総合分析  ({result.summary_model.display})",
        f"  発注方向  : {result.summary_direction.upper()}  信頼度={result.summary_confidence:.2f}",
        f"  エントリー: {result.entry_price:.5f}",
        f"  SL / TP  : {result.stop_loss:.5f} / {result.take_profit:.5f}  R/R={result.risk_reward_ratio:.1f}x",
        f"  一致度    : {result.agreement}",
        f"  一致点    : {result.key_alignment}",
        f"  リスク    : {result.key_risk}",
        f"  理由      : {result.reasoning}",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── メインエントリポイント ──────────────────────────────────────────────────
# pt_prompt() は asyncio.run() の外（同期コンテキスト）でのみ呼ぶ。
# asyncio.run() の中で prompt_toolkit を使うとイベントループが競合するため。

def run_compare(
    config: AppConfig,
    store,
    analysis_store: AnalysisStore,
    pair_arg: str | None = None,
) -> None:
    from prompt_toolkit import prompt as pt_prompt
    from src.config import BASE_DIR

    # ── [同期] ペア選択 ───────────────────────────────────────────────────
    tradeable = config.tradeable_instruments
    if not tradeable:
        _console.print("[red]取引対象ペアが設定されていません[/red]")
        return

    pair_cfg = None
    if pair_arg:
        pair_cfg = next(
            (p for p in tradeable if p.symbol.upper() == pair_arg.upper()), None
        )
        if pair_cfg is None:
            _console.print(f"[red]ペアが見つかりません: {pair_arg}[/red]")

    if pair_cfg is None:
        if len(tradeable) == 1:
            pair_cfg = tradeable[0]
        else:
            _console.print("\n取引対象ペア:")
            for i, p in enumerate(tradeable, 1):
                _console.print(f"  [{i}] {p.display_name}  ({p.symbol})")
            while True:
                raw = pt_prompt(f"ペアを選択 [1-{len(tradeable)}]: ").strip()
                try:
                    idx = int(raw) - 1
                    if 0 <= idx < len(tradeable):
                        pair_cfg = tradeable[idx]
                        break
                except ValueError:
                    pass
                _console.print(f"[red]1〜{len(tradeable)} を入力してください[/red]")

    _console.print(f"\n対象ペア: [bold]{pair_cfg.display_name}[/bold] ({pair_cfg.symbol})")

    # ── [非同期] モデル探索 ───────────────────────────────────────────────
    models = asyncio.run(discover_models(config))
    if not models:
        _console.print("[red]利用可能なモデルが見つかりません[/red]")
        return

    _console.print(f"\n[bold]利用可能なモデル ({len(models)}件)[/bold]")
    _show_model_table(models)

    # ── [同期] モデル選択 ─────────────────────────────────────────────────
    _console.print("各フェーズで使用するモデルを選択してください:")
    news_model = _select_model(models, "[A] ファンダメンタル分析モデル")
    tech_model = _select_model(models, "[B] テクニカル分析モデル    ")
    summary_model = _select_model(models, "[C] 総合分析モデル          ")

    _console.print(
        f"\n選択: [cyan][A][/cyan] {news_model.display}  "
        f"[cyan][B][/cyan] {tech_model.display}  "
        f"[cyan][C][/cyan] {summary_model.display}"
    )
    _console.print("\n[cyan]分析を開始します...[/cyan]")

    # ── [非同期] A/B/C 分析 ───────────────────────────────────────────────
    result = asyncio.run(_run_analysis(
        config, store, analysis_store, pair_cfg,
        news_model, tech_model, summary_model,
    ))

    # ── [同期] 結果表示・保存確認 ─────────────────────────────────────────
    print_compare_result(result)

    try:
        raw = pt_prompt("テキストファイルに保存しますか？ [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    if raw == "y":
        out_dir = BASE_DIR / "data" / "compare"
        path = save_compare_txt(result, out_dir)
        _console.print(f"[green]保存しました: {path}[/green]")


async def _run_analysis(
    config: AppConfig,
    store,
    analysis_store: AnalysisStore,
    pair_cfg,
    news_model: ModelOption,
    tech_model: ModelOption,
    summary_model: ModelOption,
) -> CompareResult:
    """A/B/C の LLM 分析を非同期で実行して CompareResult を返す。"""
    from src.data.price_store import PriceStore
    from src.trading_cycle import _build_macro_context

    # ── データ収集（OHLCV・指標・マクロ） ────────────────────────────────
    _console.print("[dim]OHLCV・テクニカル指標取得中...[/dim]")
    price_store = PriceStore(config.prices_db_path)
    price_data = fetch_ohlcv(
        pair_cfg.symbol,
        period=f"{config.trading.lookback_days}d",
        interval=config.trading.ohlcv_interval,
        price_store=price_store,
    )
    _, tech_summary = compute_indicators(
        price_data.df,
        indicator_cfg=config.analysis.indicators,
        pattern_cfg=config.analysis.chart_patterns,
    )
    macro_ctx = _build_macro_context(config, analysis_store)

    llm_news = _make_llm(news_model, config)
    llm_tech = _make_llm(tech_model, config)
    llm_summary = _make_llm(summary_model, config)

    # ── [A] ニュース分析 ──────────────────────────────────────────────────
    _console.print(f"[dim][A] ニュース分析中 ({news_model.display})...[/dim]")
    try:
        news_fx, news_global, news_japan = await _run_news_analysis(config, llm_news)
    except Exception as e:
        _console.print(f"[red][A] ニュース分析失敗: {e}[/red]")
        raise

    combined_score = (
        news_fx.sentiment_score + news_global.sentiment_score + news_japan.sentiment_score
    ) / 3

    news_ctx_text = (
        "=== News Context ===\n"
        f"[fx]     score={news_fx.sentiment_score:+.2f}  {news_fx.summary[:100]}\n"
        f"[global] score={news_global.sentiment_score:+.2f}  {news_global.summary[:100]}\n"
        f"[japan]  score={news_japan.sentiment_score:+.2f}  {news_japan.summary[:100]}"
    )

    # ── [B] テクニカル分析 ────────────────────────────────────────────────
    _console.print(f"[dim][B] テクニカル分析中 ({tech_model.display})...[/dim]")
    try:
        tech = await analyze_price_action(
            pair_cfg=pair_cfg,
            price_data=price_data,
            summary=tech_summary,
            llm=llm_tech,
            temperature=config.llm.price_analysis.temperature,
            news_context=news_ctx_text,
            macro_context=macro_ctx,
            user_notes_path=config.user_notes_path,
        )
    except Exception as e:
        _console.print(f"[red][B] テクニカル分析失敗: {e}[/red]")
        raise

    # ── [C] 総合分析 ──────────────────────────────────────────────────────
    _console.print(f"[dim][C] 総合分析中 ({summary_model.display})...[/dim]")
    try:
        summary_data = await _run_summary_analysis(
            pair_cfg=pair_cfg,
            news_fx=news_fx,
            news_global=news_global,
            news_japan=news_japan,
            tech=tech,
            macro_context=macro_ctx,
            news_model=news_model,
            tech_model=tech_model,
            llm=llm_summary,
        )
    except Exception as e:
        _console.print(f"[red][C] 総合分析失敗: {e}[/red]")
        raise

    def _f(key: str, default):
        val = summary_data.get(key, default)
        if isinstance(default, float):
            try:
                return float(val)
            except (TypeError, ValueError):
                return default
        return val

    return CompareResult(
        pair=pair_cfg.symbol,
        analyzed_at=datetime.now(),
        news_model=news_model,
        tech_model=tech_model,
        summary_model=summary_model,
        news_fx=news_fx,
        news_global=news_global,
        news_japan=news_japan,
        news_combined_score=combined_score,
        tech_direction=tech.direction_bias,
        tech_bias_score=tech.bias_score,
        tech_confidence=tech.confidence,
        tech_entry_zone=tech.entry_zone,
        tech_stop_loss=tech.stop_loss,
        tech_take_profit=tech.take_profit,
        tech_risk_reward=tech.risk_reward_ratio,
        tech_reasoning=tech.reasoning_summary,
        summary_direction=_f("direction", "hold"),
        summary_confidence=_f("confidence", 0.5),
        entry_price=_f("entry_price", (tech.entry_zone[0] + tech.entry_zone[1]) / 2),
        stop_loss=_f("stop_loss", tech.stop_loss),
        take_profit=_f("take_profit", tech.take_profit),
        risk_reward_ratio=_f("risk_reward_ratio", tech.risk_reward_ratio),
        agreement=_f("agreement", "partial"),
        key_alignment=_f("key_alignment", ""),
        key_risk=_f("key_risk", ""),
        reasoning=_f("reasoning", ""),
    )
