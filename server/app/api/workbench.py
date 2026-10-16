"""HTTP routes owned by the workbench area."""
from fastapi import APIRouter

router = APIRouter()
NAV: list[tuple[str, str]] = []
