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
                    if (/保存してチャートに追加/i.test(text)) { btns[i].click(); return text; }
                }
                for (var i = 0; i < btns.length; i++) {
                    var text = btns[i].textContent.trim();
                    if (/^(Add to chart|Update on chart)/i.test(text)) { btns[i].click(); return text; }
                    if (/^(チャートに追加|チャートを更新)/i.test(text)) { btns[i].click(); return text; }
                }
                // フォールバック: saveButton（既存スクリプト更新時）
                for (var i = 0; i < btns.length; i++) {
                    if (btns[i].className.indexOf('saveButton') !== -1 && btns[i].offsetParent !== null) {
                        btns[i].click(); return 'saveButton';
                    }
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

    async def _remove_existing_signal(self) -> int:
        """チャート上の既存 Finance Bot Signal インジケーターを削除する。"""
        removed = await self._cdp.evaluate("""
            (function() {
                try {
                    var chart = window.TradingViewApi._activeChartWidgetWV.value();
                    var studies = chart.getAllStudies();
                    var count = 0;
                    for (var i = studies.length - 1; i >= 0; i--) {
                        if (studies[i].title && /Finance Bot Signal/i.test(studies[i].title)) {
                            chart.removeEntity(studies[i].id);
                            count++;
                        }
                    }
                    return count;
                } catch(e) { return 0; }
            })()
        """)
        if removed:
            logger.info(f"[TV] Removed {removed} existing signal indicator(s)")
        return removed or 0

    async def _new_indicator(self) -> bool:
        """Pine Editorで新規インジケーターを作成し、エディタ状態をリセットする。"""
        result = await self._cdp.evaluate("""
            (function() {
                try {
                    var frame = document.querySelector('iframe[id*="tradingview"]');
                    var api = window.TradingViewApi;
                    if (api && api._pineEditor) {
                        api._pineEditor.newScript('indicator');
                        return 'api';
                    }
                } catch(e) {}
                // フォールバック: メニューから新規作成
                var items = document.querySelectorAll('[class*="menu"] [class*="item"], [role="menuitem"]');
                for (var i = 0; i < items.length; i++) {
                    var t = items[i].textContent.trim();
                    if (/new.*indicator|新規.*インジケーター|Open.*New/i.test(t)) {
                        items[i].click();
                        return 'menu: ' + t;
                    }
                }
                return null;
            })()
        """)
        if result:
            await asyncio.sleep(1)
            logger.debug(f"[TV] New indicator created via {result}")
            return True
        return False

    async def inject_and_compile(self, source: str) -> dict:
        """Pine Script書き込み→コンパイルを一括実行する。

        saveButton（既存スクリプト上書き）がある場合はそのまま上書き。
        ない場合（初回）は既存シグナルを削除してから「チャートに追加」で新規追加。
        """
        if not await self.set_source(source):
            return {"success": False, "errors": ["Failed to set source"]}

        # saveButtonがあれば上書き更新（削除不要）
        has_save_btn = await self._cdp.evaluate("""
            (function() {
                var btns = document.querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {
                    if (btns[i].className.indexOf('saveButton') !== -1 && btns[i].offsetParent !== null)
                        return true;
                }
                return false;
            })()
        """)

        if not has_save_btn:
            # 初回追加: 既存シグナルを削除してから新規追加
            await self._remove_existing_signal()
            await asyncio.sleep(0.5)

        return await self.compile()
