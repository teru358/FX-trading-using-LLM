# Orchestrator 5-Agent 個別 LLM 設定 — 実装設計 Spec

**Date:** 2026-06-24
**Status:** Draft for review
**Branch:** `feat/planner-watch-loop` (継続)
**Scope:** orchestrator (version2) の5 agent に、それぞれ独立した LLM (provider + model + temperature) を設定できるようにする。既存システム (trading cycle / collectors / views) の3役割は不変。

---

## 0. 背景と前提

現状、orchestrator の各 agent が使う LLM は **3 役割 (`news_analysis` / `price_analysis` / `reflection`) を共有**しており、agent 単位での model 指定はできない。`LLMConfig` は「role_overrides 等の特殊機構は意図的に持たない (シンプル化方針)」(`schema.py:457`)。

本変更は **5 agent それぞれに独立した LLM を設定できるようにする**。設定のシンプル化方針は撤回し、agent 別に provider/model/temperature を選べる柔軟性を優先する。

**現状の 5 agent 実装状況 (調査済):**
- **PlannerAgent** (`planner_agent.py`) — 実 LLM client あり。`bootstrap.py:395` で `create_llm_client(config, "price_analysis")` を注入。
- **ExecutionOpinionAgent** (`execution_opinion_agent.py`) — 実 LLM client あり。**PlannerAgent と同一 client を共有** (`bootstrap.py:398-399`)。
- **NewsAgent** — orchestrator 独立クラスは無く、既存 `news_collector.py` (`create_llm_client(config, "news_analysis")`, `:201`) が LLM 分析を担う (自走収集)。
- **TechnicalAgent** — 既存 `technical_collector.py` (`create_llm_client(config, "price_analysis")`) が担う。spec v2 §6.1 #8 で queue 化 refactor 未実施。
- **ContextSummaryAgent** — **実装クラスが存在しない** (spec v2 §10 の新規作成項目、機能は audit 系に分散)。

**LLM 接続設定の現状:** `LLMConfig.provider_config` は**単一** (`schema.py:460`)。provider 別の LLM 接続設定は存在しない (`config/providers/*.yaml` は price/broker provider 専用で LLM 用ではない)。

### 確定した設計判断 (2026-06-24 brainstorming)

