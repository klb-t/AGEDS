"""HTTP routes owned by the analysis area."""
from fastapi import APIRouter

router = APIRouter()
NAV: list[tuple[str, str]] = []
