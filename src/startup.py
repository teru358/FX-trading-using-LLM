from __future__ import annotations

import concurrent.futures
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.config import AppConfig, InstrumentConfig, _DEFAULT_OLLAMA_MODEL

_SYMBOL_CHECK_TIMEOUT = 10  # 全シンボルの並列フェッチ最大待機秒数

# ロール → 短縮ラベル (表示用)
_ROLE_LABEL = {
    "news_analysis": "news",
    "price_analysis": "price",
    "reflection": "reflect",
    "embedding": "embed",
}


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

    ロール順: news → price → reflect → embed
    """
    entries: list[tuple[str, str, str]] = []
    for role in ("news_analysis", "price_analysis", "reflection"):
        rc = getattr(config.llm, role)
        model = rc.model or _DEFAULT_OLLAMA_MODEL if rc.provider == "ollama" else rc.model
        entries.append((_ROLE_LABEL[role], rc.provider, model or "(unset)"))

    emb_provider = getattr(config.rag, "embedding_provider", "ollama")
    entries.append((_ROLE_LABEL["embedding"], emb_provider, config.rag.embedding_model))
    return entries


def _ollama_required_models(config: AppConfig) -> set[str]:
    """Ollama に要求されるモデル名の集合。"""
    models: set[str] = set()
    for role in ("news_analysis", "price_analysis", "reflection"):
        rc = getattr(config.llm, role)
        if rc.provider == "ollama":
            models.add(rc.model or _DEFAULT_OLLAMA_MODEL)
    if getattr(config.rag, "embedding_provider", "ollama") == "ollama":
        models.add(config.rag.embedding_model)
    return models


def _llamacpp_required_models(config: AppConfig) -> set[str]:
    """llama-swap に要求されるモデル名の集合。"""
    models: set[str] = set()
    for role in ("news_analysis", "price_analysis", "reflection"):
        rc = getattr(config.llm, role)
        if rc.provider == "llamacpp" and rc.model:
            models.add(rc.model)
    if getattr(config.rag, "embedding_provider", "ollama") == "llamacpp":
        models.add(config.rag.embedding_model)
    return models


def _fetch_ollama_models(config: AppConfig) -> set[str] | None:
    """Ollama の /api/tags から登録モデル一覧を取得。疎通失敗時は None。"""
    import httpx

    try:
        resp = httpx.get(f"{config.llm.ollama.base_url}/api/tags", timeout=5)
        resp.raise_for_status()
        return {m["name"] for m in resp.json().get("models", [])}
    except Exception:
        return None


def _fetch_llamacpp_models(config: AppConfig) -> set[str] | None:
    """llama-swap の /v1/models から登録モデル一覧を取得。疎通失敗時は None。"""
    import httpx

    base_url = config.llm.llamacpp.base_url.rstrip("/")
    try:
        resp = httpx.get(f"{base_url}/models", timeout=5)
        resp.raise_for_status()
        return {m["id"] for m in resp.json().get("data", [])}
    except Exception:
        return None


def _ollama_model_available(name: str, available: set[str]) -> bool:
    """Ollama はタグ付き (e.g. "llama3.1:8b") で登録されるため substring 一致。"""
    return any(name in a for a in available)


def _build_llm_table(
    config: AppConfig,
    ollama_available: set[str] | None,
    llamacpp_available: set[str] | None,
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
        else:
            # gemini / openai / claude は HTTP チェック省略 (API キーは別途)
            status_text, passed = f"[yellow]◦ api[/yellow]", True

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


def startup_checks(config: AppConfig) -> bool:
    """起動時チェック（LLMプロバイダー・シンボル疎通・ディレクトリ）を実行して結果を表示する。"""
    # LLM / Embedding チェック (provider ごとに一括で /models を取りにいく)
    ollama_avail = _fetch_ollama_models(config) if _ollama_required_models(config) else set()
    llamacpp_avail = _fetch_llamacpp_models(config) if _llamacpp_required_models(config) else set()
    llm_table, llm_ok = _build_llm_table(config, ollama_avail, llamacpp_avail)

    # Symbols チェック
    instruments = config.enabled_instruments
    trade_count = sum(1 for i in instruments if i.is_tradeable)
    watch_count = len(instruments) - trade_count
    symbol_table, symbol_ok = _build_symbol_table(instruments)

    # ディレクトリ作成
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.rag_db_path.mkdir(parents=True, exist_ok=True)
    (Path(__file__).parent.parent / "logs").mkdir(exist_ok=True)

    # レンダリング
    ok = llm_ok and symbol_ok
    llm_header = Text("LLM / Embedding", style="bold")
    symbol_header = Text(
        f"Instruments  ({trade_count} trade / {watch_count} watch)", style="bold",
    )
    body = Group(
        llm_header,
        llm_table,
        Text(""),  # spacer
        symbol_header,
        symbol_table,
    )

    overall = "[bold green]READY[/bold green]" if ok else "[bold red]FAILED[/bold red]"
    _console.print(Panel(
        body,
        title=f"[bold cyan]FX Paper Trader[/bold cyan]  Startup Checks  {overall}",
        border_style="cyan" if ok else "red",
        padding=(0, 1),
        box=box.ROUNDED,
    ))
    return ok
