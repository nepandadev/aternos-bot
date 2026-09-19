from aiogram import Router, F
from aiogram.types import CallbackQuery

from database.repositories import UserRepository


router = Router()


@router.callback_query(
    F.data == "server_start"
)
async def callback_start(
    callback: CallbackQuery,
    db,
    launch_service
):

    users = UserRepository(db)

    await users.register(
        callback.from_user
    )

    try:

        started, text = (
            await launch_service.start(
                user_id=callback.from_user.id,
                chat_id=callback.message.chat.id
            )
        )

        await callback.answer(
            text
        )

    except Exception as e:

        print(
            f"[START] Ошибка запуска: {e}"
        )

        await callback.answer(
            "Не удалось запустить сервер",
            show_alert=True
        )


@router.callback_query(
    F.data.startswith("queue_confirm:")
)
async def callback_queue_confirm(
    callback: CallbackQuery,
    db,
    launch_service
):

    users = UserRepository(db)

    await users.register(
        callback.from_user
    )

    try:

        launch_id = int(
            callback.data.split(
                ":",
                1
            )[1]
        )

    except (
        ValueError,
        IndexError
    ):

        await callback.answer(
            "Некорректный запрос",
            show_alert=True
        )

        return

    try:

        success, text = (
            await launch_service.confirm_queue(
                launch_id,
                callback.from_user.id
            )
        )

        await callback.answer(
            text,
            show_alert=not success
        )

    except Exception as e:

        print(
            f"[QUEUE] Ошибка подтверждения: {e}"
        )

        await callback.answer(
            "Не удалось подтвердить очередь",
            show_alert=True
        )