"""Запуск: python -m bot"""
import os


def _load_dotenv(path: str = ".env") -> None:
    """Минимальная загрузка .env для запуска без Docker (до импорта настроек)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

from .main import main  # noqa: E402

main()
