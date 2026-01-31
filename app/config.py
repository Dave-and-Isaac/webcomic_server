import json
from pathlib import Path

CONFIG_DIR = Path("config").resolve()
POSTERS_DIR = CONFIG_DIR / "posters"
LOGOS_DIR = CONFIG_DIR / "logos"
AVATARS_DIR = CONFIG_DIR / "avatars"
SERIES_JSON = CONFIG_DIR / "series.json"


def ensure_config() -> None:
    if not CONFIG_DIR.exists():
        raise RuntimeError("Missing required config directory: ./config")
    POSTERS_DIR.mkdir(parents=True, exist_ok=True)
    LOGOS_DIR.mkdir(parents=True, exist_ok=True)
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    if not SERIES_JSON.exists():
        SERIES_JSON.write_text("{}", encoding="utf-8")


def load_series_config() -> dict:
    raw = SERIES_JSON.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("series.json must be valid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("series.json must be a JSON object mapping series slugs to metadata")
    return data


def save_series_config(data: dict) -> None:
    SERIES_JSON.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
