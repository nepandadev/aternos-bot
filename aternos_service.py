import asyncio
import logging

from aternos_client import AternosClient


logger = logging.getLogger(__name__)


class AternosService:

    def __init__(
        self,
        session_cookie: str,
        server_id: str | None = None,
    ):
        self.session_cookie = session_cookie
        self.server_id = server_id

        self.client: AternosClient | None = None

        self._lock = asyncio.Lock()
        self._connected = False

        self._status = None
        self._server_name = None

    async def connect(self):
        async with self._lock:

            if self._connected:
                return

            self.client = AternosClient(
                session_cookie=self.session_cookie,
                server_id=self.server_id,
            )

            await self.client.connect()

            self._update_state()

            self._connected = True

            logger.info(
                "Aternos подключен: %s (%s)",
                self._server_name,
                self._status,
            )

    def _update_state(self):
        if not self.client:
            return

        self._status = self.client.status
        self._server_name = self.client.server_name

    async def refresh(self):
        """
        Никаких HTTP-запросов.

        Статус приходит через постоянный WebSocket.
        Метод оставлен для совместимости с SessionMonitor.
        """

        if not self._connected:
            await self.connect()
            return

        self._update_state()

    async def start(self):
        if not self.client:
            await self.connect()

        result = await self.client.start()

        self._update_state()

        return result

    async def confirm(self):
        if not self.client:
            await self.connect()

        return await self.client.confirm()

    async def stop(self):
        if not self.client:
            await self.connect()

        result = await self.client.stop()

        self._update_state()

        return result

    async def close(self):
        self._connected = False

        if self.client:
            await self.client.close()

        self.client = None

        self._status = None
        self._server_name = None

    @property
    def status(self):
        return self._status

    @property
    def server_name(self):
        return self._server_name

    @property
    def server_obj(self):
        """
        Совместимость со старым кодом.
        """

        return self.client

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server(self):
        return self.client