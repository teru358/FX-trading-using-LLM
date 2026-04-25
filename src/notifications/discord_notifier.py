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

    async def send_embed(
        self,
        title: str,
        description: str,
        color: int = 0x2ecc71,
        footer: str = "",
        fields: list[dict] | None = None,
    ) -> None:
        """Discord webhook 経由で embed を送信する。

        制約 (Discord の仕様):
          - title     ≤ 256 chars (超えた分は切詰め)
          - description ≤ 4096 chars (超えた分は切詰め)
          - footer.text ≤ 2048 chars
          - fields      ≤ 25 個
          - 1 embed の合計 ≤ 6000 chars
        """
        embed: dict = {
            "title":       title[:256],
            "description": description[:4096],
            "color":       color,
        }
        if footer:
            embed["footer"] = {"text": footer[:2048]}
        if fields:
            embed["fields"] = [
                {
                    "name":   str(f.get("name", ""))[:256],
                    "value":  str(f.get("value", ""))[:1024],
                    "inline": bool(f.get("inline", False)),
                }
                for f in fields[:25]
            ]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._url, json={"embeds": [embed]})
                resp.raise_for_status()
            logger.debug(
                f"[NOTIFY] Discord embed sent "
                f"(title={len(embed['title'])} desc={len(embed['description'])} chars)"
            )
        except Exception as e:
            logger.warning(f"[NOTIFY] Discord embed failed: {e}")
