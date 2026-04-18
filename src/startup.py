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


def _ollama_required_models(config: AppConfig) -> dict[str, str]:
    """Ollama に要求されるモデル {model_name: display_label}。"""
    models: dict[str, str] = {}
    roles = ("news_analysis", "price_analysis", "reflection")

    # デフォルトモデルは Ollama ロールでモデル未指定のものがある場合のみチェック
    needs_default = any(
        getattr(config.llm, role).provider == "ollama" and not getattr(config.llm, role).model
        for role in roles
    )
    if needs_default:
        models[_DEFAULT_OLLAMA_MODEL] = f"Ollama: {_DEFAULT_OLLAMA_MODEL}"

    for role in roles:
        role_cfg = getattr(config.llm, role)
        if role_cfg.provider == "ollama" and role_cfg.model:
            models[role_cfg.model] = f"Ollama: {role_cfg.model}"

    if getattr(config.rag, "embedding_provider", "ollama") == "ollama":
        models[config.rag.embedding_model] = f"Ollama: {config.rag.embedding_model}"
    return models


def _llamacpp_required_models(config: AppConfig) -> dict[str, str]:
    """llama-swap に要求されるモデル {model_name: display_label}。"""
    models: dict[str, str] = {}
    for role in ("news_analysis", "price_analysis", "reflection"):
        role_cfg = getattr(config.llm, role)
        if role_cfg.provider == "llamacpp" and role_cfg.model:
            models[role_cfg.model] = f"llamacpp: {role_cfg.model}"
    if getattr(config.rag, "embedding_provider", "ollama") == "llamacpp":
        models[config.rag.embedding_model] = f"llamacpp: {config.rag.embedding_model}"
    return models


def _check_ollama(config: AppConfig, models_to_check: dict[str, str]) -> list[tuple[str, str, bool]]:
    """Ollama の疎通と必要モデル存在をチェック。"""
    import httpx

    results: list[tuple[str, str, bool]] = []
    try:
        resp = httpx.get(f"{config.llm.ollama.base_url}/api/tags", timeout=5)
        resp.raise_for_status()
        available = [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        results.append((f"Ollama ({config.llm.ollama.base_url})", "[red]UNREACHABLE[/red]", False))
        return results

    for model_name, label in models_to_check.items():
        found = any(model_name in n for n in available)
        status = "[green]OK[/green]" if found else "[red]NOT FOUND[/red]"
        results.append((label, status, found))
    return results


def _check_llamacpp(config: AppConfig, models_to_check: dict[str, str]) -> list[tuple[str, str, bool]]:
    """llama-swap (llama.cpp) の疎通と必要モデル登録をチェック。"""
    import httpx

    results: list[tuple[str, str, bool]] = []
    base_url = config.llm.llamacpp.base_url.rstrip("/")
    try:
        resp = httpx.get(f"{base_url}/models", timeout=5)
        resp.raise_for_status()
        available = {m["id"] for m in resp.json().get("data", [])}
    except Exception:
        results.append((f"llamacpp ({base_url})", "[red]UNREACHABLE[/red]", False))
        return results

    for model_name, label in models_to_check.items():
        found = model_name in available
        status = "[green]OK[/green]" if found else "[red]NOT FOUND[/red]"
        results.append((label, status, found))
    return results


def startup_checks(config: AppConfig) -> bool:
    """起動時チェック（LLMプロバイダー・シンボル疎通・ディレクトリ）を実行して結果を表示する。"""
    checks: list[tuple[str, str, bool]] = []
    ok = True

    # Ollama チェック (ollama を利用するロールがある場合のみ)
    ollama_models = _ollama_required_models(config)
    if ollama_models:
        for item, status, passed in _check_ollama(config, ollama_models):
            checks.append((item, status, passed))
            if not passed:
                ok = False

    # llama.cpp (llama-swap) チェック (llamacpp を利用するロールがある場合のみ)
    llamacpp_models = _llamacpp_required_models(config)
    if llamacpp_models:
        for item, status, passed in _check_llamacpp(config, llamacpp_models):
            checks.append((item, status, passed))
            if not passed:
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
