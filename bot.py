import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from python_aternos import Client
import telebot
from telebot import types


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

SESSION_COOKIE = os.getenv("ATERNOS_SESSION")
TG_TOKEN = os.getenv("TG_TOKEN")

if not SESSION_COOKIE:
    raise RuntimeError("Не задан ATERNOS_SESSION в .env")

if not TG_TOKEN:
    raise RuntimeError("Не задан TG_TOKEN в .env")


DB_FILE = "bot.db"

# Как часто проверять состояние сервера
MONITOR_INTERVAL = 5

# Максимальное время ожидания запуска после нажатия "Запуск"
MONITOR_TIMEOUT = 15 * 60

# Через сколько секунд удалить временное уведомление
NOTIFICATION_DELETE_DELAY = 60

QUEUE_CONFIRM_TIMEOUT = 10 * 60


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(TG_TOKEN)


# ============================================================
# ATERNOS
# ============================================================

print("Подключение к Aternos...")

client = Client()
client.login_with_session(SESSION_COOKIE)

aternos = client.account
servs = aternos.list_servers()

if not servs:
    print("Доступные серверы не найдены!")
    server = None
else:
    server = servs[0]

    # Важно: сначала fetch(), иначе некоторые свойства
    # python-aternos могут быть недоступны.
    server.fetch()

    print(f"Подключено к серверу: {server.subdomain}")
    print(f"Статус: {server.status}")


# ============================================================
# DATABASE
# ============================================================

