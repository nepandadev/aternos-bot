import asyncio
import html
import json
import logging
import re
import secrets
import string
from typing import Any

import cloudscraper
import websockets


logger = logging.getLogger(__name__)

ATERNOS_BASE = "https://aternos.org"
WEBSOCKET_URL = "wss://aternos.org/hermes/"

STATUS_NAMES = {
    0: "offline",
    1: "online",
    2: "starting",
    3: "stopping",
    5: "saving",
    6: "loading",
    10: "preparing",
}


class AternosClient:

    def __init__(self, session_cookie: str, server_id: str | None = None):
        self.session_cookie = session_cookie
        self.server_id = server_id

        self.session = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "mobile": False,
            }
        )

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })

        self.session.cookies.set(
            "ATERNOS_SESSION",
            session_cookie,
            domain="aternos.org",
            path="/",
        )

        if server_id:
            self.session.cookies.set(
                "ATERNOS_SERVER",
                server_id,
                domain="aternos.org",
                path="/",
            )

        self.token: str | None = None
        self.sec: str | None = None

        self.server_name: str | None = None
        self.server_info: dict[str, Any] | None = None

        self.status: str | None = None
        self.status_code: int | None = None

        self.websocket = None
        self.websocket_task: asyncio.Task | None = None

        self._running = False
        self._connect_lock = asyncio.Lock()

    # ---------------------------------------------------------
    # HTTP
    # ---------------------------------------------------------

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._running:
                return

            logger.info("Подключение к Aternos...")

            servers_html = await asyncio.to_thread(
                self._get,
                "/servers/",
            )

            if not self.server_id:
                self.server_id = self._extract_server_id(
                    servers_html
                )

            if not self.server_id:
                raise RuntimeError(
                    "Aternos: сервер не найден"
                )

            self.session.cookies.set(
                "ATERNOS_SERVER",
                self.server_id,
                domain="aternos.org",
                path="/",
            )

            self.server_name = self._extract_server_name(
                servers_html,
                self.server_id,
            )

            logger.info(
                "Сервер: %s (%s)",
                self.server_name or "unknown",
                self.server_id,
            )

            info = await self.get_server_info()
            self._apply_status(info)

            self._running = True
            self.websocket_task = asyncio.create_task(
                self._status_poll_loop()
            )

            logger.info(
                "Aternos подключен: %s (%s)",
                self.server_name,
                self.status,
            )

    async def get_server_info(self) -> dict[str, Any]:
        page = await asyncio.to_thread(
            self._get,
            "/server",
        )

        info = self._extract_last_status(page)

        if info is None:
            raise RuntimeError(
                "Aternos: не найден lastStatus на /server"
            )

        self._generate_sec()
        self._extract_ajax_token(page)

        self.server_info = info
        self._apply_status(info)

        return info

    async def start(self) -> dict[str, Any]:
        await self._prepare_ajax()

        return await self._ajax(
            "/ajax/server/start",
            {
                "headstart": "false",
                "access-credits": "false",
                "SEC": self.sec,
                "TOKEN": self.token,
            },
        )

    async def confirm(self) -> dict[str, Any]:
        if not self.token or not self.sec:
            await self._prepare_ajax()

        return await self._ajax(
            "/ajax/server/confirm",
            {
                "headstart": "false",
                "access-credits": "false",
                "SEC": self.sec,
                "TOKEN": self.token,
            },
        )

    async def stop(self) -> dict[str, Any]:
        await self._prepare_ajax()

        return await self._ajax(
            "/ajax/server/stop",
            {
                "SEC": self.sec,
                "TOKEN": self.token,
            },
        )

    async def _prepare_ajax(self) -> None:
        await self.get_server_info()

        if not self.token or not self.sec:
            raise RuntimeError(
                "Aternos: не удалось получить AJAX TOKEN/SEC"
            )

    async def _ajax(
        self,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:

        def request():
            response = self.session.get(
                f"{ATERNOS_BASE}{path}",
                params=params,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{ATERNOS_BASE}/server",
                },
                timeout=20,
            )

            response.raise_for_status()

            try:
                return response.json()
            except ValueError:
                raise RuntimeError(
                    f"Aternos вернул не JSON: "
                    f"{response.status_code} "
                    f"{response.text[:500]}"
                )

        result = await asyncio.to_thread(request)

        if isinstance(result, dict) and result.get("success") is False:
            error = result.get("error") or "unknown error"
            raise RuntimeError(
                f"Aternos API: {error}"
            )

        return result

    def _get(self, path: str) -> str:
        response = self.session.get(
            f"{ATERNOS_BASE}{path}",
            headers={
                "Referer": f"{ATERNOS_BASE}/",
            },
            timeout=20,
        )

        response.raise_for_status()

        return response.text

    # ---------------------------------------------------------
    # AJAX TOKEN / SEC
    # ---------------------------------------------------------

    def _extract_ajax_token(self, page: str) -> None:
        token = None

        match = re.search(
            r'window\[\(\s*"AJA"\s*\+\s*"X_"\s*\+\s*"TOKEN"\s*\)\]\s*=\s*(.+?);',
            page,
            re.DOTALL,
        )
        if match:
            token = self._parse_token_expression(match.group(1))

        if not token:
            match = re.search(
                r'window\s*\[\s*["\']AJAX_TOKEN["\']\s*\]\s*=\s*(.+?);',
                page,
                re.DOTALL,
            )
            if match:
                token = self._parse_token_expression(match.group(1))

        if not token:
            match = re.search(
                r'window\.AJAX_TOKEN\s*=\s*(.+?);',
                page,
                re.DOTALL,
            )
            if match:
                token = self._parse_token_expression(match.group(1))

        if not token:
            match = re.search(
                r'/\*\s*window\s*\[\s*["\']AJAX_TOKEN["\']\s*\]\s*=\s*["\']([^"\']+)["\']\s*\}\s*\*/',
                page,
            )
            if match:
                token = match.group(1)

        if not token:
            match = re.search(
                r'AJAX_TOKEN\s*=\s*["\']([A-Za-z0-9_\-]{16,})["\']',
                page,
            )
            if match:
                token = match.group(1)

        if not token:
            match = re.search(
                r'\?\s*["\']([A-Za-z0-9_\-]{16,})["\']\s*:\s*["\']([A-Za-z0-9_\-]{16,})["\']',
                page,
            )
            if match:
                token = match.group(2)

        if not token:
            scripts = re.findall(
                r'<script[^>]*>(.*?)</script>',
                page,
                re.DOTALL | re.IGNORECASE,
            )
            for script in scripts:
                if "TOKEN" not in script and "AJAX" not in script:
                    continue
                candidates = re.findall(
                    r'["\']([A-Za-z0-9_\-]{20,})["\']',
                    script,
                )
                if candidates:
                    token = max(candidates, key=len)
                    break

        if not token:
            raise RuntimeError("Aternos: AJAX_TOKEN не найден")

        self.token = token
        logger.debug("AJAX_TOKEN получен: %s...", self.token[:8])

    def _parse_token_expression(self, expression: str) -> str | None:
        expression = expression.strip()

        colon = expression.rfind(":")
        if colon != -1:
            value_expression = expression[colon + 1:]
        else:
            value_expression = expression

        parts = re.findall(r'["\']([^"\']*)["\']', value_expression)
        if not parts:
            return None

        token = "".join(parts)
        return token if token else None

    def _generate_sec(self) -> None:
        alphabet = string.ascii_letters + string.digits

        key = "".join(
            secrets.choice(alphabet)
            for _ in range(11)
        ) + "00000"

        value = "".join(
            secrets.choice(alphabet)
            for _ in range(11)
        ) + "00000"

        self.sec = f"{key}:{value}"

        self.session.cookies.set(
            f"ATERNOS_SEC_{key}",
            value,
            domain="aternos.org",
            path="/",
        )

    # ---------------------------------------------------------
    # SERVER INFO
    # ---------------------------------------------------------

    def _extract_server_id(self, page: str) -> str | None:
        matches = re.findall(
            r'<div\s+class="server-body"\s+data-id="([^"]+)"',
            page,
        )

        if not matches:
            return None

        return matches[0].strip()

    def _extract_server_name(
        self,
        page: str,
        server_id: str,
    ) -> str | None:

        pattern = (
            rf'<div\s+class="server-body"\s+'
            rf'data-id="{re.escape(server_id)}"'
            rf'.*?'
            rf'<div\s+class="server-name">\s*'
            rf'(.*?)'
            rf'\s*</div>'
        )

        match = re.search(
            pattern,
            page,
            re.DOTALL,
        )

        if not match:
            return None

        return html.unescape(
            re.sub(r"<[^>]+>", "", match.group(1))
        ).strip()

    def _extract_last_status(
        self,
        page: str,
    ) -> dict[str, Any] | None:

        match = re.search(
            r"\b(?:var|let|const)\s+lastStatus\s*=",
            page,
        )

        if not match:
            return None

        start = page.find("{", match.end())

        if start == -1:
            return None

        depth = 0
        in_string = False
        escape = False

        for index in range(start, len(page)):
            char = page[index]

            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    raw = page[start:index + 1]
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return None

        return None

    def _apply_status(
        self,
        info: dict[str, Any],
    ) -> None:

        self.server_info = info

        raw_status = info.get("status")

        if isinstance(raw_status, int):
            self.status_code = raw_status
            self.status = STATUS_NAMES.get(
                raw_status,
                info.get("lang"),
            )
        else:
            self.status = info.get("lang")

        if info.get("name"):
            self.server_name = info["name"]

    # ---------------------------------------------------------
    # STATUS POLLING (вместо WebSocket — CF даёт 403 на WS)
    # ---------------------------------------------------------

    async def _status_poll_loop(self) -> None:
        while self._running:
            try:
                info = await self.get_server_info()
                old = self.status
                self._apply_status(info)
                if old != self.status:
                    logger.info(
                        "Статус Aternos: %s -> %s",
                        old,
                        self.status,
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Ошибка опроса статуса Aternos")

            await asyncio.sleep(15)

    # ---------------------------------------------------------
    # LIFECYCLE
    # ---------------------------------------------------------

    async def close(self) -> None:
        self._running = False

        task = self.websocket_task
        self.websocket_task = None

        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.websocket = None

        await asyncio.to_thread(
            self.session.close
        )

    @property
    def connected(self) -> bool:
        return self._running

    @property
    def websocket_connected(self) -> bool:
        return False