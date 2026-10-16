"""HTTP routes owned by the timeline area."""
from fastapi import APIRouter

router = APIRouter()
NAV: list[tuple[str, str]] = []
