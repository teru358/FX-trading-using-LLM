from __future__ import annotations

import concurrent.futures
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.config import AppConfig, InstrumentConfig

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.orchestrator.runtime import OrchestratorRuntime

_logger = logging.getLogger(__name__)

_SYMBOL_CHECK_TIMEOUT = 10  # 全シンボルの並列フェッチ最大待機秒数

# ロール → 短縮ラベル (表示用)
_ROLE_LABEL = {
    "news_analysis": "news",
    "price_analysis": "price",
    "reflection": "reflect",
    "embedding": "embed",
}

_LLM_ROLES = ("news_analysis", "price_analysis", "reflection")


def _check_one_symbol(inst: InstrumentConfig) -> tuple[bool, str]:
    """yfinance で最小フェッチを試み (成功フラグ, モードラベル) を返す。"""
    import yfinance as yf

    mode_label = "trade" if inst.is_tradeable else (inst.mode if inst.mode != "trade" else inst.asset_type)
    try:
        df = yf.Ticker(inst.symbol).history(period="2d", interval="1d")
        return not df.empty, mode_label
    except Exception:
        return False, mode_label


_console = Console()


def _collect_llm_role_entries(config: AppConfig) -> list[tuple[str, str, str]]:
    """表示用に (role_label, provider, model_name) のリストを組み立てる。

    新スキーマでは provider は config.llm.provider で 1 つだけ。
    embedding は config.embedding.provider で別管理。
    """
    entries: list[tuple[str, str, str]] = []
    provider = config.llm.provider
    for role in _LLM_ROLES:
        rc = getattr(config.llm, role)
        entries.append((_ROLE_LABEL[role], provider, rc.model or "(unset)"))

    emb_provider = getattr(config.embedding, "provider", "ollama")
    entries.append((_ROLE_LABEL["embedding"], emb_provider, config.embedding.model))
    return entries


def _ollama_required_models(config: AppConfig) -> set[str]:
    """Ollama に要求されるモデル名の集合 (LLM provider と embedding 両方を考慮)。"""
    models: set[str] = set()
    if config.llm.provider == "ollama":
        for role in _LLM_ROLES:
            rc = getattr(config.llm, role)
            if rc.model:
                models.add(rc.model)
    if getattr(config.embedding, "provider", "ollama") == "ollama":
        models.add(config.embedding.model)
    return models


def _llamacpp_required_models(config: AppConfig) -> set[str]:
    """llama-swap に要求されるモデル名の集合。"""
    models: set[str] = set()
    if config.llm.provider == "llamacpp":
        for role in _LLM_ROLES:
            rc = getattr(config.llm, role)
            if rc.model:
                models.add(rc.model)
    if getattr(config.embedding, "provider", "ollama") == "llamacpp":
        models.add(config.embedding.model)
    return models


def _fetch_ollama_models(config: AppConfig) -> set[str] | None:
    """Ollama の /api/tags から登録モデル一覧を取得。疎通失敗時は None。

    base_url は LLM provider が ollama のときは provider_config から、
    それ以外で embedding が ollama のときは embedding.base_url から取得。
    """
    import httpx

    if config.llm.provider == "ollama":
        base_url = config.llm.provider_config.base_url
    else:
        base_url = getattr(config.embedding, "base_url", "")
    if not base_url:
        return None
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=5)
        resp.raise_for_status()
        return {m["name"] for m in resp.json().get("models", [])}
    except Exception:
        return None


def _fetch_llamacpp_models(config: AppConfig) -> set[str] | None:
    """llama-swap の /v1/models から登録モデル一覧を取得。疎通失敗時は None。"""
    import httpx

    if config.llm.provider == "llamacpp":
        base_url = config.llm.provider_config.base_url
    else:
        base_url = getattr(config.embedding, "base_url", "")
    if not base_url:
        return None
    base_url = base_url.rstrip("/")
    try:
        resp = httpx.get(f"{base_url}/models", timeout=5)
        resp.raise_for_status()
        return {m["id"] for m in resp.json().get("data", [])}
    except Exception:
        return None


def _claude_cli_used(config: AppConfig) -> bool:
    """claude-cli が選択されているか。"""
    return config.llm.provider == "claude-cli"


