"""Firmware library: Event Machine programs, their peer-device models and tests."""

from pathlib import Path
from typing import List

from asm import assemble

FW_DIR = Path(__file__).resolve().parent


def load(name: str) -> List[int]:
    """Assemble fw/<name>.s and return its words."""
    path = FW_DIR / f"{name}.s"
    return assemble(path.read_text(), str(path))
