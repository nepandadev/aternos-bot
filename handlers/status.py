from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from database.repositories import UserRepository
from ui.keyboards import main_keyboard
from ui.texts import server_status_text


router = Router()


@router.callback_query(F.data == "server_status")
async def callback_status(
    callback: CallbackQuery,
    db,
    aternos,
):
    users = UserRepository(db)

    await users.register(
        callback.from_user
    )

    await callback.answer()

    text = await server_status_text(
        aternos
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=main_keyboard(),
        )

    except TelegramBadRequest as e:

        if "message is not modified" in str(e).lower():
            return

        print(
            f"[UI] Не удалось изменить сообщение: {e}"
        )

        try:
            await callback.message.delete()
        except Exception:
            pass

        await callback.message.answer(
            text,
            reply_markup=main_keyboard(),
        )