"""HTTP routes owned by the reports area."""
from fastapi import APIRouter

router = APIRouter()
NAV: list[tuple[str, str]] = []
