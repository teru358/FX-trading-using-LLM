"""Jinja2ベースのプロンプトテンプレートローダー。"""
from __future__ import annotations
from pathlib import Path
import jinja2

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_PROMPTS_DIR)),
    keep_trailing_newline=True,
    autoescape=False,
    undefined=jinja2.StrictUndefined,
)


def render_prompt(template_name: str, **kwargs) -> str:
    """テンプレートをレンダリングして文字列を返す。"""
    template = _env.get_template(template_name)
    return template.render(**kwargs)


def load_prompt(template_name: str) -> str:
    """静的テンプレート（変数なし）を文字列として返す。"""
    return _env.get_template(template_name).render()