def _check_claude_cli(config: AppConfig) -> tuple[bool, str]:
    """`claude` CLI の存在と呼び出し可能性を確認。

    Returns: (ok, detail_text)。
    """
    import shutil
    import subprocess

    command = config.llm.provider_config.command or "claude"

    exe = shutil.which(command)
    if not exe:
        return False, f"{command!r} not found in PATH"

    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        return False, f"{command} --version failed: {e}"

    if proc.returncode != 0:
        return False, f"{command} --version exit={proc.returncode}"

    version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr) else "?"
    return True, version


def _ollama_model_available(name: str, available: set[str]) -> bool:
    """Ollama はタグ付き (e.g. "llama3.1:8b") で登録されるため substring 一致。"""
    return any(name in a for a in available)


def _build_llm_table(
    config: AppConfig,
    ollama_available: set[str] | None,
    llamacpp_available: set[str] | None,
    claude_cli_ok: bool | None,
) -> tuple[Table, bool]:
    """LLM / Embedding の状態テーブルを組み立てる。(table, all_ok)。"""
    table = Table(box=None, show_header=False, padding=(0, 1), pad_edge=False, expand=True)
    table.add_column("role", style="cyan", width=8)
    table.add_column("provider/model", style="dim")
    table.add_column("status", justify="right")

    all_ok = True
    for role_label, provider, model in _collect_llm_role_entries(config):
        if provider == "ollama":
            if ollama_available is None:
                status_text, passed = "[red]UNREACHABLE[/red]", False
            elif _ollama_model_available(model, ollama_available):
                status_text, passed = "[green]✓[/green]", True
            else:
                status_text, passed = "[red]✗ NOT FOUND[/red]", False
        elif provider == "llamacpp":
            if llamacpp_available is None:
                status_text, passed = "[red]UNREACHABLE[/red]", False
            elif model in llamacpp_available:
                status_text, passed = "[green]✓[/green]", True
            else:
                status_text, passed = "[red]✗ NOT FOUND[/red]", False
        elif provider == "claude-cli":
            # モデル存在確認は CLI が認証済みならサブスク側の責務なので省略、
            # CLI バイナリの存在のみを確認
            if claude_cli_ok is None:
                status_text, passed = "[yellow]◦ skipped[/yellow]", True
            elif claude_cli_ok:
                status_text, passed = "[green]✓[/green]", True
            else:
                status_text, passed = "[red]✗ CLI not found[/red]", False
        else:
            # gemini / openai / claude (API) は HTTP チェック省略 (API キーは別途)
            status_text, passed = "[yellow]◦ api[/yellow]", True

        table.add_row(role_label, f"{provider} / {model}", status_text)
        if not passed:
            all_ok = False

    return table, all_ok


def _build_symbol_table(instruments: list[InstrumentConfig]) -> tuple[Table, bool]:
    """Instruments (symbols) の状態テーブルを組み立てる。(table, all_ok)。"""
    table = Table(box=None, show_header=False, padding=(0, 1), pad_edge=False, expand=True)
    table.add_column("name", style="dim")
    table.add_column("status", justify="right")

    if not instruments:
        return table, True

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(instruments)) as pool:
        future_to_inst = {pool.submit(_check_one_symbol, inst): inst for inst in instruments}
        done, not_done = concurrent.futures.wait(future_to_inst, timeout=_SYMBOL_CHECK_TIMEOUT)

    sym_results: dict[str, tuple[bool, str]] = {}
    for future in done:
        inst = future_to_inst[future]
        try:
            sym_ok, mode_label = future.result()
        except Exception:
            sym_ok, mode_label = False, "trade" if inst.is_tradeable else inst.asset_type
        sym_results[inst.symbol] = (sym_ok, mode_label)
    for future in not_done:
        inst = future_to_inst[future]
        sym_results[inst.symbol] = (False, "trade" if inst.is_tradeable else inst.asset_type)
        future.cancel()

    all_ok = True
    for inst in instruments:
        sym_ok, mode_label = sym_results.get(inst.symbol, (False, "?"))
        if sym_ok:
            status = f"[green]✓[/green] [dim]{mode_label}[/dim]"
        elif inst.is_tradeable:
            status = f"[red]✗ NOT FOUND[/red] [dim]{mode_label}[/dim]"
            all_ok = False
        else:
            status = f"[yellow]⚠ WARN[/yellow] [dim]{mode_label}[/dim]"
        table.add_row(inst.display_name, status)

    return table, all_ok


