import asyncio
import time

from python_aternos import Client


class AternosService:

    def __init__(
        self,
        session_cookie: str,
        fetch_interval: float = 5.0,
    ):
        self.session_cookie = session_cookie
        self.fetch_interval = fetch_interval

        self.client = None
        self.account = None
        self.server = None

        self._lock = asyncio.Lock()

        self._status = None
        self._server_name = None
        self._last_fetch = 0.0

        self._monitor_task = None

    # ==================================================
    # CONNECT
    # ==================================================

    async def connect(self):

        async with self._lock:

            await asyncio.to_thread(
                self._connect_sync
            )

    def _connect_sync(self):

        client = Client()

        client.login_with_session(
            self.session_cookie
        )

        account = client.account

        servers = account.list_servers()

        self.client = client
        self.account = account

        if not servers:

            self.server = None
            self._status = None
            self._server_name = None

            return

        self.server = servers[0]

        # Первый fetch после авторизации.
        self.server.fetch()

        self._server_name = (
            self.server.subdomain
        )

        self._status = (
            str(
                self.server.status
            ).lower()
        )

        self._last_fetch = (
            time.monotonic()
        )

    # ==================================================
    # STATE
    # ==================================================

    @property
    def server_name(self):

        return self._server_name

    @property
    def status(self):

        return self._status

    @property
    def last_fetch(self):

        return self._last_fetch

    # ==================================================
    # SINGLE FETCH
    # ==================================================

    async def fetch(
        self,
        force=False,
    ):

        async with self._lock:

            if self.server is None:
                return None

            now = time.monotonic()

            if (
                not force
                and (
                    now - self._last_fetch
                    < self.fetch_interval
                )
            ):
                return self._status

            await asyncio.to_thread(
                self.server.fetch
            )

            self._status = (
                str(
                    self.server.status
                ).lower()
            )

            self._server_name = (
                self.server.subdomain
            )

            self._last_fetch = (
                time.monotonic()
            )

            return self._status

    # ==================================================
    # BACKGROUND MONITOR
    # ==================================================

    async def monitor(self):

        print(
            "[ATERNOS] Монитор запущен."
        )

        while True:

            try:

                await self.fetch(
                    force=True
                )

            except asyncio.CancelledError:

                raise

            except Exception as e:

                print(
                    f"[ATERNOS] Ошибка fetch: {e}"
                )

            await asyncio.sleep(
                self.fetch_interval
            )

    # ==================================================
    # MONITOR CONTROL
    # ==================================================

    def start_monitor(self):

        if (
            self._monitor_task is not None
            and not self._monitor_task.done()
        ):
            return

        self._monitor_task = (
            asyncio.create_task(
                self.monitor()
            )
        )

    async def stop_monitor(self):

        if self._monitor_task is None:
            return

        if not self._monitor_task.done():

            self._monitor_task.cancel()

        await asyncio.gather(
            self._monitor_task,
            return_exceptions=True
        )

        self._monitor_task = None

    # ==================================================
    # SERVER ACTIONS
    # ==================================================

    async def start(self):

        async with self._lock:

            if self.server is None:

                raise RuntimeError(
                    "Сервер Aternos не найден"
                )

            await asyncio.to_thread(
                self.server.start
            )

    async def confirm(self):

        async with self._lock:

            if self.server is None:

                raise RuntimeError(
                    "Сервер Aternos не найден"
                )

            if not hasattr(
                self.server,
                "confirm"
            ):

                raise RuntimeError(
                    "В установленной версии "
                    "python-aternos отсутствует "
                    "server.confirm()"
                )

            await asyncio.to_thread(
                self.server.confirm
            )