from aiogram import Dispatcher

from .start import router as start_router
from .status import router as status_router
from .launch import router as launch_router


def setup_routers(
    dp: Dispatcher
):

    dp.include_router(
        start_router
    )

    dp.include_router(
        status_router
    )

    dp.include_router(
        launch_router
    )