def _check_approval_gate_config(config: AppConfig) -> tuple[bool, str | None]:
    """approval gate (orchestrator.approval_gate) の設定整合性を確認する。

    ON のとき plan は pending_approval になり、F-5 API 経由の承認でしか active
    化されない。api.enabled=False か API_SECRET_KEY 未設定だと承認経路が存在せず
    plan が永久に滞留する (無言の取引停止) ため、起動時エラーとして検出する
    (実装後レビュー Medium)。既定の approval_gate=False では常に (True, None)。
    """
    if not config.orchestrator.approval_gate:
        return True, None
    if not config.api.enabled:
        return False, (
            "orchestrator.approval_gate=True ですが api.enabled=False です。"
            "承認 API が起動しないため plan が pending_approval のまま滞留します。"
            "api.enabled を true にするか approval_gate を無効化してください。"
        )
    if not os.environ.get("API_SECRET_KEY"):
        return False, (
            "orchestrator.approval_gate=True ですが環境変数 API_SECRET_KEY が"
            "未設定です。承認 API を呼び出せないため plan が pending_approval の"
            "まま滞留します。API_SECRET_KEY を設定してください。"
        )
    return True, None


def startup_checks(config: AppConfig) -> bool:
    """起動時チェック（LLMプロバイダー・シンボル疎通・ディレクトリ）を実行して結果を表示する。"""
    # LLM / Embedding チェック (provider ごとに一括で /models を取りにいく)
    ollama_avail = _fetch_ollama_models(config) if _ollama_required_models(config) else set()
    llamacpp_avail = _fetch_llamacpp_models(config) if _llamacpp_required_models(config) else set()
    if _claude_cli_used(config):
        claude_cli_ok, _claude_cli_detail = _check_claude_cli(config)
    else:
        claude_cli_ok = None  # 未使用なら不問
    llm_table, llm_ok = _build_llm_table(config, ollama_avail, llamacpp_avail, claude_cli_ok)

    # Symbols チェック
    instruments = config.enabled_instruments
    trade_count = sum(1 for i in instruments if i.is_tradeable)
    watch_count = len(instruments) - trade_count
    symbol_table, symbol_ok = _build_symbol_table(instruments)

    # Approval gate 設定チェック (実装後レビュー Medium)
    gate_ok, gate_err = _check_approval_gate_config(config)

    # ディレクトリ作成
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.rag_db_path.mkdir(parents=True, exist_ok=True)
    (Path(__file__).parent.parent / "logs").mkdir(exist_ok=True)

    # レンダリング
    ok = llm_ok and symbol_ok and gate_ok
    llm_header = Text("LLM / Embedding", style="bold")
    symbol_header = Text(
        f"Instruments  ({trade_count} trade / {watch_count} watch)", style="bold",
    )
    body_items = [
        llm_header,
        llm_table,
        Text(""),  # spacer
        symbol_header,
        symbol_table,
    ]
    if gate_err:
        body_items.append(Text(""))  # spacer
        body_items.append(Text(f"✗ Approval gate: {gate_err}", style="bold red"))
        _logger.error("[STARTUP] approval gate 設定エラー: %s", gate_err)
    body = Group(*body_items)

    overall = "[bold green]READY[/bold green]" if ok else "[bold red]FAILED[/bold red]"
    _console.print(Panel(
        body,
        title=f"[bold cyan]FX Paper Trader[/bold cyan]  Startup Checks  {overall}",
        border_style="cyan" if ok else "red",
        padding=(0, 1),
        box=box.ROUNDED,
    ))
    return ok


