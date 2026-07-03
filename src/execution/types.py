from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FillResult:
    success: bool
    order_id: Optional[int]
    price: Optional[float]
    retcode: Optional[int]
    comment: str
