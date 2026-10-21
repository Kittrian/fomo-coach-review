"""v1.16: recognise caller recap / brag posts ("$Babem did 3.8X from our call", "MC $11K → $42K") so they are
not treated as new calls. Conservative: only clear recap phrasing matches; real "SMUG CALL" / "LAUNCH" posts pass."""
from __future__ import annotations

import re
from typing import Optional

_DID_X = re.compile(r"\bdid\s+\d+(?:[.,]\d+)?\s*x\b", re.I)
_FROM_CALL = re.compile(r"\bfrom\s+(?:our|the|my|this)\s+call\b", re.I)
_MC_ARROW = re.compile(r"\bMC\s*\$?\s*[\d.,]+\s*[KkMm]?\s*(?:→|->|➡️?|to)\s*\$?\s*[\d.,]+\s*[KkMm]?", re.I)
_SOL_ARROW = re.compile(r"\d+(?:\.\d+)?\s*SOL\s*(?:→|->|➡️?)\s*\d+(?:\.\d+)?\s*SOL", re.I)
_X_GAIN = re.compile(r"\b\d+(?:[.,]\d+)?\s*x\s+(?:from|since)\s+(?:our|the|my)?\s*(?:call|entry)\b", re.I)


def recap_reason(text: Optional[str]) -> Optional[str]:
    t = text or ""
    if _DID_X.search(t):
        return "recap_post: 'did N×'"
    if _FROM_CALL.search(t) or _X_GAIN.search(t):
        return "recap_post: 'from our call'"
    if _MC_ARROW.search(t) and _SOL_ARROW.search(t):
        return "recap_post: MC → / SOL → gain recap"
    return None


def is_recap_post(text: Optional[str]) -> bool:
    return recap_reason(text) is not None
