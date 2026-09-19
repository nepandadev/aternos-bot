import asyncio

from python_aternos import Client


class AternosService:

    def __init__(self, session_cookie: str):
        self.session_cookie = session_cookie

        self.client = None
        self.account = None
        self.server = None

        # Не даём двум операциям одновременно
        # обращаться к python-aternos.
        self._lock = asyncio.Lock()

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
            return

        self.server = servers[0]

        # Важно для python-aternos.
        self.server.fetch()

    @property
    def server_name(self):
        if self.server is None:
            return None

        return self.server.subdomain

    @property
    def status(self):
        if self.server is None:
            return None

        return str(
            self.server.status
        ).lower()

    async def fetch(self):
        async with self._lock:

            if self.server is None:
                return None

            await asyncio.to_thread(
                self.server.fetch
            )

            return str(
                self.server.status
            ).lower()

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