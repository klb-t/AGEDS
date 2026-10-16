"""Output location policy and exclusive publication of a report directory.

Reports go only into a NEW subdirectory of a user-chosen private output
directory. Refused: the repository (code checkout), the content store and
every configured scan root (sources are read-only), including any path that
resolves into them through symlinks. Nothing existing is overwritten: the
final directory is reserved with an exclusive ``mkdir`` and the completed
staging directory is renamed onto that still-empty reservation.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .common import ReportOutputRefused

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
STAGING_PREFIX = '.ageds-report-staging-'


def forbidden_roots() -> list[tuple[str, Path]]:
    from .. import db
    settings = db.settings
    roots = [('repository', REPOSITORY_ROOT), ('content_store', Path(settings.store_dir))]
    roots.extend(('scan_root', Path(root)) for root in settings.scan_roots)
    return [(name, Path(os.path.realpath(path))) for name, path in roots]


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def check_output_dir(output_dir: str | Path) -> Path:
    """Resolve ``output_dir`` and refuse forbidden locations (ReportOutputRefused)."""
    if output_dir is None or str(output_dir).strip() == '':
        raise ReportOutputRefused('an output directory is required')
    path = Path(os.path.realpath(Path(output_dir).expanduser()))
    for name, root in forbidden_roots():
        if _inside(path, root):
            raise ReportOutputRefused(f'output directory is inside the {name} ({root}); choose a private directory elsewhere')
    if path.exists() and not path.is_dir():
        raise ReportOutputRefused(f'output path exists and is not a directory: {path}')
    return path


class Publication:
    """Staging directory → atomic, no-clobber rename onto a reserved final name."""

    def __init__(self, output_dir: Path, final_name: str):
        self.output_dir = check_output_dir(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Re-check after creation: a symlinked component may have appeared.
        self.output_dir = check_output_dir(self.output_dir)
        self.final = self.output_dir / final_name
        os.mkdir(self.final, 0o700)  # exclusive reservation; FileExistsError if taken
        self.staging = self.output_dir / (STAGING_PREFIX + final_name)
        try:
            os.mkdir(self.staging, 0o700)
        except BaseException:
            os.rmdir(self.final)
            raise
        self.published = False

    def publish(self) -> Path:
        os.rename(self.staging, self.final)  # replaces only our own empty reservation
        self.published = True
        return self.final

    def abort(self) -> None:
        if not self.published:
            shutil.rmtree(self.staging, ignore_errors=True)
            try:
                os.rmdir(self.final)
            except OSError:
                pass
