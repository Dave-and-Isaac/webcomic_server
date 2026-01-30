import json
from pathlib import Path

CONFIG_DIR = Path("config").resolve()
POSTERS_DIR = CONFIG_DIR / "posters"
LOGOS_DIR = CONFIG_DIR / "logos"
SERIES_JSON = CONFIG_DIR / "series.json"


def ensure_config() -> None:
    if not CONFIG_DIR.exists():
        raise RuntimeError("Missing required config directory: ./config")
    if not POSTERS_DIR.exists():
        raise RuntimeError("Missing required posters directory: ./config/posters")
    if not LOGOS_DIR.exists():
        raise RuntimeError("Missing required logos directory: ./config/logos")
    if not SERIES_JSON.exists():
        raise RuntimeError("Missing required config file: ./config/series.json")


def load_series_config() -> dict:
    with SERIES_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError("series.json must be a JSON object mapping series slugs to metadata")
    return data


def save_series_config(data: dict) -> None:
    SERIES_JSON.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