def db_connection():
    connection = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_database():
    connection = db_connection()

    try:
        # ----------------------------------------------------
        # Пользователи Telegram
        # ----------------------------------------------------

        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # История запросов на запуск
        # ----------------------------------------------------

        connection.execute("""
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
            )
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_launch_history_user
            ON launch_history(user_id)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_launch_history_requested
            ON launch_history(requested_at)
        """)

        connection.execute("""
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
                FOREIGN KEY (launch_id) REFERENCES launch_history(id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_queue_confirm_user
            ON queue_confirmations(user_id)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_queue_confirm_status
            ON queue_confirmations(status)
        """)

        # ----------------------------------------------------
        # Реальные сессии сервера
        #
        # Одна сессия:
        #
        # online
        #   ↓
        # сервер работает
        #   ↓
        # offline
        #
        # ----------------------------------------------------

        connection.execute("""
            CREATE TABLE IF NOT EXISTS server_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                server_name TEXT NOT NULL,

                started_at TEXT NOT NULL,

                ended_at TEXT,

                duration_seconds INTEGER
            )
        """)

        # Для одного сервера может быть только одна
        # незакрытая сессия.
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_active_server_session
            ON server_sessions(server_name)
            WHERE ended_at IS NULL
        """)

        connection.commit()

    finally:
        connection.close()


# ============================================================
# TIME
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_datetime(value):
    if not value:
        return None

    return datetime.fromisoformat(value)


def duration_from(started_at, ended_at=None):
    start = parse_datetime(started_at)

    if start is None:
        return 0

    if ended_at:
        end = parse_datetime(ended_at)
    else:
        end = datetime.now(timezone.utc)

    seconds = int((end - start).total_seconds())

    return max(seconds, 0)


def format_duration(seconds):
    if seconds is None:
        return "—"

    seconds = int(seconds)

    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []

    if days:
        parts.append(f"{days} д.")

    if hours:
        parts.append(f"{hours} ч.")

    if minutes:
        parts.append(f"{minutes} мин.")

    if seconds or not parts:
        parts.append(f"{seconds} сек.")

    return " ".join(parts)


# ============================================================
# USERS
# ============================================================

def register_user(user):
    connection = db_connection()

    try:
        now = utc_now()

        connection.execute("""
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
        """, (
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            now,
            now
        ))

        connection.commit()

    finally:
        connection.close()


# ============================================================
# LAUNCH HISTORY
# ============================================================

def create_launch(user_id, server_name):
    connection = db_connection()

    try:
        cursor = connection.execute("""
            INSERT INTO launch_history (
                user_id,
                server_name,
                requested_at,
                status
            )
            VALUES (?, ?, ?, ?)
        """, (
            user_id,
            server_name,
            utc_now(),
            "requested"
        ))

        connection.commit()

        return cursor.lastrowid

    finally:
        connection.close()


def mark_launch_started(launch_id):
    connection = db_connection()

    try:
        connection.execute("""
            UPDATE launch_history
            SET
                started_at = ?,
                status = ?
            WHERE id = ?
        """, (
            utc_now(),
            "starting",
            launch_id
        ))

        connection.commit()

    finally:
        connection.close()


def finish_launch(
    launch_id,
    status,
    duration_seconds=None
):
    connection = db_connection()

    try:
        connection.execute("""
            UPDATE launch_history
            SET
                finished_at = ?,
                status = ?,
                duration_seconds = ?
            WHERE id = ?
        """, (
            utc_now(),
            status,
            duration_seconds,
            launch_id
        ))

        connection.commit()

    finally:
        connection.close()


# ============================================================
# QUEUE CONFIRMATION
# ============================================================

def create_queue_confirmation(
    launch_id,
    user_id,
    chat_id,
    timeout_seconds=QUEUE_CONFIRM_TIMEOUT
):
    requested_at = utc_now()
    expires_at = datetime.fromtimestamp(
        time.time() + timeout_seconds,
        timezone.utc
    ).isoformat()

    connection = db_connection()
    try:
        connection.execute("""
            INSERT INTO queue_confirmations (
                launch_id, user_id, chat_id,
                requested_at, expires_at, status
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
        """, (
            launch_id, user_id, chat_id,
            requested_at, expires_at, "pending"
        ))
        connection.commit()
    finally:
        connection.close()


def get_queue_confirmation(launch_id):
    connection = db_connection()
    try:
        return connection.execute("""
            SELECT *
            FROM queue_confirmations
            WHERE launch_id = ?
            LIMIT 1
        """, (launch_id,)).fetchone()
    finally:
        connection.close()


def set_queue_confirmation_message(launch_id, message_id):
    connection = db_connection()
    try:
        connection.execute("""
            UPDATE queue_confirmations
            SET message_id = ?
            WHERE launch_id = ?
        """, (message_id, launch_id))
        connection.commit()
    finally:
        connection.close()


def mark_queue_confirmed(launch_id):
    connection = db_connection()
    try:
        cursor = connection.execute("""
            UPDATE queue_confirmations
            SET confirmed_at = ?, status = ?
            WHERE launch_id = ?
              AND status = 'pending'
        """, (utc_now(), "confirmed", launch_id))
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def mark_queue_confirmation_expired(launch_id):
    connection = db_connection()
    try:
        connection.execute("""
            UPDATE queue_confirmations
            SET status = ?
            WHERE launch_id = ?
              AND status = 'pending'
        """, ("expired", launch_id))
        connection.commit()
    finally:
        connection.close()


def queue_confirmation_expired(row):
    if row is None:
        return False

    expires_at = parse_datetime(row["expires_at"])
    if expires_at is None:
        return False

    return datetime.now(timezone.utc) >= expires_at


# ============================================================
# SERVER SESSIONS
# ============================================================

def get_active_session(server_name):
    connection = db_connection()

    try:
        return connection.execute("""
            SELECT *
            FROM server_sessions
            WHERE server_name = ?
              AND ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
        """, (
            server_name,
        )).fetchone()

    finally:
        connection.close()


def create_server_session(
    server_name,
    started_at=None
):
    if started_at is None:
        started_at = utc_now()

    connection = db_connection()

    try:
        # Проверяем, нет ли уже активной сессии.
        existing = connection.execute("""
            SELECT *
            FROM server_sessions
            WHERE server_name = ?
              AND ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
        """, (
            server_name,
        )).fetchone()

        if existing:
            return existing["id"]

        try:
            cursor = connection.execute("""
                INSERT INTO server_sessions (
                    server_name,
                    started_at
                )
                VALUES (?, ?)
            """, (
                server_name,
                started_at
            ))

            connection.commit()

            session_id = cursor.lastrowid

            print(
                f"[SESSION] Создана сессия #{session_id} "
                f"для {server_name}"
            )

            return session_id

        except sqlite3.IntegrityError:
            # На случай, если другой поток успел создать
            # сессию одновременно.
            existing = connection.execute("""
                SELECT *
                FROM server_sessions
                WHERE server_name = ?
                  AND ended_at IS NULL
                ORDER BY id DESC
                LIMIT 1
            """, (
                server_name,
            )).fetchone()

            if existing:
                return existing["id"]

            raise

    finally:
        connection.close()


def finish_server_session(
    session_id,
    ended_at=None
):
    if ended_at is None:
        ended_at = utc_now()

    connection = db_connection()

    try:
        session = connection.execute("""
            SELECT *
            FROM server_sessions
            WHERE id = ?
        """, (
            session_id,
        )).fetchone()

        if not session:
            return

        if session["ended_at"] is not None:
            return

        duration_seconds = duration_from(
            session["started_at"],
            ended_at
        )

        connection.execute("""
            UPDATE server_sessions
            SET
                ended_at = ?,
                duration_seconds = ?
            WHERE id = ?
        """, (
            ended_at,
            duration_seconds,
            session_id
        ))

        connection.commit()

        print(
            f"[SESSION] Сессия #{session_id} завершена. "
            f"Продолжительность: "
            f"{format_duration(duration_seconds)}"
        )

    finally:
        connection.close()


def get_server_session_stats(server_name):
    connection = db_connection()

    try:
        row = connection.execute("""
            SELECT
                COUNT(*) AS session_count,
                COALESCE(
                    SUM(duration_seconds),
                    0
                ) AS total_duration,
                COALESCE(
                    AVG(duration_seconds),
                    0
                ) AS average_duration
            FROM server_sessions
            WHERE server_name = ?
              AND ended_at IS NOT NULL
        """, (
            server_name,
        )).fetchone()

        active = connection.execute("""
            SELECT *
            FROM server_sessions
            WHERE server_name = ?
              AND ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
        """, (
            server_name,
        )).fetchone()

        return row, active

    finally:
        connection.close()


# ============================================================
# STATUS
# ============================================================

def status_text(status):
    statuses = {
        "offline": "Оффлайн",
        "online": "Онлайн",
        "starting": "Запуск",
        "stopping": "Остановка",
        "loading": "Загрузка",
        "restarting": "Перезапуск",
        "saving": "Сохранение",
        "preparing": "Подготовка",
        "queue": "В очереди",
        "queued": "В очереди",
        "connecting": "Подключение",
        "crashed": "Аварийно остановлен",
    }

    return statuses.get(
        str(status).lower(),
        str(status).capitalize()
    )


def server_status_text():
    if server is None:
        return "<b>Сервер не найден</b>"

    try:
        server.fetch()

        return (
            "<b>Состояние сервера</b>\n\n"
            f"Сервер: <code>{server.subdomain}</code>\n"
            f"Статус: <b>{status_text(server.status)}</b>"
        )

    except Exception as e:
        print(f"[STATUS] Ошибка получения статуса: {e}")

        return (
            "<b>Состояние сервера</b>\n\n"
            "Не удалось получить состояние сервера."
        )


# ============================================================
# KEYBOARD
# ============================================================

def main_keyboard():
    keyboard = types.InlineKeyboardMarkup(
        row_width=2
    )

    keyboard.add(
        types.InlineKeyboardButton(
            "Запуск",
            callback_data="server_start"
        ),
        types.InlineKeyboardButton(
            "Статус",
            callback_data="server_status"
        )
    )

    return keyboard


# ============================================================
# TELEGRAM MESSAGE HELPERS
# ============================================================

def update_panel(
    chat_id,
    message_id,
    text
):
    """
    Пытаемся изменить существующее сообщение.

    Если не получилось:
        1. пытаемся удалить старое;
        2. отправляем новое.
    """

    try:
        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

        return

    except Exception as e:
        print(
            f"[UI] Не удалось изменить сообщение: {e}"
        )

    try:
        bot.delete_message(
            chat_id,
            message_id
        )

    except Exception as e:
        print(
            f"[UI] Не удалось удалить старое сообщение: {e}"
        )

    try:
        bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

    except Exception as e:
        print(
            f"[UI] Не удалось отправить новое сообщение: {e}"
        )


def queue_confirmation_keyboard(launch_id):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(
            "Подтвердить очередь",
            callback_data=f"queue_confirm:{launch_id}"
        )
    )
    return keyboard


def send_queue_confirmation_notification(chat_id, launch_id):
    try:
        message = bot.send_message(
            chat_id,
            "<b>Aternos требует подтверждение очереди</b>\n\n"
            "Нажмите кнопку ниже, чтобы подтвердить участие в очереди "
            "и продолжить запуск сервера.",
            parse_mode="HTML",
            reply_markup=queue_confirmation_keyboard(launch_id)
        )
        set_queue_confirmation_message(
            launch_id,
            message.message_id
        )
        return message.message_id
    except Exception as e:
        print(f"[QUEUE] Не удалось отправить уведомление: {e}")
        return None


def edit_queue_confirmation_message(chat_id, message_id, text):
    if not message_id:
        return
    try:
        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"[QUEUE] Не удалось изменить уведомление: {e}")


def delete_queue_confirmation_message(chat_id, message_id):
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception as e:
        print(f"[QUEUE] Не удалось удалить уведомление: {e}")


def notify_queue_confirmation_expired(chat_id, message_id):
    edit_queue_confirmation_message(
        chat_id,
        message_id,
        "<b>Подтверждение очереди истекло</b>\n\n"
        "Запуск этого сервера отменён, потому что очередь "
        "не была подтверждена вовремя."
    )


def delete_message_later(
    chat_id,
    message_id,
    delay=NOTIFICATION_DELETE_DELAY
):
    def worker():
        time.sleep(delay)

        try:
            bot.delete_message(
                chat_id,
                message_id
            )

        except Exception as e:
            print(
                f"[UI] Не удалось удалить уведомление: {e}"
            )

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


def send_temporary_notification(
    chat_id,
    text
):
    try:
        message = bot.send_message(
            chat_id,
            text,
            parse_mode="HTML"
        )

        delete_message_later(
            chat_id,
            message.message_id
        )

    except Exception as e:
        print(
            f"[UI] Не удалось отправить уведомление: {e}"
        )


# ============================================================
# LAUNCH MONITOR
# ============================================================

monitor_lock = threading.Lock()

monitor_running = False

current_launch_id = None
current_launch_user_id = None


def launch_monitor(
    launch_id,
    user_id,
    chat_id
):
    """
    Отдельный монитор запуска.

    Следит за переходом:

        starting
        ↓
        queue
        ↓
        loading
        ↓
        online
    """

    global monitor_running
    global current_launch_id
    global current_launch_user_id

    start_time = time.time()

    queue_notification_sent = False
    queue_message_id = None
    queue_confirmation_created = False

    try:
        while True:

            elapsed = time.time() - start_time

            if elapsed >= MONITOR_TIMEOUT:

                print(
                    f"[LAUNCH] Таймаут запуска #{launch_id}"
                )

                finish_launch(
                    launch_id,
                    "timeout"
                )

                send_temporary_notification(
                    chat_id,
                    "<b>Запуск не завершён</b>\n\n"
                    "Сервер не перешёл в состояние «Онлайн» "
                    "за отведённое время."
                )

                break

            try:
                if server is None:
                    finish_launch(
                        launch_id,
                        "failed"
                    )

                    send_temporary_notification(
                        chat_id,
                        "<b>Ошибка</b>\n\n"
                        "Сервер не найден."
                    )

                    break

                server.fetch()

                status = str(
                    server.status
                ).lower()

                print(
                    f"[LAUNCH] #{launch_id}: {status}"
                )

                # ------------------------------------------------
                # Очередь
                # ------------------------------------------------

                if status in (
                    "queue",
                    "queued"
                ):
                    if not queue_confirmation_created:
                        queue_confirmation_created = True
                        queue_notification_sent = True

                        create_queue_confirmation(
                            launch_id,
                            user_id,
                            chat_id
                        )

                        queue_message_id = (
                            send_queue_confirmation_notification(
                                chat_id,
                                launch_id
                            )
                        )

                    confirmation = get_queue_confirmation(
                        launch_id
                    )

                    if confirmation is not None:
                        if (
                            confirmation["status"] == "pending"
                            and queue_confirmation_expired(confirmation)
                        ):
                            mark_queue_confirmation_expired(
                                launch_id
                            )

                            finish_launch(
                                launch_id,
                                "queue_confirmation_timeout"
                            )

                            notify_queue_confirmation_expired(
                                confirmation["chat_id"],
                                confirmation["message_id"]
                            )

                            print(
                                f"[QUEUE] Истекло подтверждение "
                                f"launch #{launch_id}"
                            )

                            break

                # ------------------------------------------------
                # Сервер онлайн
                # ------------------------------------------------

                elif status == "online":

                    confirmation = get_queue_confirmation(
                        launch_id
                    )

                    if confirmation is not None:
                        delete_queue_confirmation_message(
                            confirmation["chat_id"],
                            confirmation["message_id"]
                        )

                    online_time = utc_now()

                    duration_seconds = int(
                        time.time() - start_time
                    )

                    finish_launch(
                        launch_id,
                        "success",
                        duration_seconds
                    )

                    # Создаём реальную серверную сессию.
                    #
                    # Если фоновый монитор уже успел её создать,
                    # новая сессия не появится.
                    create_server_session(
                        server.subdomain,
                        started_at=online_time
                    )

                    send_temporary_notification(
                        chat_id,
                        "<b>Сервер запущен</b>\n\n"
                        f"Сервер: <code>{server.subdomain}</code>"
                    )

                    break

                # ------------------------------------------------
                # Краш
                # ------------------------------------------------

                elif status == "crashed":

                    finish_launch(
                        launch_id,
                        "crashed"
                    )

                    send_temporary_notification(
                        chat_id,
                        "<b>Сервер аварийно остановлен</b>"
                    )

                    break

                # ------------------------------------------------
                # Остальные состояния
                # ------------------------------------------------

                else:
                    pass

            except Exception as e:
                print(
                    f"[LAUNCH] Ошибка проверки запуска "
                    f"#{launch_id}: {e}"
                )

            time.sleep(MONITOR_INTERVAL)

    finally:
        with monitor_lock:

            monitor_running = False
            current_launch_id = None
            current_launch_user_id = None


# ============================================================
# SERVER SESSION MONITOR
# ============================================================

session_monitor_running = False


def server_session_monitor():
    """
    Постоянно следит за фактическим состоянием сервера.

    Нужен не только для запусков из Telegram.

    Например:

        Telegram -> запуск
        Aternos -> online
        Telegram -> бот фиксирует начало сессии

    Но также:

        Aternos -> сервер был запущен вручную
        Telegram -> видит online
        Telegram -> создаёт сессию

    И:

        Aternos -> сервер остановили вручную
        Telegram -> видит offline
        Telegram -> закрывает сессию
    """

    global session_monitor_running

    session_monitor_running = True

    print("[SESSION MONITOR] Запущен.")

    while True:

        try:
            if server is None:
                time.sleep(MONITOR_INTERVAL)
                continue

            server.fetch()

            status = str(
                server.status
            ).lower()

            server_name = server.subdomain

            active_session = get_active_session(
                server_name
            )

            # ====================================================
            # ONLINE
            # ====================================================

            if status == "online":

                if active_session is None:

                    session_id = create_server_session(
                        server_name
                    )

                    print(
                        f"[SESSION MONITOR] "
                        f"Обнаружен работающий сервер. "
                        f"Создана сессия #{session_id}"
                    )

            # ====================================================
            # OFFLINE
            # ====================================================

            elif status == "offline":

                if active_session is not None:

                    finish_server_session(
                        active_session["id"]
                    )

            # ====================================================
            # CRASHED
            # ====================================================

            elif status == "crashed":

                # Не закрываем сразу.
                #
                # Aternos может ещё перейти в offline.
                # Сессия будет закрыта тогда.
                pass

            # ====================================================
            # STOPPING / SAVING / RESTARTING
            # ====================================================

            else:

                # Сессия остаётся открытой.
                #
                # Это важно для состояний:
                #
                # stopping
                # saving
                # restarting
                #
                # Мы закрываем сессию только когда сервер
                # действительно стал offline.
                pass

        except Exception as e:

            # Критически важно:
            #
            # ошибка соединения НЕ означает,
            # что сервер остановился.
            #
            # Поэтому сессию здесь НЕ закрываем.

            print(
                f"[SESSION MONITOR] "
                f"Ошибка проверки сервера: {e}"
            )

        time.sleep(MONITOR_INTERVAL)


# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def start_command(message):

    register_user(
        message.from_user
    )

    if server is None:

        bot.send_message(
            message.chat.id,
            "<b>Сервер не найден</b>",
            parse_mode="HTML"
        )

        return

    bot.send_message(
        message.chat.id,
        "<b>Управление сервером</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ============================================================
# CALLBACK: STATUS
# ============================================================

@bot.callback_query_handler(
    func=lambda call: call.data == "server_status"
)
def callback_status(call):

    register_user(
        call.from_user
    )

    try:
        bot.answer_callback_query(
            call.id
        )

    except Exception:
        pass

    text = server_status_text()

    update_panel(
        call.message.chat.id,
        call.message.message_id,
        text
    )


# ============================================================
# CALLBACK: QUEUE CONFIRMATION
# ============================================================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("queue_confirm:")
)
def callback_queue_confirm(call):
    register_user(call.from_user)

    try:
        launch_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(
                call.id,
                "Некорректный запрос",
                show_alert=True
            )
        except Exception:
            pass
        return

    confirmation = get_queue_confirmation(launch_id)

    if confirmation is None:
        try:
            bot.answer_callback_query(
                call.id,
                "Подтверждение очереди не найдено",
                show_alert=True
            )
        except Exception:
            pass
        return

    if int(confirmation["user_id"]) != int(call.from_user.id):
        try:
            bot.answer_callback_query(
                call.id,
                "Подтвердить очередь может только пользователь, запустивший сервер.",
                show_alert=True
            )
        except Exception:
            pass
        return

    if confirmation["status"] != "pending":
        try:
            bot.answer_callback_query(
                call.id,
                "Это подтверждение уже обработано."
            )
        except Exception:
            pass
        return

    if queue_confirmation_expired(confirmation):
        mark_queue_confirmation_expired(launch_id)
        notify_queue_confirmation_expired(
            confirmation["chat_id"],
            confirmation["message_id"]
        )
        try:
            bot.answer_callback_query(
                call.id,
                "Время подтверждения истекло",
                show_alert=True
            )
        except Exception:
            pass
        return

    with monitor_lock:
        active_launch_id = current_launch_id
        active_user_id = current_launch_user_id
        active_monitor = monitor_running

    if (
        not active_monitor
        or active_launch_id != launch_id
        or active_user_id != call.from_user.id
    ):
        try:
            bot.answer_callback_query(
                call.id,
                "Этот запуск уже не активен",
                show_alert=True
            )
        except Exception:
            pass
        return

    if server is None:
        try:
            bot.answer_callback_query(
                call.id,
                "Сервер не найден",
                show_alert=True
            )
        except Exception:
            pass
        return

    # python-aternos предоставляет server.confirm() именно для
    # подтверждения запуска после окончания очереди.
    if not hasattr(server, "confirm"):
        print(
            "[QUEUE] В установленной версии python-aternos "
            "отсутствует метод server.confirm()"
        )
        try:
            bot.answer_callback_query(
                call.id,
                "В установленной версии python-aternos нет подтверждения очереди",
                show_alert=True
            )
        except Exception:
            pass
        return

    try:
        server.fetch()
        current_status = str(server.status).lower()

        if current_status not in ("queue", "queued"):
            if current_status in (
                "starting",
                "loading",
                "preparing",
                "connecting",
                "online"
            ):
                mark_queue_confirmed(launch_id)

                edit_queue_confirmation_message(
                    confirmation["chat_id"],
                    confirmation["message_id"],
                    "<b>Очередь уже подтверждена</b>\n\n"
                    "Aternos продолжил запуск сервера."
                )

                try:
                    bot.answer_callback_query(
                        call.id,
                        "Очередь уже подтверждена"
                    )
                except Exception:
                    pass
                return

            try:
                bot.answer_callback_query(
                    call.id,
                    f"Aternos сейчас: {status_text(current_status)}",
                    show_alert=True
                )
            except Exception:
                pass
            return

        # Реальный вызов подтверждения Aternos.
        server.confirm()

        mark_queue_confirmed(launch_id)

        edit_queue_confirmation_message(
            confirmation["chat_id"],
            confirmation["message_id"],
            "<b>Очередь подтверждена</b>\n\n"
            "Aternos получил подтверждение. Ожидаю запуск сервера..."
        )

        try:
            bot.answer_callback_query(
                call.id,
                "Очередь подтверждена"
            )
        except Exception:
            pass

        print(
            f"[QUEUE] Пользователь {call.from_user.id} "
            f"подтвердил очередь launch #{launch_id}"
        )

    except Exception as e:
        print(
            f"[QUEUE] Ошибка подтверждения launch #{launch_id}: {e}"
        )

        try:
            bot.answer_callback_query(
                call.id,
                "Не удалось подтвердить очередь. Попробуйте ещё раз.",
                show_alert=True
            )
        except Exception:
            pass


# ============================================================
# CALLBACK: START
# ============================================================

@bot.callback_query_handler(
    func=lambda call: call.data == "server_start"
)
def callback_start(call):

    global monitor_running
    global current_launch_id
    global current_launch_user_id

    register_user(
        call.from_user
    )

    # --------------------------------------------------------
    # Проверяем наличие сервера
    # --------------------------------------------------------

    if server is None:

        try:
            bot.answer_callback_query(
                call.id,
                "Сервер не найден",
                show_alert=True
            )
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # Проверяем, не идёт ли уже запуск
    # --------------------------------------------------------

    with monitor_lock:

        if monitor_running:

            try:
                bot.answer_callback_query(
                    call.id,
                    "Запуск сервера уже выполняется"
                )
            except Exception:
                pass

            return

        monitor_running = True
        current_launch_user_id = call.from_user.id

    # --------------------------------------------------------
    # Получаем актуальный статус
    # --------------------------------------------------------

    try:
        server.fetch()

        current_status = str(
            server.status
        ).lower()

    except Exception as e:

        print(
            f"[START] Ошибка получения статуса: {e}"
        )

        with monitor_lock:
            monitor_running = False
            current_launch_user_id = None

        try:
            bot.answer_callback_query(
                call.id,
                "Не удалось получить статус сервера",
                show_alert=True
            )
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # Если сервер уже онлайн
    # --------------------------------------------------------

    if current_status == "online":

        # На всякий случай гарантируем наличие сессии.
        create_server_session(
            server.subdomain
        )

        with monitor_lock:
            monitor_running = False
            current_launch_user_id = None

        try:
            bot.answer_callback_query(
                call.id,
                "Сервер уже запущен"
            )
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # Если сервер уже запускается
    # --------------------------------------------------------

    if current_status in (
        "starting",
        "loading",
        "preparing",
        "connecting",
        "queue",
        "queued"
    ):

        with monitor_lock:
            monitor_running = False
            current_launch_user_id = None

        try:
            bot.answer_callback_query(
                call.id,
                "Сервер уже запускается"
            )
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # Создаём запись запуска
    # --------------------------------------------------------

    launch_id = create_launch(
        call.from_user.id,
        server.subdomain
    )

    with monitor_lock:
        current_launch_id = launch_id

    # --------------------------------------------------------
    # Пытаемся запустить сервер
    # --------------------------------------------------------

    try:

        mark_launch_started(
            launch_id
        )

        server.start()

        print(
            f"[START] Запуск сервера. "
            f"Launch ID: {launch_id}, "
            f"User ID: {call.from_user.id}"
        )

        # ----------------------------------------------------
        # Telegram popup
        # ----------------------------------------------------

        try:
            bot.answer_callback_query(
                call.id,
                "Запуск сервера начат."
            )

        except Exception as e:
            print(
                f"[START] Не удалось показать popup: {e}"
            )

        # ----------------------------------------------------
        # Запускаем монитор запуска
        # ----------------------------------------------------

        threading.Thread(
            target=launch_monitor,
            args=(
                launch_id,
                call.from_user.id,
                call.from_user.id
            ),
            daemon=True
        ).start()

    except Exception as e:

        print(
            f"[START] Ошибка запуска: {e}"
        )

        finish_launch(
            launch_id,
            "failed"
        )

        with monitor_lock:
            monitor_running = False
            current_launch_id = None
            current_launch_user_id = None

        try:
            bot.answer_callback_query(
                call.id,
                "Не удалось запустить сервер",
                show_alert=True
            )

        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

def main():

    print("Инициализация базы данных...")

    init_database()

    print("База данных готова.")

    # --------------------------------------------------------
    # Запускаем постоянный монитор сессий.
    # --------------------------------------------------------

    threading.Thread(
        target=server_session_monitor,
        daemon=True
    ).start()

    print("Монитор сервера запущен.")

    # --------------------------------------------------------
    # Telegram polling
    # --------------------------------------------------------

    print("Запуск Telegram-бота...")

    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30
    )


if __name__ == "__main__":
    main()