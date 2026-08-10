import os
import re
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


def enabled(name: str, default: bool = True) -> bool:
    """Reads a per-source on/off toggle from the environment. Unset =
    default (enabled), so adding a new toggle never silently disables
    an existing source for someone who hasn't set it.
    """
    value = env(name, default=str(default))
    return value.strip().lower() not in ("false", "0", "no", "off", "")


def company_enabled(name: str, default: bool = True) -> bool:
    """Per-company on/off toggle, same ENABLE_<NAME> convention and same
    place (.env) as the per-source toggles above — just one level more
    granular. The env var name is derived straight from the company's own
    config name (uppercased, any run of non-alphanumeric characters
    collapsed to one underscore: "ONE Hungary" -> ENABLE_ONE_HUNGARY),
    so there's nothing to keep in sync beyond adding the .env line.
    """
    env_var = "ENABLE_" + re.sub(r"[^A-Za-z0-9]+", "_", name.strip()).upper().strip("_")
    return enabled(env_var, default)
