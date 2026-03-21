from __future__ import annotations

"""Telegram Bot API を使ったプッシュ通知実装。

# セットアップ手順
1. @BotFather に /newbot でBotを作成 → BOT_TOKEN を取得
2. 自分のchat_idを調べる:
   https://api.telegram.org/bot{BOT_TOKEN}/getUpdates
   （Botにメッセージを送った後にアクセスすると chat.id が確認できる）
3. .env に設定:
   TELEGRAM_BOT_TOKEN=1234567890:ABCDEFxxxx
   TELEGRAM_CHAT_ID=987654321
"""

import logging

import httpx

from src.notifications.notifier import NotifierAdapter

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier(NotifierAdapter):
    def __init__(self, bot_token: str, chat_id: str) -> None:
        if not bot_token or not chat_id:
            raise EnvironmentError(
                "TELEGRAM_BOT_TOKEN と TELEGRAM_CHAT_ID を .env に設定してください。"
            )
        self._token = bot_token
        self._chat_id = chat_id
        self._url = _API_BASE.format(token=bot_token)

    async def send(self, message: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._url, json={
                    "chat_id": self._chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                })
                resp.raise_for_status()
            logger.debug(f"[NOTIFY] Telegram sent ({len(message)} chars)")
        except Exception as e:
            logger.warning(f"[NOTIFY] Telegram failed: {e}")
