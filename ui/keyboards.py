from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Запуск"),
                KeyboardButton(text="Статус"),
            ]
        ],
        resize_keyboard=True,
    )