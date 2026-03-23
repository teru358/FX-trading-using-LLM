from __future__ import annotations

import concurrent.futures
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.config import AppConfig, InstrumentConfig, _DEFAULT_OLLAMA_MODEL

_SYMBOL_CHECK_TIMEOUT = 10  # 全シンボルの並列フェッチ最大待機秒数


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


def startup_checks(config: AppConfig) -> bool:
    """起動時チェック（Ollamaモデル・シンボル疎通・ディレクトリ）を実行して結果を表示する。"""
    checks: list[tuple[str, str, bool]] = []
    ok = True

    # Ollama チェック
    try:
        import httpx

        base_url = config.llm.ollama.base_url
        resp = httpx.get(f"{base_url}/api/tags", timeout=5)
        resp.raise_for_status()
        model_names = [m["name"] for m in resp.json().get("models", [])]

        # チェック対象: ロール別モデル（明示指定分）+ embeddingモデル
        # デフォルトモデルは Ollama ロールでモデル未指定のものがある場合のみチェック
        models_to_check: dict[str, str] = {}
        needs_default = any(
            getattr(config.llm, role).provider == "ollama" and not getattr(config.llm, role).model
            for role in ("news_analysis", "price_analysis", "reflection")
        )
        if needs_default:
            models_to_check[_DEFAULT_OLLAMA_MODEL] = f"Ollama: {_DEFAULT_OLLAMA_MODEL}"
        for role in ("news_analysis", "price_analysis", "reflection"):
            role_cfg = getattr(config.llm, role)
            if role_cfg.provider == "ollama" and role_cfg.model:
                models_to_check[role_cfg.model] = f"Ollama: {role_cfg.model}"
        models_to_check[config.rag.embedding_model] = f"Ollama: {config.rag.embedding_model}"

        for model_name, model_key in models_to_check.items():
            found = any(model_name in n for n in model_names)
            checks.append((model_key, "[green]OK[/green]" if found else "[red]NOT FOUND[/red]", found))
            if not found:
                ok = False
    except Exception:
        checks.append((f"Ollama ({config.llm.ollama.base_url})", "[red]UNREACHABLE[/red]", False))
        ok = False

    # シンボル疎通チェック（並列フェッチ）
    instruments = config.enabled_instruments
    if instruments:
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

        for inst in instruments:
            sym_ok, mode_label = sym_results.get(inst.symbol, (False, "?"))
            if sym_ok:
                status = f"[green]OK[/green]  ({mode_label})"
                checks.append((f"Symbol: {inst.display_name}", status, True))
            elif inst.is_tradeable:
                checks.append((f"Symbol: {inst.display_name}", f"[red]NOT FOUND[/red]  ({mode_label})", False))
                ok = False
            else:
                checks.append((f"Symbol: {inst.display_name}", f"[yellow]WARN[/yellow]  ({mode_label})", True))

    # ディレクトリ作成
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.rag_db_path.mkdir(parents=True, exist_ok=True)
    (Path(__file__).parent.parent / "logs").mkdir(exist_ok=True)

    # 結果表示
    check_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    check_table.add_column("Item", style="dim")
    check_table.add_column("Status")
    for item, status, _ in checks:
        check_table.add_row(item, status)

    overall = "[bold green]READY[/bold green]" if ok else "[bold red]FAILED[/bold red]"
    _console.print(Panel(
        check_table,
        title=f"[bold cyan]FX Paper Trader[/bold cyan]  Startup Checks  {overall}",
        border_style="cyan" if ok else "red",
        padding=(0, 1),
    ))
    return ok
