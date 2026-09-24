import asyncio


class SessionMonitor:

    def __init__(
        self,
        db,
        aternos,
        interval,
    ):
        self.aternos = aternos
        self.interval = interval

        from database.repositories import (
            SessionRepository
        )

        self.sessions = SessionRepository(
            db
        )

    async def run(self):

        print(
            "[SESSION MONITOR] Запущен."
        )

        while True:

            try:

                # НЕ вызываем aternos.fetch()
                # Берём уже полученный статус.
                status = self.aternos.status

                server_name = (
                    self.aternos.server_name
                )

                if (
                    status is None
                    or server_name is None
                ):
                    await asyncio.sleep(
                        self.interval
                    )
                    continue

                active = (
                    await self.sessions.get_active(
                        server_name
                    )
                )

                if status == "online":

                    if active is None:

                        session_id = (
                            await self.sessions.create(
                                server_name
                            )
                        )

                        print(
                            "[SESSION MONITOR] "
                            "Обнаружен работающий сервер. "
                            f"Создана сессия #{session_id}"
                        )

                elif status == "offline":

                    if active is not None:

                        await self.sessions.finish(
                            active["id"]
                        )

            except asyncio.CancelledError:

                raise

            except Exception as e:

                print(
                    "[SESSION MONITOR] "
                    f"Ошибка проверки: {e}"
                )

            await asyncio.sleep(
                self.interval
            )