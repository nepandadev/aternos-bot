import asyncio
import html
import json
import logging
import re
import secrets
import string
from typing import Any

import cloudscraper


logger = logging.getLogger(__name__)

ATERNOS_BASE = "https://aternos.org"

STATUS_NAMES = {
    0: "offline",
    1: "online",
    2: "starting",
    3: "stopping",
    5: "saving",
    6: "loading",
    10: "preparing",
}

_BAD_TOKEN_PARTS = (
    "document",
    "element",
    "timeout",
    "prepend",
    "append",
    "prototype",
    "consent",
    "window",
    "map",
)


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

            servers_html = await asyncio.to_thread(self._get, "/servers/")

            if not self.server_id:
                self.server_id = self._extract_server_id(servers_html)

            if not self.server_id:
                raise RuntimeError("Aternos: сервер не найден")

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

            info = await self.get_server_info(update_token=True)
            self._apply_status(info)

            self._running = True
            self.websocket_task = asyncio.create_task(self._status_poll_loop())

            logger.info(
                "Aternos подключен: %s (%s)",
                self.server_name,
                self.status,
            )

    async def get_server_info(self, update_token: bool = True) -> dict[str, Any]:
        page = await asyncio.to_thread(self._get, "/server")

        info = self._extract_last_status(page)
        if info is None:
            raise RuntimeError("Aternos: не найден lastStatus на /server")

        if update_token:
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
        await self.get_server_info(update_token=True)
        if not self.token or not self.sec:
            raise RuntimeError("Aternos: не удалось получить AJAX TOKEN/SEC")

    async def _ajax(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
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
                    f"{response.status_code} {response.text[:500]}"
                )

        result = await asyncio.to_thread(request)

        if isinstance(result, dict) and result.get("success") is False:
            error = result.get("error") or "unknown error"
            raise RuntimeError(f"Aternos API: {error}")

        return result

    def _get(self, path: str) -> str:
        response = self.session.get(
            f"{ATERNOS_BASE}{path}",
            headers={"Referer": f"{ATERNOS_BASE}/"},
            timeout=20,
        )
        response.raise_for_status()
        return response.text

    # ---------------------------------------------------------
    # AJAX TOKEN / SEC
    # ---------------------------------------------------------

    def _extract_ajax_token(self, page: str) -> None:
        scripts = re.findall(
            r"<script[^>]*>(.*?)</script>",
            page,
            re.DOTALL | re.IGNORECASE,
        )

        target = ""
        for script in scripts:
            if "AJAX_TOKEN" in script or "XAJA" in script or "EKOT_" in script:
                target = script
                break

        if not target:
            raise RuntimeError("Aternos: script с AJAX_TOKEN не найден")

        # Только исполняемый код (после /*...*/)
        exec_part = target.split("*/")[-1]

        token = self._parse_ajax_assignment(exec_part)

        if not self._is_valid_token(token):
            with open("aternos_token_debug.js", "w", encoding="utf-8") as f:
                f.write(target)
            raise RuntimeError(f"Aternos: невалидный AJAX_TOKEN: {token!r}")

        self.token = token
        logger.info("AJAX_TOKEN получен: %s...", self.token[:8])

    def _parse_ajax_assignment(self, exec_part: str) -> str | None:
        # window["AJAX_TOKEN"]=  ИЛИ  window['AJAX_TOKEN']=  ИЛИ  ]=
        m = re.search(
            r'(?:window\s*\[\s*[\'"]AJAX_TOKEN[\'"]\s*\]|\])\s*=\s*',
            exec_part,
        )
        if not m:
            return self._token_candidates(exec_part)

        i = m.end()

        # опциональный !
        while i < len(exec_part) and exec_part[i].isspace():
            i += 1
        if i < len(exec_part) and exec_part[i] == "!":
            i += 1
            while i < len(exec_part) and exec_part[i].isspace():
                i += 1

        # условие в (...)
        if i >= len(exec_part) or exec_part[i] != "(":
            return self._token_candidates(exec_part)

        depth = 0
        while i < len(exec_part):
            ch = exec_part[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1

        while i < len(exec_part) and exec_part[i].isspace():
            i += 1

        if i >= len(exec_part) or exec_part[i] != "?":
            return self._token_candidates(exec_part)

        ternary = exec_part[i + 1:]
        return self._parse_ternary_rhs(ternary)

    def _parse_ternary_rhs(self, ternary: str) -> str | None:
        """
        В браузере condition=true => берём ветку после ':'.
        LEFT : RIGHT
        """
        # Найдём ':' верхнего уровня
        depth = 0
        in_str = False
        quote = ""
        colon = -1
        i = 0
        while i < len(ternary):
            ch = ternary[i]
            if in_str:
                if ch == "\\" and i + 1 < len(ternary):
                    i += 2
                    continue
                if ch == quote:
                    in_str = False
                i += 1
                continue
            if ch in "\"'":
                in_str = True
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == ":" and depth == 0:
                colon = i
                break
            i += 1

        if colon == -1:
            return None

        rhs = ternary[colon + 1:].strip()
        return self._eval_token_expr(rhs)

    def _eval_token_expr(self, expr: str) -> str | None:
        expr = expr.strip().rstrip(";").strip()

        # ("a" + "b" + "c")
        m = re.match(
            r'^\(\s*("[^"]+"\s*(?:\+\s*"[^"]+"\s*)*)\)\s*$',
            expr,
            re.DOTALL,
        )
        if m:
            return "".join(re.findall(r'"([^"]*)"', m.group(1)))

        # "TOKEN"
        m = re.match(r'^"([^"]+)"\s*$', expr)
        if m:
            return m.group(1)

        # ["c","b","a"].reverse().join('')
        m = re.match(
            r'^\[([^\]]+)\]\s*\.\s*reverse\s*\(\s*\)\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)\s*$',
            expr,
            re.DOTALL,
        )
        if m:
            parts = re.findall(r'"([^"]*)"', m.group(1))
            return "".join(reversed(parts))

        # ["a","b"].map(s => s.split('').reverse().join('')).join('')
        m = re.match(
            r'^\[([^\]]+)\]\s*\.\s*map\s*\(\s*s\s*=>\s*s\.split\(\s*[\'"]{0,2}\s*\)'
            r'\.reverse\(\s*\)\.join\(\s*[\'"]{0,2}\s*\)\s*\)\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)\s*$',
            expr,
            re.DOTALL,
        )
        if m:
            parts = re.findall(r'"([^"]*)"', m.group(1))
            return "".join(p[::-1] for p in parts)

        # ["a","b"].join('')
        m = re.match(
            r'^\[([^\]]+)\]\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)\s*$',
            expr,
            re.DOTALL,
        )
        if m:
            parts = re.findall(r'"([^"]*)"', m.group(1))
            return "".join(parts)

        # fallback: все строковые литералы подряд
        parts = re.findall(r'"([^"]*)"', expr)
        if parts:
            joined = "".join(parts)
            if self._is_valid_token(joined):
                return joined

        return None

    def _token_candidates(self, exec_part: str) -> str | None:
        candidates: list[str] = []

        for m in re.finditer(
            r':\s*\(\s*("[^"]+"\s*(?:\+\s*"[^"]+"\s*)*)\)',
            exec_part,
            re.DOTALL,
        ):
            candidates.append("".join(re.findall(r'"([^"]*)"', m.group(1))))

        for m in re.finditer(
            r':\s*\[([^\]]+)\]\s*\.\s*reverse\s*\(\s*\)\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)',
            exec_part,
            re.DOTALL,
        ):
            parts = re.findall(r'"([^"]*)"', m.group(1))
            candidates.append("".join(reversed(parts)))

        for m in re.finditer(
            r':\s*\[([^\]]+)\]\s*\.\s*map\s*\(\s*s\s*=>\s*s\.split\([\'"]{0,2}\)'
            r'\.reverse\(\)\.join\([\'"]{0,2}\)\)\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)',
            exec_part,
            re.DOTALL,
        ):
            parts = re.findall(r'"([^"]*)"', m.group(1))
            candidates.append("".join(p[::-1] for p in parts))

        for m in re.finditer(r':\s*"([A-Za-z0-9]{12,})"', exec_part):
            candidates.append(m.group(1))

        for token in reversed(candidates):
            if self._is_valid_token(token):
                return token
        return None

    def _token_from_script(self, exec_part: str) -> str | None:
        candidates: list[str] = []

        # :["c","b","a"].reverse().join('')
        for m in re.finditer(
            r':\s*\[([^\]]+)\]\s*\.\s*reverse\s*\(\s*\)\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)',
            exec_part,
            re.DOTALL,
        ):
            parts = re.findall(r'"([^"]*)"', m.group(1))
            if parts:
                candidates.append("".join(reversed(parts)))

        # :["a","b","c"].join('')  (без reverse)
        for m in re.finditer(
            r':\s*\[([^\]]+)\]\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)',
            exec_part,
            re.DOTALL,
        ):
            parts = re.findall(r'"([^"]*)"', m.group(1))
            if parts:
                candidates.append("".join(parts))

        # :"REALTOKEN"
        for m in re.finditer(r':\s*"([A-Za-z0-9]{12,})"', exec_part):
            candidates.append(m.group(1))

        # ? "FAKE" : "REAL"  уже покрыто выше через :"
        # Дополнительно: последняя строка вида "TOKEN" после ?
        for m in re.finditer(
            r'\?\s*(?:"[^"]+"|\[[^\]]+\][^:]*)\s*:\s*(?:"([A-Za-z0-9]{12,})"|\[[^\]]+\])',
            exec_part,
            re.DOTALL,
        ):
            if m.group(1):
                candidates.append(m.group(1))

        # Берём последний валидный кандидат (обычно RHS ternary)
        for token in reversed(candidates):
            if self._is_valid_token(token):
                return token

        return candidates[-1] if candidates else None

    def _parse_ternary_token(self, ternary: str) -> str:
        """
        В браузере условие истинно => !(true) == false => берём ветку после ':'.
        Поддерживаемые формы:
          "FAKE" : ["c","b","a"].reverse().join('')
          ["c","b","a"].reverse().join('') : "REAL"
          "FAKE" : "REAL"
        """
        # "FAKE" : [...].reverse().join('')
        match = re.match(
            r'\s*"([^"]+)"\s*:\s*\[([^\]]+)\]\s*\.\s*reverse\s*\(\s*\)'
            r'\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)',
            ternary,
            re.DOTALL,
        )
        if match:
            parts = re.findall(r'"([^"]*)"', match.group(2))
            return "".join(reversed(parts))

        # [...].reverse().join('') : "REAL"
        match = re.match(
            r'\s*\[[^\]]+\]\s*\.\s*reverse\s*\(\s*\)\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)'
            r'\s*:\s*"([^"]+)"',
            ternary,
            re.DOTALL,
        )
        if match:
            return match.group(1)

        # "FAKE" : "REAL"
        match = re.match(
            r'\s*"([^"]+)"\s*:\s*"([^"]+)"',
            ternary,
        )
        if match:
            return match.group(2)

        # [...].join('') : "REAL"  (без reverse)
        match = re.match(
            r'\s*\[([^\]]+)\]\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)\s*:\s*"([^"]+)"',
            ternary,
            re.DOTALL,
        )
        if match:
            return match.group(2)

        # "FAKE" : [...].join('')  (без reverse)
        match = re.match(
            r'\s*"([^"]+)"\s*:\s*\[([^\]]+)\]\s*\.\s*join\s*\(\s*[\'"]{0,2}\s*\)',
            ternary,
            re.DOTALL,
        )
        if match:
            parts = re.findall(r'"([^"]*)"', match.group(2))
            return "".join(parts)

        raise RuntimeError("Aternos: не удалось разобрать AJAX_TOKEN ternary")

    @staticmethod
    def _is_valid_token(token: str | None) -> bool:
        if not token or len(token) < 10:
            return False
        low = token.lower()
        return not any(part in low for part in _BAD_TOKEN_PARTS)

    def _generate_sec(self) -> None:
        alphabet = string.ascii_letters + string.digits

        key = "".join(secrets.choice(alphabet) for _ in range(11)) + "00000"
        value = "".join(secrets.choice(alphabet) for _ in range(11)) + "00000"

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

    def _extract_server_name(self, page: str, server_id: str) -> str | None:
        pattern = (
            rf'<div\s+class="server-body"\s+'
            rf'data-id="{re.escape(server_id)}"'
            rf'.*?'
            rf'<div\s+class="server-name">\s*'
            rf'(.*?)'
            rf'\s*</div>'
        )
        match = re.search(pattern, page, re.DOTALL)
        if not match:
            return None
        return html.unescape(
            re.sub(r"<[^>]+>", "", match.group(1))
        ).strip()

    def _extract_last_status(self, page: str) -> dict[str, Any] | None:
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

    def _apply_status(self, info: dict[str, Any]) -> None:
        self.server_info = info
        raw_status = info.get("status")

        if isinstance(raw_status, int):
            self.status_code = raw_status
            self.status = STATUS_NAMES.get(raw_status, info.get("lang"))
        else:
            self.status = info.get("lang")

        if info.get("name"):
            self.server_name = info["name"]

    # ---------------------------------------------------------
    # STATUS POLLING
    # ---------------------------------------------------------

    async def _status_poll_loop(self) -> None:
        while self._running:
            try:
                info = await self.get_server_info(update_token=False)
                old = self.status
                self._apply_status(info)
                if old != self.status:
                    logger.info("Статус Aternos: %s -> %s", old, self.status)
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
        await asyncio.to_thread(self.session.close)

    @property
    def connected(self) -> bool:
        return self._running

    @property
    def websocket_connected(self) -> bool:
        return False