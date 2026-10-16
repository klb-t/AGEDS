"""Catalog scan limits. Importing this module touches no database or settings
(the CLI parser registers limit options without creating a data directory)."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .. import scanner

GiB = 1024 ** 3
MiB = 1024 ** 2


@dataclass(frozen=True)
class CatalogLimits:
    """Explicit resource limits. Each hit is recorded and makes coverage partial."""
    max_files: int = 200_000
    max_directory_entries: int = 100_000
    max_total_entries: int = 1_000_000
    max_directory_depth: int = 64
    max_hash_bytes: int = 8 * GiB
    max_total_hash_bytes: int = 1024 * GiB
    max_archive_depth: int = 8
    max_members_per_archive: int = 100_000
    max_total_members: int = 1_000_000
    max_expanded_bytes: int = 256 * GiB
    max_compression_ratio: int = 200
    ratio_check_min_bytes: int = 1 * MiB
    max_member_hash_bytes: int = 4 * GiB
    max_nested_archive_bytes: int = 8 * GiB
    max_zip_directory_bytes: int = 64 * MiB
    spool_memory_bytes: int = 32 * MiB
    sniff_bytes: int = 64 * 1024
    expand_document_containers: bool = False

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == 'expand_document_containers':
                if not isinstance(value, bool):
                    raise ValueError('expand_document_containers must be a boolean')
            elif not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f'catalog limit {name} must be a positive integer')
        if self.sniff_bytes < 1024:
            raise ValueError('sniff_bytes must be at least 1024')

    def scan_limits(self) -> scanner.ScanLimits:
        return scanner.ScanLimits(max_files=self.max_files, max_directory_entries=self.max_directory_entries,
                                  max_total_entries=self.max_total_entries, max_depth=self.max_directory_depth,
                                  max_hash_bytes=self.max_hash_bytes, max_total_hash_bytes=self.max_total_hash_bytes)


LIMIT_NAMES = tuple(f for f in CatalogLimits.__dataclass_fields__ if f != 'expand_document_containers')
