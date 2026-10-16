"""HTTP routes owned by the catalog area."""
from fastapi import APIRouter

router = APIRouter()
NAV: list[tuple[str, str]] = []
