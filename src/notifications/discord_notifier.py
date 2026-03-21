from __future__ import annotations

"""Discord Webhook を使ったプッシュ通知実装。

# セットアップ手順
1. Discord サーバーの通知を受け取りたいチャンネルを開く
2. チャンネル設定 > 連携サービス > ウェブフック > 新しいウェブフック
3. Webhook URL をコピー
4. .env に設定:
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy

# スマホへの通知
Discord モバイルアプリの通知設定でそのチャンネルをミュート解除しておくこと。
"""

import logging

import httpx

from src.notifications.notifier import NotifierAdapter

logger = logging.getLogger(__name__)


class DiscordNotifier(NotifierAdapter):
    def __init__(self, webhook_url: str) -> None:
        if not webhook_url:
            raise EnvironmentError(
                "DISCORD_WEBHOOK_URL を .env に設定してください。"
            )
        self._url = webhook_url

    async def send(self, message: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._url, json={"content": message})
                resp.raise_for_status()
            logger.debug(f"[NOTIFY] Discord sent ({len(message)} chars)")
        except Exception as e:
            logger.warning(f"[NOTIFY] Discord failed: {e}")
