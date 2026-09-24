import asyncio
import logging
import os
import gc

import psutil
import tracemalloc

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import settings
from database import Database
from aternos_service import AternosService

from services.launch_service import LaunchService
from services.session_monitor import SessionMonitor

from handlers import setup_routers


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    )
)

async def memory_monitor():

    process = psutil.Process(
        os.getpid()
    )

    snapshot_number = 0

    while True:

        try:

            memory = process.memory_info()

            rss_mb = (
                memory.rss / 1024 / 1024
            )

            vms_mb = (
                memory.vms / 1024 / 1024
            )

            print(
                f"[MEMORY] "
                f"RSS={rss_mb:.2f} MB | "
                f"VMS={vms_mb:.2f} MB | "
                f"Objects={len(gc.get_objects())}"
            )

            snapshot_number += 1

            # Снимок каждые 30 секунд
            if snapshot_number % 30 == 0:

                snapshot = (
                    tracemalloc.take_snapshot()
                )

                top_stats = snapshot.statistics(
                    "lineno"
                )

                print(
                    "\n========== MEMORY TOP =========="
                )

                for stat in top_stats[:10]:

                    print(
                        stat
                    )

                print(
                    "================================\n"
                )

        except Exception as e:

            print(
                f"[MEMORY] "
                f"Ошибка: {e}"
            )

        await asyncio.sleep(1)

async def main():

    # ==========================================
    # DATABASE
    # ==========================================

    print(
        "Инициализация базы данных..."
    )

    db = Database(
        settings.db_file
    )

    await db.init()

    print(
        "База данных готова."
    )

    # ==========================================
    # ATERNOS
    # ==========================================

    print(
        "Подключение к Aternos..."
    )

    aternos = AternosService(
        settings.aternos_session
    )

    await aternos.connect()

    if aternos.server is None:

        print(
            "Доступные серверы не найдены!"
        )

        await db.close()

        return

    print(
        f"Подключено к серверу: "
        f"{aternos.server_name}"
    )

    print(
        f"Статус: {aternos.status}"
    )

    # ==========================================
    # TELEGRAM
    # ==========================================

    bot = Bot(
        token=settings.tg_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )

    dp = Dispatcher()

    # ==========================================
    # SERVICES
    # ==========================================

    launch_service = LaunchService(
        bot=bot,
        db=db,
        aternos=aternos,

        monitor_interval=(
            settings.monitor_interval
        ),

        monitor_timeout=(
            settings.monitor_timeout
        ),

        queue_confirm_timeout=(
            settings.queue_confirm_timeout
        ),

        notification_delete_delay=(
            settings.notification_delete_delay
        )
    )

    session_monitor = SessionMonitor(
        db=db,
        aternos=aternos,
        interval=(
            settings.monitor_interval
        )
    )

    # ==========================================
    # DEPENDENCIES FOR HANDLERS
    # ==========================================

    dp["db"] = db
    dp["aternos"] = aternos
    dp["launch_service"] = launch_service

    # ==========================================
    # ROUTERS
    # ==========================================

    setup_routers(
        dp
    )

    # ==========================================
    # BACKGROUND MONITOR
    # ==========================================

    session_task = asyncio.create_task(
        session_monitor.run()
    )

    memory_task = None

    if os.getenv("MEMORY_MONITOR") == "1":
        tracemalloc.start(10)
        memory_task = asyncio.create_task(
            memory_monitor()
        )

    print(
        "Монитор сервера запущен."
    )

    print(
        "Запуск Telegram-бота..."
    )

    # ==========================================
    # POLLING
    # ==========================================

    try:

        await dp.start_polling(
            bot
        )

    finally:

        launch_service.stop()

        if memory_task is not None:
            memory_task.cancel()

            await asyncio.gather(
                memory_task,
                return_exceptions=True
            )

        session_task.cancel()

        await asyncio.gather(
            session_task,
            return_exceptions=True
        )

        await bot.session.close()

        await db.close()


if __name__ == "__main__":
    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        pass