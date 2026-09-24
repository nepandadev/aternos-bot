from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Запуск",
                    callback_data="server_start",
                ),
                InlineKeyboardButton(
                    text="📊 Статус",
                    callback_data="server_status",
                ),
            ]
        ]
    )