1. **対象:** orchestrator の 5 agent 専用。既存 trading cycle / collectors / views の3役割は不変 (完全移行後に旧システム不要箇所はオミット予定だが本 spec のスコープ外)。
2. **スコープ:** config 基盤は 5 agent 全員分を作る。実 client 配線は**実装済み agent のみ** (Planner / ExecutionOpinion + News/Technical の既存 LLM 経路)。ContextSummary は設定枠のみ用意し配線は将来 (未実装 agent の実装は本 spec に含めない)。
3. **粒度:** agent 別に **provider + model + temperature** を変更可能。
4. **config 分離:** 5 agent 設定を `config/agents.yaml` (新規) に集約 (instruments.yaml / providers/*.yaml と同じドメイン別ファイルパターン)。
5. **LLM 接続設定:** `settings.yaml` の `llm:` に provider 別接続設定 `provider_configs` (provider 名→接続設定) を複数持つ。agent は provider 名で参照。既存単一 `provider_config` は fallback として維持。
6. **fallback:** `config/agents.yaml` 無し / agent エントリ欠落時は既存の役割 model にマップ (後方互換、回帰グリーン)。
7. **Planner / ExecutionOpinion を別 client に分離** (現状は共有)。fallback 時は両方 price_analysis に落ちて従来動作。

### 不変条件

- `config/agents.yaml` が存在しない状態で全既存挙動が無改変 (回帰グリーン)。
- 既存 `create_llm_client(config, role)` と3役割 (trading cycle/collectors/views) は一切変更しない。
- 実 LLM はテストで mock (コスト抑制・provider 接続を起こさない)。

---

## 1. config レイアウト

### settings.yaml の `llm:` 拡張 (provider 別接続設定)

```yaml
llm:
  provider: "claude-cli"           # 既存システム用の既定 provider (不変)
  provider_config:                 # 既存・単一。後方互換 fallback として維持
    command: ""
    timeout_seconds: 120
  provider_configs:                # 新規: provider 名ごとの接続設定 (dict)
    claude-cli:
      command: ""
      isolated_cwd: ""
      timeout_seconds: 120
    llamacpp:
      base_url: "http://localhost:8080/v1"
      timeout_seconds: 120
  news_analysis:                   # 既存3役割 (不変・fallback 用)
    model: "claude-haiku-4-5"
    temperature: 0.3
  price_analysis:
    model: "claude-sonnet-4-6"
    temperature: 0.1
  reflection:
    model: "claude-haiku-4-5"
    temperature: 0.3
```

### config/agents.yaml (新規) — 5 agent の LLM 設定

```yaml
agents:
  planner:
    provider: "claude-cli"
    model: "claude-sonnet-4-6"
    temperature: 0.1
  news:
    provider: "llamacpp"
    model: "llama3.1-8b"
    temperature: 0.3
  technical:
    provider: "llamacpp"
    model: "plutus"
    temperature: 0.1
  execution_opinion:
    provider: "claude-cli"
    model: "claude-sonnet-4-6"
    temperature: 0.1
  context_summary:
    provider: "llamacpp"
    model: "deepseek-r1-8b"
    temperature: 0.3
```

### 解決ロジック

- agent の LLM = `provider_configs[agent.provider]` (接続設定) + `agent.model` + `agent.temperature` を束ねた `AgentLlm` (client + temperature)。
- **fallback (agents.yaml 無し / agent エントリ欠落 / provider 空):** 既存の役割にマップ — planner/execution_opinion/technical→`price_analysis`、news→`news_analysis`、context_summary→`reflection` — し、既存 `provider`/`provider_config` + 役割 temperature を使う。
- **provider 接続設定の解決 (codex High #3):** `agent.provider == config.llm.provider` なら既存単一 `provider_config` を流用。`agent.provider != config.llm.provider` なら `provider_configs[agent.provider]` を**必須**とし、未定義なら ConfigError (空 ProviderConfig への暗黙 fallback は base_url 欠落で壊れた client を生むため禁止)。

---

## 2. config schema (`src/config/schema.py`)

### 新規 `AgentLlmConfig`

```python
@dataclass
class AgentLlmConfig:
    """1 agent の LLM 設定。provider 空欄なら fallback (既存役割 model)。
    provider 指定時は LLMConfig.provider_configs[provider] から接続設定を引く。
    """
    provider: str = ""        # 空欄=fallback
    model: str = ""
    temperature: float = 0.2
```

### 新規 `OrchestratorAgentsLlmConfig`

```python
@dataclass
class OrchestratorAgentsLlmConfig:
    """5 agent の LLM 設定 (config/agents.yaml の agents:)。未指定 agent は fallback。"""
    planner: AgentLlmConfig = field(default_factory=AgentLlmConfig)
    news: AgentLlmConfig = field(default_factory=AgentLlmConfig)
    technical: AgentLlmConfig = field(default_factory=AgentLlmConfig)
    execution_opinion: AgentLlmConfig = field(default_factory=AgentLlmConfig)
    context_summary: AgentLlmConfig = field(default_factory=AgentLlmConfig)
```

### `LLMConfig` に `provider_configs` 追加 (既存 `provider_config` は維持)

```python
    provider_configs: dict[str, ProviderConfig] = field(default_factory=dict)
```

### runtime bundle `AgentLlm` (codex High #1 — temperature の経路)

**問題:** `temperature` は client が保持せず、agent の `chat()` 呼出**毎の引数**である (`PlannerAgent.scan_opportunity(..., temperature=0.1)` → `self._llm.chat(..., temperature=...)`, `planner_agent.py:48,60`)。client だけ返すと agent 別 temperature が届かない。`PlanningPipeline._pipeline` も `scan_opportunity(pair=..., context=...)` を temperature 無しで呼ぶ (`planning_pipeline.py:111`) ため method default 0.1 に落ちる。

**解決:** factory は client 単体ではなく **`AgentLlm` バンドル (client + temperature)** を返し、agent がそれを保持して `chat()` に渡す:

```python
@dataclass
class AgentLlm:
    """agent に注入する LLM ハンドル (client + 解決済 temperature)。"""
    client: "LLMClient"
    temperature: float
```

agent 側 (`PlannerAgent` / `ExecutionOpinionAgent`) は `__init__(self, agent_llm: AgentLlm)` を受け、`scan_opportunity`/`draft` の `temperature` 既定を `self._agent_llm.temperature` にする (method 引数 default 0.1 ではなく注入値を使う)。method 引数 temperature は明示指定時のみ上書き。

### 既存 `OrchestratorAgentsConfig` との関係

- 既存 `OrchestratorAgentsConfig` (`schema.py:665`、`*_enabled` の on/off) は**そのまま維持** = 「動かすか」。
- 新規 `OrchestratorAgentsLlmConfig` = 「どの LLM か」。別 dataclass・別ファイル (agents.yaml) で関心分離。
- 命名: 既存が `OrchestratorAgentsConfig` なので LLM 側は `OrchestratorAgentsLlmConfig` で明確に区別。

### AppConfig 配線 + loader (codex Medium #2 — 具体化)

- `AppConfig` に `agent_llms: OrchestratorAgentsLlmConfig = field(default_factory=OrchestratorAgentsLlmConfig)` を **top-level** で持たせる (`config.agent_llms`)。理由: `config/agents.yaml` は別ファイルで、既存の別ファイル (instruments.yaml / news_sources.yaml) が top-level merge される (`loader.py:132-142` `_merge_split_configs`) のと同じ扱い。
- **loader 実装項目:**
  1. `_merge_split_configs` (`loader.py:134`) の対象ファイル tuple に `"agents.yaml"` を追加 (instruments/news_sources と同列で top-level merge → `base["agents"]` に入る)。
  2. `_build_agent_llms(raw_agents: dict) -> OrchestratorAgentsLlmConfig` を新規追加。`raw.get("agents", {})` の各 agent キー (planner/news/technical/execution_opinion/context_summary) を `AgentLlmConfig(provider=..., model=..., temperature=...)` に組み立てる。未知 agent キーは警告して無視 (typo 検出)。`load_config` の AppConfig 構築 (`loader.py:379-` 周辺) で `agent_llms=_build_agent_llms(...)` を渡す。
  3. `llm.provider_configs` の各エントリは既存 `_build_provider_config(provider, raw)` (`loader.py:289`) で構築 (provider 別 base_url 検証を流用)。

### validation (loader)

- `AgentLlmConfig.provider` が非空なら、その provider 名が既知 (`LLM_PROVIDERS`、`schema.py:15`) であることを検証。不正 provider は起動時 ConfigError。
- `agent.provider != config.llm.provider` のとき `llm.provider_configs[agent.provider]` が存在することを検証 (High #3、無ければ ConfigError)。
- `temperature` 範囲 validation は既存 `LLMRoleConfig` に無いので追加しない (YAGNI)。

---

## 3. factory 拡張 (`src/llm/factory.py`)

### `_build_client` helper を抽出 (DRY)

現行 `create_llm_client` の provider dispatch (`factory.py:43-80`、ollama/llamacpp/claude-cli/gemini/openai を `provider` + `pc` (ProviderConfig) + `model` から構築) を module-level helper に抽出:

```python
def _build_client(provider: str, pc: ProviderConfig, model: str) -> LLMClient:
    """provider + 接続設定 + model から LLMClient を作る (現行 dispatch を抽出)。"""
    # 既存 factory.py:43-80 の provider 分岐をそのまま移す
    ...
```

`create_llm_client` (既存) も `_build_client` を呼ぶ形に整理 (temperature は現行 factory が client に渡していないなら踏襲 — 実装時に現行挙動を確認し、temperature を client に渡しているか合わせる)。

### 新規 `create_agent_llm` (`AgentLlm` バンドルを返す)

```python
AGENT_NAMES = ("planner", "news", "technical", "execution_opinion", "context_summary")

_AGENT_FALLBACK_ROLE = {
    "planner": "price_analysis",
    "news": "news_analysis",
    "technical": "price_analysis",
    "execution_opinion": "price_analysis",
    "context_summary": "reflection",
}

def create_agent_llm(config: AppConfig, agent_name: str) -> AgentLlm:
    """agent 専用 LLM ハンドル (client + temperature)。agents.yaml 設定があれば
    その provider/model/temperature、無ければ fallback 役割に委譲 (後方互換)。"""
    if agent_name not in AGENT_NAMES:
        raise ValueError(f"unknown agent '{agent_name}', expected {AGENT_NAMES}")

    agent_cfg = getattr(config.agent_llms, agent_name)  # AgentLlmConfig
    if not agent_cfg.provider:
        # fallback: 既存役割の client + 役割 temperature
        role = _AGENT_FALLBACK_ROLE[agent_name]
        role_cfg = getattr(config.llm, role)  # LLMRoleConfig (model + temperature)
        return AgentLlm(
            client=create_llm_client(config, role),
            temperature=role_cfg.temperature,
        )

    # provider 別接続設定を解決 (codex High #3: 危険な空 fallback を禁止)
    if agent_cfg.provider == config.llm.provider:
        pc = config.llm.provider_config           # top-level と同一 provider なら既存設定を流用
    else:
        pc = config.llm.provider_configs.get(agent_cfg.provider)
        if pc is None:
            raise ConfigError(
                f"agent '{agent_name}' provider '{agent_cfg.provider}' は "
                f"llm.provider_configs に未定義。接続設定を追加してください "
                f"(空の provider_config への fallback は base_url 欠落で危険)"
            )
    return AgentLlm(
        client=_build_client(agent_cfg.provider, pc, agent_cfg.model),
        temperature=agent_cfg.temperature,
    )
```

> `create_llm_client` (既存・3役割) は不変。`create_agent_llm` が agent 用の新 API。

**provider_configs の検証 (codex High #3):** loader は `llm.provider_configs` の各エントリを **既存 `_build_provider_config(provider, raw)` (`loader.py:289`)** で構築する。この helper は ollama/llamacpp の base_url 空欄を ConfigError にする (`loader.py:289-`)。これにより agent provider が llamacpp で base_url 未設定なら起動時に弾かれる (空 ProviderConfig で壊れた client を作らない)。`agent.provider != config.llm.provider` のとき `provider_configs[agent.provider]` を**必須**とし、単一 `provider_config` への暗黙 fallback は行わない。

---

## 4. bootstrap 配線 (実装済み agent のみ)

### Planner / ExecutionOpinion を別 `AgentLlm` に (本 spec で実配線する唯一の agent)

現状 `bootstrap.py:395` は両 agent で `create_llm_client(config, "price_analysis")` を**共有**。これを agent 別 `AgentLlm` バンドルに分ける:

```python
    planner_llm = create_agent_llm(config, "planner")
    exec_llm = create_agent_llm(config, "execution_opinion")
    return PlanningPipeline(
        planner=PlannerAgent(planner_llm),          # PlannerAgent.__init__(self, agent_llm: AgentLlm)
        execution_agent=ExecutionOpinionAgent(exec_llm),
        risk_gate=RiskGateWorker(spread_max_pips=config.orchestrator.entry.spread_max_pips),
        config=config.orchestrator,
    )
```

- `PlannerAgent` / `ExecutionOpinionAgent` の `__init__` は `AgentLlm` を受け、`self._llm = agent_llm.client` / `self._temperature = agent_llm.temperature` を保持。`scan_opportunity`/`draft` の temperature 既定を `self._temperature` にする (High #1)。
- fallback 時は両方 `price_analysis` の client + 役割 temperature に落ちるので従来と同一動作。
- bootstrap の「同一 LLM を共有する」コメント (`bootstrap.py:386`) を「agent 別 AgentLlm (fallback 時は同一役割)」に更新。

### NewsAgent / TechnicalAgent — 設定枠のみ・本 spec では未使用 (codex High #2)

既存 collector (`news_collector.py:201,155` / `technical_collector.py:417`) が `create_llm_client(config, "news_analysis"/"price_analysis")` と**既存役割 temperature を直接使う**。collector は既存システム (trading cycle) と共有経路であり、per-agent client/temperature 注入点を作るには collector 本体の refactor (optional 注入引数の追加) が必要で、既存システムに波及する。

**本 spec のスコープでは collector を変更しない。** よって `config/agents.yaml` の `news` / `technical` エントリは **設定枠として用意するが、現時点では未使用** (collector は引き続き既存役割 client を使う)。collector への per-agent 注入は別 task (§8)。Review Checklist もこれに合わせる。

### ContextSummaryAgent — 設定枠のみ・未使用

実装クラス無し → 設定枠 (agents.yaml の context_summary) のみ。配線は将来 (agent 実装と共に、§8)。

### 逐次実行への影響

`_build_pipeline` は現状 dispatcher を注入せず `PlannerAgent(llm)` / `ExecutionOpinionAgent(llm)` を直接構築する (`bootstrap.py:395`)。**本 spec は dispatcher 配線を行わない。** planning pipeline 内は逐次 await (Planner→ExecutionOpinion を直列実行) で保たれるが、collector との全体直列化は本 spec の保証外 (LlmDispatcher 配線は別 task)。agent 別 `AgentLlm` 化は pipeline 内の逐次 await を変えない。

---

## 5. テスト方針 (TDD)

1. **schema/config:** `AgentLlmConfig` 既定 (provider 空)、`OrchestratorAgentsLlmConfig` 既定 (全 agent 未指定)、`LLMConfig.provider_configs` 複数 provider 読み込み。
2. **factory (`create_agent_llm`):**
   - agent 設定あり → 指定 provider/model/**temperature** の `AgentLlm` (`_build_client` に正しい値、`AgentLlm.temperature` が agent 設定値、provider は mock)。
   - agent 設定なし (provider 空) → fallback 役割の client + **役割 temperature** の `AgentLlm` (5 agent 全部の fallback マッピング検証)。
   - `agent.provider != top-level provider` かつ `provider_configs` 該当なし → **ConfigError** (High #3: 空 fallback しない)。
   - `agent.provider == top-level provider` → 既存 `provider_config` を流用。
   - 不正 agent 名 → ValueError。不正 provider 名 / base_url 欠落 → ConfigError (loader)。
3. **temperature 経路 (High #1):** `PlannerAgent(AgentLlm(client, temperature=0.4))` を構築し、`scan_opportunity(pair, context)` (temperature 明示なし) で `client.chat` に **0.4** が渡ることを mock client で検証。ExecutionOpinionAgent も同様 (`draft`)。
4. **bootstrap 配線:** agents.yaml あり → Planner と ExecutionOpinion が別 `AgentLlm` (別 client + 別 temperature)。agents.yaml なし → 両方 price_analysis client + 役割 temperature (従来動作)。
5. **loader:** `_merge_split_configs` に agents.yaml 追加 → `config.agent_llms` 構築。ファイル無し → 全 agent fallback。`llm.provider_configs` の base_url 欠落 (llamacpp) → ConfigError。
6. **後方互換回帰:** `config/agents.yaml` 無しで全既存テスト green。既存 `create_llm_client` / 3役割 / collector (news/technical) 不変。

> **News / Technical はテスト対象外 (設定枠のみ・未使用、High #2):** collector を変えないため、news/technical の agents.yaml 設定が LLM 呼出に効くテストは書かない (効かないのが仕様)。設定枠が parse される (schema に載る) ことのみ #1 で確認。

実 LLM はテストで mock。

---

## 6. ドキュメント

- `config/settings.yaml.example` の `llm:` に `provider_configs:` 記載例を追記。
- `config/agents.yaml.example` を新規作成 (5 agent の記載例)。
- 本番 `config/agents.yaml` は作らない (未指定→fallback で従来動作。投入は運用判断)。

---

## 7. Review Checklist

- [ ] `config/agents.yaml` 無しで全既存挙動が無改変 (回帰グリーン)。
- [ ] `create_llm_client` (既存3役割) と trading cycle/collectors/views が不変。
- [ ] `create_agent_llm` が 5 agent それぞれの設定を `AgentLlm` (client + temperature) に解決。
- [ ] **temperature が実際に効く** — Planner/ExecutionOpinion が `AgentLlm.temperature` を `chat()` に渡す (High #1)。
- [ ] fallback マッピング (planner/exec/technical→price_analysis, news→news_analysis, context_summary→reflection) が正しい (client + 役割 temperature)。
- [ ] Planner と ExecutionOpinion が別 `AgentLlm` を受ける (agents.yaml 指定時、別 client + 別 temperature)。
- [ ] `agent.provider != top-level provider` で `provider_configs[agent.provider]` 必須・未定義は ConfigError (High #3、空 fallback しない)。base_url 欠落 (llamacpp) も ConfigError。
- [ ] 不正 provider/agent 名が起動時に検出される。
- [ ] **News/Technical/ContextSummary は設定枠のみで現時点未使用** — collector 未変更、agents.yaml の news/technical 設定は LLM 呼出に効かない (仕様、High #2)。これが明記され誤解を生まないか。

---

## 8. スコープ外 (将来 / 別 task)

- NewsAgent / TechnicalAgent collector の per-agent LLM 化 (collector refactor を伴う)。
- ContextSummaryAgent の実装 + 配線 (spec v2 §10 の新規 agent)。
- TechnicalAgent の LLM queue 化 refactor (spec v2 §6.1 #8)。
- 既存システム (trading cycle/collectors/views) の3役割からの移行・オミット (version2 完全移行 cleanup)。
- agent 別の provider 接続設定 (同一 provider で agent ごとに別 base_url 等) — YAGNI、provider_configs は provider 単位で共有。
- LlmDispatcher の planning pipeline / collector への配線 (全体直列化の保証) — 現状未配線、本 spec の対象外 (codex Medium #1)。

関連: `2026-06-20-orchestrator-agent-loop-design-v2.md` (v2 全体、5 agent 定義)
