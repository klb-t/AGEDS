from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("EW_DATA_DIR", "./data"))
    db_path: Path = Path(os.getenv("EW_DB_PATH", "./data/evidence.db"))
    store_dir: Path = Path(os.getenv("EW_STORE_DIR", "./data/store"))
    case_name: str = os.getenv("EW_CASE_NAME", "Default case")
    whisper_model: str = os.getenv("EW_WHISPER_MODEL", "small")
    whisper_device: str = os.getenv("EW_WHISPER_DEVICE", "cpu")
    whisper_compute_type: str = os.getenv("EW_WHISPER_COMPUTE_TYPE", "int8")

settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.store_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
