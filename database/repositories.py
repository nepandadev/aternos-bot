from datetime import datetime, timezone
import time

from .database import Database


def utc_now():
    return datetime.now(
        timezone.utc
    ).isoformat()


def parse_datetime(value):

    if not value:
        return None

    return datetime.fromisoformat(
        value
    )


def duration_from(
    started_at,
    ended_at=None
):

    start = parse_datetime(
        started_at
    )

    if start is None:
        return 0

    if ended_at:
        end = parse_datetime(
            ended_at
        )
    else:
        end = datetime.now(
            timezone.utc
        )

    return max(
        int(
            (
                end - start
            ).total_seconds()
        ),
        0
    )


def format_duration(seconds):

    if seconds is None:
        return "—"

    seconds = int(seconds)

    days, remainder = divmod(
        seconds,
        86400
    )

    hours, remainder = divmod(
        remainder,
        3600
    )

    minutes, seconds = divmod(
        remainder,
        60
    )

    parts = []

    if days:
        parts.append(
            f"{days} д."
        )

    if hours:
        parts.append(
            f"{hours} ч."
        )

    if minutes:
        parts.append(
            f"{minutes} мин."
        )

    if seconds or not parts:
        parts.append(
            f"{seconds} сек."
        )

    return " ".join(parts)


class UserRepository:

    def __init__(self, db: Database):
        self.db = db

    async def register(self, user):

        now = utc_now()

        await self.db.execute(
            """
            INSERT INTO users (
                user_id,
                username,
                first_name,
                last_name,
                first_seen,
                last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_seen = excluded.last_seen
            """,
            (
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                now,
                now
            )
        )


class LaunchRepository:

    def __init__(self, db):
        self.db = db

    async def create(
        self,
        user_id,
        server_name
    ):

        cursor = await self.db.execute(
            """
            INSERT INTO launch_history (
                user_id,
                server_name,
                requested_at,
                status
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                server_name,
                utc_now(),
                "requested"
            )
        )

        return cursor.lastrowid

    async def mark_started(
        self,
        launch_id
    ):

        await self.db.execute(
            """
            UPDATE launch_history
            SET
                started_at = ?,
                status = ?
            WHERE id = ?
            """,
            (
                utc_now(),
                "starting",
                launch_id
            )
        )

    async def finish(
        self,
        launch_id,
        status,
        duration_seconds=None
    ):

        await self.db.execute(
            """
            UPDATE launch_history
            SET
                finished_at = ?,
                status = ?,
                duration_seconds = ?
            WHERE id = ?
            """,
            (
                utc_now(),
                status,
                duration_seconds,
                launch_id
            )
        )


class QueueRepository:

    def __init__(self, db):
        self.db = db

    async def create(
        self,
        launch_id,
        user_id,
        chat_id,
        timeout_seconds
    ):

        requested_at = utc_now()

        expires_at = datetime.fromtimestamp(
            time.time() + timeout_seconds,
            timezone.utc
        ).isoformat()

        await self.db.execute(
            """
            INSERT INTO queue_confirmations (
                launch_id,
                user_id,
                chat_id,
                requested_at,
                expires_at,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(launch_id)
            DO UPDATE SET
                user_id = excluded.user_id,
                chat_id = excluded.chat_id,
                requested_at = excluded.requested_at,
                expires_at = excluded.expires_at,
                confirmed_at = NULL,
                status = excluded.status
            """,
            (
                launch_id,
                user_id,
                chat_id,
                requested_at,
                expires_at,
                "pending"
            )
        )

    async def get(self, launch_id):

        return await self.db.fetchone(
            """
            SELECT *
            FROM queue_confirmations
            WHERE launch_id = ?
            LIMIT 1
            """,
            (launch_id,)
        )

    async def set_message(
        self,
        launch_id,
        message_id
    ):

        await self.db.execute(
            """
            UPDATE queue_confirmations
            SET message_id = ?
            WHERE launch_id = ?
            """,
            (
                message_id,
                launch_id
            )
        )

    async def mark_confirmed(
        self,
        launch_id
    ):

        cursor = await self.db.execute(
            """
            UPDATE queue_confirmations
            SET
                confirmed_at = ?,
                status = ?
            WHERE launch_id = ?
              AND status = 'pending'
            """,
            (
                utc_now(),
                "confirmed",
                launch_id
            )
        )

        return cursor.rowcount > 0

    async def mark_expired(
        self,
        launch_id
    ):

        await self.db.execute(
            """
            UPDATE queue_confirmations
            SET status = ?
            WHERE launch_id = ?
              AND status = 'pending'
            """,
            (
                "expired",
                launch_id
            )
        )

    @staticmethod
    def is_expired(row):

        if row is None:
            return False

        expires_at = parse_datetime(
            row["expires_at"]
        )

        if expires_at is None:
            return False

        return (
            datetime.now(
                timezone.utc
            ) >= expires_at
        )


class SessionRepository:

    def __init__(self, db):
        self.db = db

    async def get_active(
        self,
        server_name
    ):

        return await self.db.fetchone(
            """
            SELECT *
            FROM server_sessions
            WHERE server_name = ?
              AND ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (server_name,)
        )

    async def create(
        self,
        server_name,
        started_at=None
    ):

        if started_at is None:
            started_at = utc_now()

        existing = await self.get_active(
            server_name
        )

        if existing:
            return existing["id"]

        cursor = await self.db.execute(
            """
            INSERT INTO server_sessions (
                server_name,
                started_at
            )
            VALUES (?, ?)
            """,
            (
                server_name,
                started_at
            )
        )

        session_id = cursor.lastrowid

        print(
            f"[SESSION] Создана сессия "
            f"#{session_id} для {server_name}"
        )

        return session_id

    async def finish(
        self,
        session_id,
        ended_at=None
    ):

        if ended_at is None:
            ended_at = utc_now()

        session = await self.db.fetchone(
            """
            SELECT *
            FROM server_sessions
            WHERE id = ?
            """,
            (session_id,)
        )

        if not session:
            return

        if session["ended_at"] is not None:
            return

        duration = duration_from(
            session["started_at"],
            ended_at
        )

        await self.db.execute(
            """
            UPDATE server_sessions
            SET
                ended_at = ?,
                duration_seconds = ?
            WHERE id = ?
            """,
            (
                ended_at,
                duration,
                session_id
            )
        )

        print(
            f"[SESSION] Сессия #{session_id} завершена. "
            f"Продолжительность: "
            f"{format_duration(duration)}"
        )