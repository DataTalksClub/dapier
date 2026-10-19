"""Ideal Customer Profile (ICP) filter for DataTalksClub company signals."""

import re
from typing import Dict, Tuple

from .models import SignalItem


def _contains_phrase_or_word(text: str, keyword: str) -> bool:
    """Check if keyword/phrase appears as a whole token or phrase in text."""
    kw = keyword.strip().lower()
    if not kw:
        return False
    # If phrase contains spaces, search verbatim phrase bounded by non-alphanumeric
    escaped = re.escape(kw)
    pattern = rf"(?:\b|^|\s){escaped}(?:\b|$|\s)"
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def evaluate_icp(item: SignalItem, icp_config: Dict) -> Tuple[bool, str]:
    """Evaluate whether a signal item passes the ICP filter.

    Returns:
        (True, "ok") if item passes ICP.
        (False, reason) if item fails ICP.
    """
    if not icp_config:
        return True, "ok"

    # Aggregate search corpus
    parts = [
        item.company_name or "",
        item.signal_summary or "",
        item.raw_text or "",
        item.domain or "",
        item.evidence_url or "",
    ]
    corpus = " ".join(parts).lower()

    # 1. Deny list check (highest priority: filter noise/prohibited industries)
    deny_keywords = icp_config.get("deny_keywords") or []
    for kw in deny_keywords:
        if _contains_phrase_or_word(corpus, str(kw)):
            return False, f"Matched deny keyword: '{kw}'"

    # 2. Allow list check (relevance to data/ML/AI engineering education buyers)
    allow_keywords = icp_config.get("allow_keywords") or []
    if allow_keywords:
        matched_allow = False
        for kw in allow_keywords:
            if _contains_phrase_or_word(corpus, str(kw)):
                matched_allow = True
                break
        if not matched_allow:
            return False, "Did not match ICP allow keywords (data/ML/AI education)"

    # 3. Allowed stages check (if round is present)
    allowed_stages = icp_config.get("allowed_stages") or []
    if allowed_stages and item.round:
        normalized_round = item.round.strip().lower().replace(" ", "-")
        # match prefixes or normalized forms like 'series-a', 'seed'
        stage_matched = any(
            normalized_round == stage.lower() or stage.lower() in normalized_round
            for stage in allowed_stages
        )
        if not stage_matched:
            return False, f"Funding round '{item.round}' not in allowed stages"

    return True, "ok"
