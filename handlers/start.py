from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardRemove

from database.repositories import UserRepository
from ui.keyboards import main_keyboard


router = Router()


@router.message(CommandStart())
async def start_command(
    message: Message,
    db,
    aternos,
):
    users = UserRepository(db)
    await users.register(message.from_user)

    if aternos.server is None:
        await message.answer("<b>Сервер не найден</b>")
        return

    await message.answer(
        "<b>Управление сервером</b>",
        reply_markup=ReplyKeyboardRemove(),
    )

    await message.answer(
        "<b>Выберите действие:</b>",
        reply_markup=main_keyboard(),
    )