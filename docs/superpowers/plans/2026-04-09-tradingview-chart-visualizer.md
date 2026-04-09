# TradingView チャート可視化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** financeシステムの発注判断（エントリー/SL/TP/方向）をTradingView Desktopのチャートに自動反映する

**Architecture:** Python CDP (Chrome DevTools Protocol) クライアントで TradingView Desktop (port 9222) に接続し、Pine Scriptを注入・コンパイルする。TradingView Desktopが起動していない場合はスキップするオプション機能として実装。

**Tech Stack:** Python 3.12, websockets (CDP通信), httpx (ターゲット発見), Jinja2 (Pine Script テンプレート)

---

## File Structure

```
src/
  tradingview/
    __init__.py           — パッケージ初期化
    cdp_client.py         — CDP接続・evaluate・切断
    pine_injector.py      — Pine Script注入・コンパイル・エラー検出
    chart_control.py      — シンボル変更・チャート状態取得
    script_generator.py   — 発注情報からPine Scriptを生成
prompts/
  pine_signal.j2          — Pine Scriptテンプレート
tests/
  test_script_generator.py — Pine Script生成のユニットテスト
  test_cdp_client.py       — CDPクライアントのユニットテスト（モック）
config/
  settings.yaml            — tradingview セクション追加
```

---

### Task 1: CDP接続クライアント

**Files:**
- Create: `src/tradingview/__init__.py`
- Create: `src/tradingview/cdp_client.py`
- Create: `tests/test_cdp_client.py`

- [ ] **Step 1: Write failing test for target discovery**

```python
# tests/test_cdp_client.py
"""CDPクライアントのテスト（httpx/websocketsをモック）。"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.tradingview.cdp_client import CDPClient


@pytest.mark.asyncio
async def test_discover_chart_target():
    """TradingViewチャートページを正しく発見する。"""
    mock_targets = [
        {"type": "page", "url": "https://www.tradingview.com/chart/abc123", "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/1"},
        {"type": "page", "url": "https://www.google.com", "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/2"},
    ]
    client = CDPClient(port=9222)
    with patch("httpx.AsyncClient") as mock_http:
        mock_resp = AsyncMock()
        mock_resp.json.return_value = mock_targets
        mock_resp.raise_for_status = MagicMock()
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http.return_value)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.return_value.get = AsyncMock(return_value=mock_resp)
        target = await client._discover_target()
    assert target == "ws://localhost:9222/devtools/page/1"


@pytest.mark.asyncio
async def test_discover_no_tradingview():
    """TradingViewページがない場合Noneを返す。"""
    mock_targets = [
        {"type": "page", "url": "https://www.google.com", "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/2"},
    ]
    client = CDPClient(port=9222)
    with patch("httpx.AsyncClient") as mock_http:
        mock_resp = AsyncMock()
        mock_resp.json.return_value = mock_targets
        mock_resp.raise_for_status = MagicMock()
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http.return_value)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.return_value.get = AsyncMock(return_value=mock_resp)
        target = await client._discover_target()
    assert target is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cdp_client.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'src.tradingview'"

- [ ] **Step 3: Create package and implement CDPClient**

```python
# src/tradingview/__init__.py
"""TradingView Desktop連携モジュール（CDP経由）。"""
```

```python
# src/tradingview/cdp_client.py
"""Chrome DevTools Protocol クライアント。

TradingView Desktop (Electron) に CDP で接続し、
JavaScript を評価する最小限のクライアント。
"""
from __future__ import annotations

import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_TV_URL_PATTERN = re.compile(r"tradingview\.com/chart", re.IGNORECASE)


