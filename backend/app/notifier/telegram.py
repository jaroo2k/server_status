import asyncio
import json
import urllib.error
import urllib.request

API_BASE = "https://api.telegram.org"
TIMEOUT_SECONDS = 15
DEFAULT_RETRY_AFTER_SECONDS = 5


class TelegramError(Exception):
    """Delivery failed. The message is safe to log: it never contains the bot token."""


class TelegramRetryAfter(TelegramError):
    def __init__(self, retry_after: int):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


def _post(url: str, body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _parse(raw: bytes) -> dict:
    try:
        payload = json.loads(raw)
    except ValueError:  # e.g. an HTML error page from a proxy
        return {}
    return payload if isinstance(payload, dict) else {}


class TelegramClient:
    def __init__(self, token: str, chat_id: str, api_base: str = API_BASE):
        self._token = token
        self._chat_id = chat_id
        self._api_base = api_base

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    async def send(self, text: str) -> None:
        url = f"{self._api_base}/bot{self._token}/sendMessage"
        # No parse_mode: log text must never be read as markup.
        body = json.dumps(
            {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        ).encode()
        try:
            status, raw = await asyncio.to_thread(_post, url, body)
        except Exception as e:  # noqa: BLE001
            # `from None`: the original exception's message may hold the URL with the token
            raise TelegramError(self._redact(f"{type(e).__name__}: {e}")) from None

        if status == 200:
            return
        payload = _parse(raw)
        if status == 429:
            parameters = payload.get("parameters") or {}
            raise TelegramRetryAfter(parameters.get("retry_after", DEFAULT_RETRY_AFTER_SECONDS))
        description = payload.get("description", "(no description)")
        raise TelegramError(self._redact(f"HTTP {status}: {description}"))
