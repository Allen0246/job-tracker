import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

load_dotenv(PROJECT_ROOT / ".env")


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
