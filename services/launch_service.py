import asyncio


class LaunchService:

    def __init__(
        self,
        bot,
        db,
        aternos,
        monitor_interval,
        monitor_timeout,
        queue_confirm_timeout,
        notification_delete_delay
    ):

        self.bot = bot
        self.aternos = aternos

        self.monitor_interval = (
            monitor_interval
        )

        self.monitor_timeout = (
            monitor_timeout
        )

        self.queue_confirm_timeout = (
            queue_confirm_timeout
        )

        self.notification_delete_delay = (
            notification_delete_delay
        )

        from database.repositories import (
            LaunchRepository,
            QueueRepository,
            SessionRepository
        )

        self.launches = LaunchRepository(
            db
        )

        self.queues = QueueRepository(
            db
        )

        self.sessions = SessionRepository(
            db
        )

        self.monitor_task = None

        self.state_lock = asyncio.Lock()

        self.current_launch_id = None
        self.current_user_id = None

    @property
    def running(self):

        return (
            self.monitor_task is not None
            and not self.monitor_task.done()
        )

    async def start(
        self,
        user_id,
        chat_id
    ):

        async with self.state_lock:

            if self.running:

                return (
                    False,
                    "Запуск сервера уже выполняется"
                )

            status = (
                await self.aternos.fetch()
            )

            if status is None:

                return (
                    False,
                    "Сервер не найден"
                )

            if status == "online":

                await self.sessions.create(
                    self.aternos.server_name
                )

                return (
                    False,
                    "Сервер уже запущен"
                )

            if status in {
                "starting",
                "loading",
                "preparing",
                "connecting",
                "queue",
                "queued"
            }:

                return (
                    False,
                    "Сервер уже запускается"
                )

            launch_id = (
                await self.launches.create(
                    user_id,
                    self.aternos.server_name
                )
            )

            try:

                await self.launches.mark_started(
                    launch_id
                )

                await self.aternos.start()

            except Exception:

                await self.launches.finish(
                    launch_id,
                    "failed"
                )

                raise

            self.current_launch_id = (
                launch_id
            )

            self.current_user_id = (
                user_id
            )

            self.monitor_task = (
                asyncio.create_task(
                    self.monitor(
                        launch_id,
                        user_id,
                        chat_id
                    )
                )
            )

            return (
                True,
                "Запуск сервера начат."
            )

    async def monitor(
        self,
        launch_id,
        user_id,
        chat_id
    ):

        start_time = (
            asyncio.get_running_loop().time()
        )

        queue_created = False

        try:

            while True:

                elapsed = (
                    asyncio.get_running_loop().time()
                    - start_time
                )

                if (
                    elapsed
                    >= self.monitor_timeout
                ):

                    await self.launches.finish(
                        launch_id,
                        "timeout"
                    )

                    await self.notify(
                        chat_id,
                        "<b>Запуск не завершён</b>\n\n"
                        "Сервер не перешёл "
                        "в состояние «Онлайн» "
                        "за отведённое время."
                    )

                    return

                try:

                    status = (
                        await self.aternos.fetch()
                    )

                    print(
                        f"[LAUNCH] "
                        f"#{launch_id}: {status}"
                    )

                    # ==============================
                    # QUEUE
                    # ==============================

                    if status in {
                        "queue",
                        "queued"
                    }:

                        if not queue_created:

                            queue_created = True

                            await self.queues.create(
                                launch_id,
                                user_id,
                                chat_id,
                                self.queue_confirm_timeout
                            )

                            await self.send_queue_notification(
                                chat_id,
                                launch_id
                            )

                        confirmation = (
                            await self.queues.get(
                                launch_id
                            )
                        )

                        if (
                            confirmation
                            and confirmation["status"]
                            == "pending"
                            and self.queues.is_expired(
                                confirmation
                            )
                        ):

                            await self.queues.mark_expired(
                                launch_id
                            )

                            await self.launches.finish(
                                launch_id,
                                "queue_confirmation_timeout"
                            )

                            await self.edit_queue_message(
                                confirmation,
                                "<b>Подтверждение очереди истекло</b>\n\n"
                                "Запуск отменён, потому что очередь "
                                "не была подтверждена вовремя."
                            )

                            return

                    # ==============================
                    # ONLINE
                    # ==============================

                    elif status == "online":

                        confirmation = (
                            await self.queues.get(
                                launch_id
                            )
                        )

                        if confirmation:

                            await self.delete_queue_message(
                                confirmation
                            )

                        duration = int(
                            asyncio.get_running_loop().time()
                            - start_time
                        )

                        await self.launches.finish(
                            launch_id,
                            "success",
                            duration
                        )

                        await self.sessions.create(
                            self.aternos.server_name
                        )

                        await self.notify(
                            chat_id,
                            "<b>Сервер запущен</b>\n\n"
                            f"Сервер: "
                            f"<code>{self.aternos.server_name}</code>"
                        )

                        return

                    # ==============================
                    # CRASHED
                    # ==============================

                    elif status == "crashed":

                        await self.launches.finish(
                            launch_id,
                            "crashed"
                        )

                        await self.notify(
                            chat_id,
                            "<b>Сервер аварийно остановлен</b>"
                        )

                        return

                except asyncio.CancelledError:

                    raise

                except Exception as e:

                    print(
                        f"[LAUNCH] Ошибка проверки "
                        f"#{launch_id}: {e}"
                    )

                await asyncio.sleep(
                    self.monitor_interval
                )

        finally:

            async with self.state_lock:

                self.current_launch_id = None
                self.current_user_id = None
                self.monitor_task = None

    async def confirm_queue(
        self,
        launch_id,
        user_id
    ):

        confirmation = (
            await self.queues.get(
                launch_id
            )
        )

        if confirmation is None:

            return (
                False,
                "Подтверждение очереди не найдено"
            )

        if (
            int(confirmation["user_id"])
            != int(user_id)
        ):

            return (
                False,
                "Подтвердить очередь может только "
                "пользователь, запустивший сервер."
            )

        if (
            confirmation["status"]
            != "pending"
        ):

            return (
                False,
                "Это подтверждение уже обработано."
            )

        if self.queues.is_expired(
            confirmation
        ):

            await self.queues.mark_expired(
                launch_id
            )

            return (
                False,
                "Время подтверждения истекло"
            )

        if (
            not self.running
            or self.current_launch_id
            != launch_id
        ):

            return (
                False,
                "Этот запуск уже не активен"
            )

        status = (
            await self.aternos.fetch()
        )

        if status not in {
            "queue",
            "queued"
        }:

            if status in {
                "starting",
                "loading",
                "preparing",
                "connecting",
                "online"
            }:

                await self.queues.mark_confirmed(
                    launch_id
                )

                return (
                    True,
                    "Очередь уже подтверждена"
                )

            return (
                False,
                f"Aternos сейчас: {status}"
            )

        await self.aternos.confirm()

        await self.queues.mark_confirmed(
            launch_id
        )

        await self.edit_queue_message(
            confirmation,
            "<b>Очередь подтверждена</b>\n\n"
            "Aternos получил подтверждение. "
            "Ожидаю запуск сервера..."
        )

        return (
            True,
            "Очередь подтверждена"
        )

    async def send_queue_notification(
        self,
        chat_id,
        launch_id
    ):

        from ui.keyboards import queue_keyboard

        message = await self.bot.send_message(
            chat_id,
            "<b>Aternos требует подтверждение очереди</b>\n\n"
            "Нажмите кнопку ниже, чтобы подтвердить "
            "участие в очереди и продолжить запуск сервера.",
            reply_markup=queue_keyboard(
                launch_id
            )
        )

        await self.queues.set_message(
            launch_id,
            message.message_id
        )

    async def edit_queue_message(
        self,
        confirmation,
        text
    ):

        if not confirmation["message_id"]:
            return

        try:

            await self.bot.edit_message_text(
                chat_id=confirmation["chat_id"],
                message_id=confirmation["message_id"],
                text=text
            )

        except Exception as e:

            print(
                f"[QUEUE] "
                f"Не удалось изменить уведомление: {e}"
            )

    async def delete_queue_message(
        self,
        confirmation
    ):

        if not confirmation["message_id"]:
            return

        try:

            await self.bot.delete_message(
                confirmation["chat_id"],
                confirmation["message_id"]
            )

        except Exception:
            pass

    async def notify(
        self,
        chat_id,
        text
    ):

        try:

            message = (
                await self.bot.send_message(
                    chat_id,
                    text
                )
            )

            asyncio.create_task(
                self.delete_later(
                    chat_id,
                    message.message_id
                )
            )

        except Exception as e:

            print(
                f"[UI] "
                f"Не удалось отправить уведомление: {e}"
            )

    async def delete_later(
        self,
        chat_id,
        message_id
    ):

        await asyncio.sleep(
            self.notification_delete_delay
        )

        try:

            await self.bot.delete_message(
                chat_id,
                message_id
            )

        except Exception:
            pass

    def stop(self):

        if (
            self.monitor_task
            and not self.monitor_task.done()
        ):

            self.monitor_task.cancel()