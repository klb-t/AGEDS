"""HTTP routes owned by the ledger area."""
from fastapi import APIRouter

router = APIRouter()
NAV: list[tuple[str, str]] = []