def ensure_orchestrator_or_exit(
    runtime: "OrchestratorRuntime | None", *, enabled: bool, mode: str = "live",
) -> None:
    """発注経路の存在を保証する (spec §1.5)。無ければ起動中止。

    FATAL 理由は console と logger の両方に出す。logging_setup は
    RotatingFileHandler / TimedRotatingFileHandler しか登録せず StreamHandler が
    無いため、console 出力はログファイルにも API /logs にも残らない。tmux/nohup
    越しで stdout を見ていない運用者が「なぜか起動していない」状態に陥るのを防ぐ
    (レビュー M-2)。
    """
    if not enabled:
        msg = ("orchestrator.enabled=false — 発注経路がありません。起動を中止します")
        _console.print(f"[red][FATAL] {msg}[/red]")
        _logger.critical("[STARTUP] %s", msg)
        raise SystemExit(1)
    if runtime is None:
        msg = "orchestrator の構築に失敗しました。起動を中止します"
        _console.print(f"[red][FATAL] {msg}[/red]")
        _logger.critical("[STARTUP] %s", msg)
        raise SystemExit(1)
    if mode != "live":
        _logger.warning("[ORCH] mode=%s — 発注は行われません (検証運転)", mode)


def run_startup_sequence(
    *,
    build: "Callable[[], OrchestratorRuntime | None]",
    validate: "Callable[[OrchestratorRuntime | None], None]",
    initialize: "Callable[[], None]",
    start_api: "Callable[[], None]",
    start_scheduler: "Callable[[], None]",
) -> "OrchestratorRuntime":
    """build → validate → API → initialize → start → scheduler の順序を固定する
    seam (spec §1.5 / plan レビュー Medium-5 / High-1 / 実装後レビュー H-1)。

    順序の根拠:

    - **build + validate が最初** — orchestrator の構築と起動可能性の検証を
      API・scheduler より前に済ませる (spec §1.5)。発注経路が無いまま他を
      起動しない。
    - **API が発注ループより前** — API は制御面 (halt/resume・承認ゲート・
      /status) を担う。EADDRINUSE で旧プロセスと競合する等で API が落ちたとき、
      発注ループがまだ動いていない状態で中止できる。副次的に、初回収集
      (LLM 込みで数分) の間 REST API が無応答になる問題も解消する。
    - **initialize が runtime.start() より前** — initialize は初回 news/tech
      収集。orchestrator の planning/watch loop は start() 直後から動くため、
      古い snapshot で判断・発注させない (plan レビュー High-1)。
    - **scheduler が最後**。

    どの段の失敗 (例外 / SystemExit) でも後段は実行されない。さらに
    runtime.start() 以降で失敗したら runtime.stop() で graceful に畳んでから
    再送出する — main.py の try/finally は run_startup_sequence より後ろにあり、
    ここで stop() しないと通知 worker drain / quote producer 停止 / plan finalize
    が走らないまま部分起動状態で例外が抜ける。

    **起動状態の公開 (レビュー Medium):** API が発注ループより前に立つため、
    /status が常に ok を返すと初回収集中・scheduler 未起動でも外部監視から ready に
    見えてしまう。``api._state.readiness`` を starting → ready で遷移させ、
    scheduler 起動完了で初めて ready にする。

    観測可能な失敗だけが failed になる。build / validate の失敗は API 起動前なので
    fail-fast でプロセスごと落ち、/status を読む相手がそもそもいない (failed を
    返す暇はない)。API 起動後の initialize / runtime.start / start_scheduler の
    失敗は、例外送出後も API スレッドが生きている間 /status で failed として観測できる。
    """
    from src.api._state import state as _api_state

    runtime = build()
    validate(runtime)
    # validate 通過後は非 None が契約。将来 validate を差し替えて None を通した
    # 場合、start() を黙ってスキップすると fail-fast が無音で無効化されるため、
    # 契約違反はここで顕在化させる (レビュー L-2)。
    assert runtime is not None, "validate() must reject a None runtime"
    start_api()
    # ここから先が「API は応答するが、まだ ready ではない」区間 (レビュー Medium)。
    # 初回収集 (LLM 込みで数分) と scheduler 起動が済むまで /status は starting。
    try:
        initialize()
    except BaseException:
        _api_state.readiness = "failed"
        raise
    try:
        runtime.start()
        start_scheduler()
    except BaseException:
        _api_state.readiness = "failed"
        try:
            runtime.stop()
        except Exception:
            # stop() の失敗で元の起動失敗理由を握り潰さない。
            _logger.exception("[STARTUP] 起動中止中の runtime.stop() が失敗")
        raise
    # scheduler まで起動して初めて ready。
    _api_state.readiness = "ready"
    return runtime