class CDPClient:
    """TradingView Desktop への CDP 接続を管理する。"""

    def __init__(self, host: str = "localhost", port: int = 9222) -> None:
        self._host = host
        self._port = port
        self._ws = None
        self._msg_id = 0

    async def _discover_target(self) -> str | None:
        """CDP ターゲット一覧から TradingView チャートページの WebSocket URL を返す。"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"http://{self._host}:{self._port}/json/list")
                resp.raise_for_status()
                targets = resp.json()
        except Exception as e:
            logger.debug(f"[TV] CDP discovery failed: {e}")
            return None

        for t in targets:
            if t.get("type") == "page" and _TV_URL_PATTERN.search(t.get("url", "")):
                return t.get("webSocketDebuggerUrl")
        return None

    async def connect(self) -> bool:
        """TradingView Desktop に接続する。成功すれば True。"""
        ws_url = await self._discover_target()
        if not ws_url:
            logger.info("[TV] TradingView Desktop not found, skipping")
            return False

        try:
            import websockets
            self._ws = await websockets.connect(ws_url, max_size=10 * 1024 * 1024)
            # Runtime domain を有効化
            await self._send("Runtime.enable", {})
            logger.info("[TV] Connected to TradingView Desktop")
            return True
        except Exception as e:
            logger.warning(f"[TV] CDP connection failed: {e}")
            self._ws = None
            return False

    async def disconnect(self) -> None:
        """CDP 接続を閉じる。"""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _send(self, method: str, params: dict) -> dict:
        """CDP コマンドを送信して結果を返す。"""
        if not self._ws:
            raise RuntimeError("Not connected")
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params}
        await self._ws.send(json.dumps(msg))

        while True:
            raw = await self._ws.recv()
            data = json.loads(raw)
            if data.get("id") == self._msg_id:
                return data.get("result", {})

    async def evaluate(self, expression: str, await_promise: bool = False) -> any:
        """JavaScript 式を評価して結果を返す。"""
        result = await self._send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if "exceptionDetails" in result:
            desc = (result["exceptionDetails"].get("exception", {}).get("description")
                    or result["exceptionDetails"].get("text", "Unknown error"))
            raise RuntimeError(f"JS evaluation error: {desc}")
        return result.get("result", {}).get("value")

    @property
    def is_connected(self) -> bool:
        return self._ws is not None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cdp_client.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/tradingview/__init__.py src/tradingview/cdp_client.py tests/test_cdp_client.py
git commit -m "feat: TradingView CDP接続クライアント"
```

---

### Task 2: Pine Script テンプレート生成

**Files:**
- Create: `src/tradingview/script_generator.py`
- Create: `prompts/pine_signal.j2`
- Create: `tests/test_script_generator.py`

- [ ] **Step 1: Write failing test for script generation**

```python
# tests/test_script_generator.py
"""Pine Script生成のテスト。"""
from src.tradingview.script_generator import generate_signal_pine


class TestGenerateSignalPine:
    def test_long_signal(self):
        script = generate_signal_pine(
            pair="USD/JPY",
            direction="long",
            entry_price=158.250,
            stop_loss=157.500,
            take_profit=160.500,
            confidence=0.85,
            reason="Strong bullish momentum",
        )
        assert "//@version=6" in script
        assert "158.25" in script
        assert "157.5" in script
        assert "160.5" in script
        assert "LONG" in script or "long" in script.lower()

    def test_short_signal(self):
        script = generate_signal_pine(
            pair="EUR/USD",
            direction="short",
            entry_price=1.17000,
            stop_loss=1.17500,
            take_profit=1.16000,
            confidence=0.70,
            reason="Bearish reversal",
        )
        assert "SHORT" in script or "short" in script.lower()
        assert "1.175" in script
        assert "1.16" in script

    def test_hold_signal(self):
        """HOLD時はエントリーラインなし、方向予測のみ表示。"""
        script = generate_signal_pine(
            pair="USD/JPY",
            direction="hold",
            entry_price=0,
            stop_loss=0,
            take_profit=0,
            confidence=0.45,
            reason="No clear signal",
        )
        assert "HOLD" in script
        assert "line.new" not in script or "na" in script
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_script_generator.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Create Pine Script template**

```jinja2
{# prompts/pine_signal.j2 #}
//@version=6
indicator("Finance Bot Signal — {{ pair }}", overlay=true)

// --- Auto-generated: {{ timestamp }} ---
entryPrice  = {{ entry_price }}
stopLoss    = {{ stop_loss }}
takeProfit  = {{ take_profit }}
direction   = "{{ direction }}"
confidence  = {{ confidence }}
reason      = "{{ reason[:80] }}"

// Colors
colEntry = color.new(color.blue, 0)
colSL    = color.new(color.red, 0)
colTP    = color.new(color.green, 0)
colBG    = direction == "long" ? color.new(color.green, 90) : direction == "short" ? color.new(color.red, 90) : color.new(color.gray, 90)

// Label
var label infoLabel = na
if barstate.islast
    txt = direction + " | conf=" + str.tostring(confidence, "#.##") + "\n" + reason
    infoLabel := label.new(bar_index, close, txt, style=label.style_label_left, color=colBG, textcolor=color.white, size=size.normal)

{% if direction != "hold" %}
// Entry / SL / TP lines
if barstate.islast
    line.new(bar_index - 50, entryPrice, bar_index + 10, entryPrice, color=colEntry, width=2)
    line.new(bar_index - 50, stopLoss,   bar_index + 10, stopLoss,   color=colSL,    width=1, style=line.style_dashed)
    line.new(bar_index - 50, takeProfit,  bar_index + 10, takeProfit, color=colTP,    width=1, style=line.style_dashed)
    // Labels
    label.new(bar_index + 12, entryPrice, "Entry " + str.tostring(entryPrice), style=label.style_none, textcolor=colEntry, size=size.small)
    label.new(bar_index + 12, stopLoss,   "SL " + str.tostring(stopLoss),      style=label.style_none, textcolor=colSL,    size=size.small)
    label.new(bar_index + 12, takeProfit,  "TP " + str.tostring(takeProfit),    style=label.style_none, textcolor=colTP,    size=size.small)
{% endif %}
```

- [ ] **Step 4: Implement script generator**

```python
# src/tradingview/script_generator.py
"""financeシステムの発注情報からPine Scriptを生成する。"""
from __future__ import annotations

from datetime import datetime

from src.analysis.prompt_loader import render_prompt


def generate_signal_pine(
    pair: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    confidence: float,
    reason: str,
) -> str:
    """発注シグナルをPine Script indicatorとして生成する。"""
    return render_prompt(
        "pine_signal.j2",
        pair=pair,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        confidence=confidence,
        reason=reason,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_script_generator.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/tradingview/script_generator.py prompts/pine_signal.j2 tests/test_script_generator.py
git commit -m "feat: Pine Script シグナルテンプレート生成"
```

---

### Task 3: Pine Script注入・コンパイル

**Files:**
- Create: `src/tradingview/pine_injector.py`

- [ ] **Step 1: Implement Pine injector**

```python
# src/tradingview/pine_injector.py
"""Pine Scriptの注入・コンパイル・エラー検出。

