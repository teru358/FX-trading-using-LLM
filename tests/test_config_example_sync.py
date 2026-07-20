"""config/*.yaml.example と config/*.yaml のキー整合性チェック。

開発時に `config/settings.yaml` (個人設定) にキーを追加したが `.example`
への伝播を忘れる、というレグレッションを検出する。

また `.example` ファイル群だけで AppConfig が構築できることも確認し、
新規利用者が .example をコピーして使える保証を得る。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"

PAIRS = [
    ("settings.yaml",      "settings.yaml.example"),
    ("instruments.yaml",   "instruments.yaml.example"),
    ("news_sources.yaml",  "news_sources.yaml.example"),
]


def _flatten_keys(node, prefix: str = "") -> set[str]:
    """YAML 辞書構造をドット区切りのフルキー集合に展開する。

    リストの中身は展開しない (要素構造の差はユーザー設定依存のため)。
    """
    keys: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            full = f"{prefix}.{k}" if prefix else k
            keys.add(full)
            keys |= _flatten_keys(v, full)
    return keys


# ── キー欠損チェック ──────────────────────────────────────────


# Loader が起動時に warn しつつ silently 落とす deprecated キー。
# 個人設定ファイルに残っていても .example に書き戻す必要はない。
_DEPRECATED_KEYS_ALLOWED_IN_REAL: set[str] = {
    "trading.max_total_positions",
    "trading.max_positions_per_currency_group",
    "trading.max_same_direction_per_group",
    "trading.trading_mode",
    "trading.initial_balance",
    "trading.drawdown_kill_switch_lookback_days",
}


@pytest.mark.parametrize("real_name,example_name", PAIRS)
def test_example_has_all_keys_from_real(real_name, example_name):
    """個人設定ファイル (*.yaml) のキーは全て *.yaml.example にも存在すること。

    個人設定ファイルが存在しない環境 (CI / 新規 clone) ではスキップ。
    Loader 側で warn + drop される deprecated キーは比較対象外。
    """
    real = CONFIG_DIR / real_name
    example = CONFIG_DIR / example_name

    if not real.exists():
        pytest.skip(f"{real_name} not present")
    assert example.exists(), f"{example_name} must exist in repo"

    real_keys = _flatten_keys(yaml.safe_load(real.read_text(encoding="utf-8")) or {})
    example_keys = _flatten_keys(yaml.safe_load(example.read_text(encoding="utf-8")) or {})

    missing = (real_keys - example_keys) - _DEPRECATED_KEYS_ALLOWED_IN_REAL
    assert not missing, (
        f"{real_name} にあるが {example_name} にないキー: {sorted(missing)}\n"
        f"新機能を追加したら .example にも雛形を書き足してください。"
    )


# ── .example 読み込み動作チェック ─────────────────────────────


@pytest.mark.skip(
    reason="config migration 2026-07-20: llm.yaml.example / strategy.yaml.example は "
           "Task 7 で作成する。それまで .example だけでは必須ファイルが揃わない。"
)
def test_load_config_from_examples(tmp_path):
    """*.yaml.example ファイル群だけで AppConfig が構築できること。

    新規利用者が .example をコピーして使える保証。
    """
    from src.config import load_config

    # tmp_path に .example を .yaml としてコピー (load_config はファイル名で判定するため)
    for _, example_name in PAIRS:
        src = CONFIG_DIR / example_name
        if not src.exists():
            pytest.skip(f"{example_name} not found")
        # "settings.yaml.example" → "settings.yaml"
        dst_name = example_name.removesuffix(".example")
        shutil.copy2(src, tmp_path / dst_name)

    config_path = tmp_path / "settings.yaml"
    config = load_config(config_path)

    # 主要属性がアクセス可能であることだけ確認 (値の妥当性までは踏み込まない)
    assert config.trading is not None
    assert config.llm is not None
    assert config.news_collection is not None
    assert config.rag is not None
    assert config.schedule is not None


def test_settings_do_not_carry_removed_position_management_keys() -> None:
    import yaml
    from pathlib import Path

    files = [
        Path("config/settings.yaml"),
        Path("config/settings.yaml.example"),
        Path("docs/deploy-stick-pc/settings.yaml"),
    ]
    removed = {
        "initial_balance",
        "trading_mode",
        "max_holding_days",
        "timeout_min_progress_pct",
        "profit_lock_min_progress_pct",
        "profit_lock_score_floor",
        "legacy_profit_lock_enabled",
        "trailing_stop_enabled",
        "trailing_stop_breakeven_pct",
        "trailing_stop_activation_pct",
        "trailing_stop_distance_ratio",
    }
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        trading = data.get("trading", {}) or {}
        assert not (removed & set(trading)), f"{path} still has removed keys: {removed & set(trading)}"


@pytest.mark.skip(
    reason="config migration 2026-07-20: agents.yaml.example は llm.yaml.example の "
           "agents: へ統合する。Task 7 で llm.yaml.example 前提のテストに置換する。"
)
def test_agents_example_loads_with_settings_example(tmp_path) -> None:
    """agents.yaml.example をそのままコピーしても既定 settings.yaml.example で load できる。

    Task 5.5 の strict 検証 (cross-provider は provider_configs 必須) に対し、
    .example が内部整合していることを保証する (有効行は claude-cli = top-level 同一)。
    """
    from src.config import load_config

    examples = [
        ("settings.yaml.example", "settings.yaml"),
        ("instruments.yaml.example", "instruments.yaml"),
        ("news_sources.yaml.example", "news_sources.yaml"),
        ("agents.yaml.example", "agents.yaml"),
    ]
    for example_name, dst_name in examples:
        src = CONFIG_DIR / example_name
        if not src.exists():
            pytest.skip(f"{example_name} not found")
        shutil.copy2(src, tmp_path / dst_name)

    cfg = load_config(tmp_path / "settings.yaml")
    # 有効化されている 2 agent が読める (claude-cli = top-level provider)
    assert cfg.agent_llms.planner.provider == "claude-cli"
    assert cfg.agent_llms.execution_opinion.provider == "claude-cli"
    # コメントアウトされた 3 agent は fallback (provider 空)
    assert cfg.agent_llms.news.provider == ""
    assert cfg.agent_llms.technical.provider == ""
    assert cfg.agent_llms.context_summary.provider == ""
