STATUS_NAMES = {

    "offline":
        "Оффлайн",

    "online":
        "Онлайн",

    "starting":
        "Запуск",

    "stopping":
        "Остановка",

    "loading":
        "Загрузка",

    "restarting":
        "Перезапуск",

    "saving":
        "Сохранение",

    "preparing":
        "Подготовка",

    "queue":
        "В очереди",

    "queued":
        "В очереди",

    "connecting":
        "Подключение",

    "crashed":
        "Аварийно остановлен",
}


def status_text(status):

    if status is None:
        return "Неизвестно"

    return STATUS_NAMES.get(
        status.lower(),
        status.capitalize()
    )


async def server_status_text(
    aternos
):

    if aternos.server is None:
        return "<b>Сервер не найден</b>"

    try:

        status = await aternos.fetch()

        return (
            "<b>Состояние сервера</b>\n\n"
            f"Сервер: "
            f"<code>{aternos.server_name}</code>\n"
            f"Статус: "
            f"<b>{status_text(status)}</b>"
        )

    except Exception as e:

        print(
            f"[STATUS] Ошибка получения статуса: {e}"
        )

        return (
            "<b>Состояние сервера</b>\n\n"
            "Не удалось получить состояние сервера."
        )