tradingview-mcp の pine.js を参考に、最小限の機能をPython CDPで実装。
"""
from __future__ import annotations

import asyncio
import json
import logging

from src.tradingview.cdp_client import CDPClient

logger = logging.getLogger(__name__)

# Monaco エディタ発見用 JS (React Fiber ツリー走査)
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

        # bottomWidgetBar経由で開く
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

        # エディタが表示されるまで待機
        for _ in range(25):  # 5秒
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
        """エディタのスクリプトをコンパイルしてチャートに追加する。

        Returns:
            {"success": bool, "button": str|None, "errors": list}
        """
        # ボタンクリック
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

        # コンパイル完了を待機
        await asyncio.sleep(2.5)

        # エラー確認
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
```

- [ ] **Step 2: Commit**

```bash
git add src/tradingview/pine_injector.py
git commit -m "feat: Pine Script注入・コンパイル (CDP)"
```

---

### Task 4: チャート操作（シンボル変更）

**Files:**
- Create: `src/tradingview/chart_control.py`

- [ ] **Step 1: Implement chart control**

```python
# src/tradingview/chart_control.py
"""TradingViewチャート操作（シンボル変更等）。"""
from __future__ import annotations

import asyncio
import json
import logging

from src.tradingview.cdp_client import CDPClient

logger = logging.getLogger(__name__)

# yfinance → TradingView シンボル変換
_SYMBOL_MAP = {
    "USDJPY=X": "FX:USDJPY",
    "EURUSD=X": "FX:EURUSD",
    "GBPUSD=X": "FX:GBPUSD",
}


def to_tv_symbol(yf_symbol: str) -> str:
    """yfinanceシンボルをTradingView形式に変換する。"""
    return _SYMBOL_MAP.get(yf_symbol, yf_symbol)


class ChartControl:
    """TradingViewチャートの操作を行う。"""

    def __init__(self, cdp: CDPClient) -> None:
        self._cdp = cdp

    async def set_symbol(self, symbol: str) -> bool:
        """チャートのシンボルを変更する。"""
        tv_sym = to_tv_symbol(symbol)
        escaped = json.dumps(tv_sym)
        await self._cdp.evaluate(f"""
            (function() {{
                var chart = window.TradingViewApi._activeChartWidgetWV.value();
                chart.setSymbol({escaped}, {{}});
            }})()
        """, await_promise=False)

        # データ読み込み完了を待機
        for _ in range(25):  # 5秒
            await asyncio.sleep(0.2)
            ready = await self._cdp.evaluate("""
                (function() {
                    var spinner = document.querySelector('[class*="loader"]')
                        || document.querySelector('[data-name="loading"]');
                    return !(spinner && spinner.offsetParent !== null);
                })()
            """)
            if ready:
                logger.info(f"[TV] Symbol changed to {tv_sym}")
                return True
        return False

    async def get_symbol(self) -> str | None:
        """現在のチャートシンボルを取得する。"""
        return await self._cdp.evaluate("""
            (function() {
                try {
                    return window.TradingViewApi._activeChartWidgetWV.value().symbol();
                } catch(e) { return null; }
            })()
        """)
```

- [ ] **Step 2: Commit**

```bash
git add src/tradingview/chart_control.py
git commit -m "feat: TradingViewチャートシンボル変更"
```

---

### Task 5: 設定追加 + 取引サイクル統合

**Files:**
- Modify: `config/settings.yaml`
- Modify: `src/config.py`
- Modify: `src/trading_cycle.py`

- [ ] **Step 1: Add tradingview config section**

`config/settings.yaml` に追加:
```yaml
# TradingView Desktop連携（オプション）
tradingview:
  enabled: false                # true で取引判定時にチャートへ自動反映
  cdp_port: 9222                # TradingView Desktop の CDP ポート
```

`src/config.py` に追加:
```python
@dataclass
class TradingViewConfig:
    """TradingView Desktop 連携設定。"""
    enabled: bool = False
    cdp_port: int = 9222
```

`AppConfig` に `tradingview: TradingViewConfig` フィールドを追加。

`load_config()` に読み込み追加:
```python
tv = raw.get("tradingview", {})
tradingview_cfg = TradingViewConfig(
    enabled=tv.get("enabled", False),
    cdp_port=tv.get("cdp_port", 9222),
)
```

- [ ] **Step 2: Integrate into trading cycle Phase 5**

`src/trading_cycle.py` の注文執行後に:
```python
# TradingView チャート反映（オプション）
if config.tradingview.enabled:
    try:
        from src.tradingview.cdp_client import CDPClient
        from src.tradingview.pine_injector import PineInjector
        from src.tradingview.chart_control import ChartControl
        from src.tradingview.script_generator import generate_signal_pine

        tv_cdp = CDPClient(port=config.tradingview.cdp_port)
        if await tv_cdp.connect():
            try:
                chart = ChartControl(tv_cdp)
                injector = PineInjector(tv_cdp)

                # 最後に処理したシグナルのペアに切り替え
                pair_cfg = next((p for p in config.tradeable_instruments if p.symbol == sig.pair), None)
                if pair_cfg:
                    await chart.set_symbol(sig.pair)

                pine = generate_signal_pine(
                    pair=pair_cfg.display_name if pair_cfg else sig.pair,
                    direction=sig.action,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    confidence=sig.confidence,
                    reason=sig.signal_reason,
                )
                result = await injector.inject_and_compile(pine)
                if result["success"]:
                    logger.info(f"[TV] Pine Script injected for {sig.pair}")
                else:
                    logger.warning(f"[TV] Pine compile errors: {result['errors']}")
            finally:
                await tv_cdp.disconnect()
    except Exception as e:
        logger.warning(f"[TV] Chart visualization failed: {e}")
```

- [ ] **Step 3: Run all tests**

Run: `pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add config/settings.yaml src/config.py src/trading_cycle.py
git commit -m "feat: TradingView連携を取引サイクルに統合"
```

---

### Task 6: websockets 依存追加 + settings.yaml.example 同期

**Files:**
- Modify: `pyproject.toml`
- Modify: `config/settings.yaml.example`

- [ ] **Step 1: Add websockets dependency**

`pyproject.toml` の dependencies に追加:
```
"websockets>=13.0",
```

Run: `uv sync`

- [ ] **Step 2: Sync settings.yaml.example**

```yaml
# TradingView Desktop連携（オプション）
tradingview:
  enabled: false                # true で取引判定時にチャートへ自動反映
  cdp_port: 9222                # TradingView Desktop の CDP ポート
```

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock config/settings.yaml.example
git commit -m "chore: websockets依存追加 + settings.yaml.example同期"
```
