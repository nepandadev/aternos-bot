import aiosqlite


class Database:

    def __init__(self, path: str):
        self.path = path
        self.connection = None

    async def init(self):

        self.connection = await aiosqlite.connect(
            self.path
        )

        self.connection.row_factory = (
            aiosqlite.Row
        )

        await self.connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        await self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS launch_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_id INTEGER NOT NULL,
                server_name TEXT NOT NULL,

                requested_at TEXT NOT NULL,
                started_at TEXT,

                finished_at TEXT,

                status TEXT NOT NULL,

                duration_seconds INTEGER,

                FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
            );

            CREATE INDEX IF NOT EXISTS
            idx_launch_history_user
            ON launch_history(user_id);

            CREATE INDEX IF NOT EXISTS
            idx_launch_history_requested
            ON launch_history(requested_at);


            CREATE TABLE IF NOT EXISTS queue_confirmations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                launch_id INTEGER NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,

                message_id INTEGER,

                requested_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,

                confirmed_at TEXT,

                status TEXT NOT NULL,

                FOREIGN KEY (launch_id)
                    REFERENCES launch_history(id),

                FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
            );


            CREATE INDEX IF NOT EXISTS
            idx_queue_confirm_user
            ON queue_confirmations(user_id);


            CREATE INDEX IF NOT EXISTS
            idx_queue_confirm_status
            ON queue_confirmations(status);


            CREATE TABLE IF NOT EXISTS server_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                server_name TEXT NOT NULL,

                started_at TEXT NOT NULL,

                ended_at TEXT,

                duration_seconds INTEGER
            );


            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_active_server_session
            ON server_sessions(server_name)
            WHERE ended_at IS NULL;
            """
        )

        await self.connection.commit()

    async def close(self):

        if self.connection:
            await self.connection.close()

            self.connection = None

    async def execute(
        self,
        query: str,
        parameters: tuple = ()
    ):
        cursor = await self.connection.execute(
            query,
            parameters
        )

        await self.connection.commit()

        return cursor

    async def fetchone(
        self,
        query: str,
        parameters: tuple = ()
    ):
        cursor = await self.connection.execute(
            query,
            parameters
        )

        try:
            return await cursor.fetchone()

        finally:
            await cursor.close()

    async def fetchall(
        self,
        query: str,
        parameters: tuple = ()
    ):
        cursor = await self.connection.execute(
            query,
            parameters
        )

        try:
            return await cursor.fetchall()

        finally:
            await cursor.close()