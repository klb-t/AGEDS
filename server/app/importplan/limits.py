"""Planner and run limits. Importing this module touches no database or settings
(the CLI parser registers limit options without creating a data directory)."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PlanLimits:
    """Planner bounds (presets; every hit is reported as incomplete coverage)."""
    max_entries: int = 1_000_000
    max_probe_bytes: int = 4 * 1024 ** 3
    max_unit_members: int = 200_000
    max_conversation_id_scan_bytes: int = 64 * 1024 ** 2
    max_conversation_ids_listed: int = 500
    max_spreadsheet_probe_bytes: int = 32 * 1024 ** 2
    max_whatsapp_txt_probes_per_zip: int = 8

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f'plan limit {name} must be a positive integer')


LIMIT_NAMES = tuple(PlanLimits.__dataclass_fields__)


@dataclass(frozen=True)
class RunLimits:
    """Execution bounds (presets). A hit refuses the item before anything is read."""
    max_item_bytes: int = 64 * 1024 ** 3
    max_item_members: int = 200_000
    min_free_temp_bytes: int = 256 * 1024 ** 2
    lease_seconds: int = 6 * 3600

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f'run limit {name} must be a positive integer')


RUN_LIMIT_NAMES = tuple(RunLimits.__dataclass_fields__)
