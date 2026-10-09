from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from .asr_recipe import DEFAULT_RECIPE_PATH, load_recipe

_asr_recipe_path = Path(os.getenv("EW_ASR_RECIPE", str(DEFAULT_RECIPE_PATH)))
_asr_defaults = load_recipe(_asr_recipe_path)

@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("EW_DATA_DIR", "./data"))
    db_path: Path = Path(os.getenv("EW_DB_PATH", "./data/evidence.db"))
    store_dir: Path = Path(os.getenv("EW_STORE_DIR", "./data/store"))
    case_name: str = os.getenv("EW_CASE_NAME", "Default case")
    whisper_model: str = os.getenv("EW_WHISPER_MODEL", _asr_defaults.model)
    whisper_device: str = os.getenv("EW_WHISPER_DEVICE", _asr_defaults.device)
    whisper_compute_type: str = os.getenv("EW_WHISPER_COMPUTE_TYPE", _asr_defaults.compute_type)
    scan_roots: tuple[Path, ...] = tuple(Path(p).absolute() for p in os.getenv("EW_SCAN_ROOTS", "").split(os.pathsep) if p)
    asr_recipe_path: Path = _asr_recipe_path

settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.store_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
