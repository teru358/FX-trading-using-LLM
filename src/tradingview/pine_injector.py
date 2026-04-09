"""Pine Scriptの注入・コンパイル・エラー検出。

tradingview-mcp の pine.js を参考に、最小限の機能をPython CDPで実装。
"""
from __future__ import annotations

import asyncio
import json
import logging

from src.tradingview.cdp_client import CDPClient

logger = logging.getLogger(__name__)

_FIND_MONACO = """
(function findMonacoEditor() {
    var container = document.querySelector('.monaco-editor.pine-editor-monaco');
    if (!container) return null;
    var el = container;
    var fiberKey;
    for (var i = 0; i < 20; i++) {
        if (!el) break;
        fiberKey = Object.keys(el).find(function(k) {
            return k.startsWith('__reactFiber$');
        });
        if (fiberKey) break;
        el = el.parentElement;
    }
    if (!fiberKey) return null;
    var current = el[fiberKey];
    for (var d = 0; d < 15; d++) {
        if (!current) break;
        if (current.memoizedProps && current.memoizedProps.value &&
            current.memoizedProps.value.monacoEnv) {
            var env = current.memoizedProps.value.monacoEnv;
            if (env.editor && typeof env.editor.getEditors === 'function') {
                var editors = env.editor.getEditors();
                if (editors.length > 0) return { found: true };
            }
        }
        current = current.return;
    }
    return null;
})()
"""


class PineInjector:
    """Pine Scriptの注入とコンパイルを行う。"""

    def __init__(self, cdp: CDPClient) -> None:
        self._cdp = cdp

    async def _ensure_editor_open(self) -> bool:
        """Pine Editorパネルが開いているか確認し、閉じていれば開く。"""
        found = await self._cdp.evaluate(_FIND_MONACO)
        if found:
            return True

        await self._cdp.evaluate("""
            (function() {
                try {
                    var bar = window.TradingView.bottomWidgetBar;
                    if (bar && bar.showWidget) { bar.showWidget('pine-editor'); return true; }
                    if (bar && bar.activateScriptEditorTab) { bar.activateScriptEditorTab(); return true; }
                } catch(e) {}
                var btn = document.querySelector('[aria-label="Pine"]')
                    || document.querySelector('[data-name="pine-dialog-button"]');
                if (btn) { btn.click(); return true; }
                return false;
            })()
        """)

        for _ in range(25):
            await asyncio.sleep(0.2)
            found = await self._cdp.evaluate(_FIND_MONACO)
            if found:
                return True
        return False

    async def set_source(self, source: str) -> bool:
        """Pine Scriptをエディタに書き込む。"""
        if not await self._ensure_editor_open():
            logger.warning("[TV] Pine Editor not found")
            return False

        escaped = json.dumps(source)
        result = await self._cdp.evaluate(f"""
            (function() {{
                var container = document.querySelector('.monaco-editor.pine-editor-monaco');
                if (!container) return false;
                var el = container;
                var fiberKey;
                for (var i = 0; i < 20; i++) {{
                    if (!el) break;
                    fiberKey = Object.keys(el).find(function(k) {{
                        return k.startsWith('__reactFiber$');
                    }});
                    if (fiberKey) break;
                    el = el.parentElement;
                }}
                if (!fiberKey) return false;
                var current = el[fiberKey];
                for (var d = 0; d < 15; d++) {{
                    if (!current) break;
                    if (current.memoizedProps && current.memoizedProps.value &&
                        current.memoizedProps.value.monacoEnv) {{
                        var env = current.memoizedProps.value.monacoEnv;
                        if (env.editor && typeof env.editor.getEditors === 'function') {{
                            var editors = env.editor.getEditors();
                            if (editors.length > 0) {{
                                editors[0].setValue({escaped});
                                return true;
                            }}
                        }}
                    }}
                    current = current.return;
                }}
                return false;
            }})()
        """)
        return bool(result)

    async def compile(self) -> dict:
        """エディタのスクリプトをコンパイルしてチャートに追加する。"""
        button = await self._cdp.evaluate("""
            (function() {
                var btns = document.querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {
                    var text = btns[i].textContent.trim();
                    if (/save and add to chart/i.test(text)) { btns[i].click(); return text; }
                }
                for (var i = 0; i < btns.length; i++) {
                    var text = btns[i].textContent.trim();
                    if (/^(Add to chart|Update on chart)/i.test(text)) { btns[i].click(); return text; }
                }
                return null;
            })()
        """)

        if not button:
            return {"success": False, "button": None, "errors": ["Compile button not found"]}

        await asyncio.sleep(2.5)

        errors = await self._cdp.evaluate("""
            (function() {
                var container = document.querySelector('.monaco-editor.pine-editor-monaco');
                if (!container) return [];
                var el = container;
                var fiberKey;
                for (var i = 0; i < 20; i++) {
                    if (!el) break;
                    fiberKey = Object.keys(el).find(function(k) {
                        return k.startsWith('__reactFiber$');
                    });
                    if (fiberKey) break;
                    el = el.parentElement;
                }
                if (!fiberKey) return [];
                var current = el[fiberKey];
                for (var d = 0; d < 15; d++) {
                    if (!current) break;
                    if (current.memoizedProps && current.memoizedProps.value &&
                        current.memoizedProps.value.monacoEnv) {
                        var env = current.memoizedProps.value.monacoEnv;
                        var editors = env.editor.getEditors();
                        if (editors.length > 0) {
                            var model = editors[0].getModel();
                            var markers = env.editor.getModelMarkers({ resource: model.uri });
                            return markers.filter(function(m) { return m.severity >= 8; })
                                .map(function(m) {
                                    return { line: m.startLineNumber, message: m.message };
                                });
                        }
                    }
                    current = current.return;
                }
                return [];
            })()
        """) or []

        return {
            "success": len(errors) == 0,
            "button": button,
            "errors": errors,
        }

    async def inject_and_compile(self, source: str) -> dict:
        """Pine Script書き込み→コンパイルを一括実行する。"""
        if not await self.set_source(source):
            return {"success": False, "errors": ["Failed to set source"]}
        return await self.compile()
