"""HTTP routes owned by the location area."""
from fastapi import APIRouter

router = APIRouter()
NAV: list[tuple[str, str]] = []
