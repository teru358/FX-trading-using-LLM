"""orchestrator agent loop ランタイム (spec §4)。

LLM queue / context builder / runtime を提供する。
発注の有無は `orchestrator.mode` で決まる (`shadow` = 記録のみ、`live` = 実発注)。
"""
