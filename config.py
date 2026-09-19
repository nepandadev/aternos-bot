import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    aternos_session: str
    tg_token: str

    db_file: str = "bot.db"

    monitor_interval: int = 5
    monitor_timeout: int = 15 * 60

    notification_delete_delay: int = 60

    queue_confirm_timeout: int = 10 * 60


def required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Не задан {name} в .env"
        )

    return value

settings = Settings(
    aternos_session=required_env("ATERNOS_SESSION"),
    tg_token=required_env("TG_TOKEN"),

    db_file=os.getenv(
        "DB_FILE",
        "bot.db"
    ),

    monitor_interval=int(
        os.getenv(
            "MONITOR_INTERVAL",
            "5"
        )
    ),

    monitor_timeout=int(
        os.getenv(
            "MONITOR_TIMEOUT",
            "900"
        )
    ),

    notification_delete_delay=int(
        os.getenv(
            "NOTIFICATION_DELETE_DELAY",
            "60"
        )
    ),

    queue_confirm_timeout=int(
        os.getenv(
            "QUEUE_CONFIRM_TIMEOUT",
            "600"
        )
    ),
)