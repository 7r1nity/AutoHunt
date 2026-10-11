"""Text normalization shared by every routing layer (pure, deterministic).

Normalization is deliberately cheap and dependency-free: NFKC folds full-width
forms (（ＡＢＣ１２３）→ (ABC123)) and lowercasing; punctuation/emoji are
replaced with spaces so rule patterns can match compact text.
"""

from __future__ import annotations

import re
import unicodedata

# Anything that is not a word char (a-z0-9_ after lowercasing) or CJK becomes a
# separator. Emoji, punctuation, full-width symbols all land here after NFKC.
_SEPARATOR_RE = re.compile(r"[^\w\u4e00-\u9fff]+", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Return the canonical form used by rules, clarify memory and hashing."""
    folded = unicodedata.normalize("NFKC", str(text)).lower()
    folded = _SEPARATOR_RE.sub(" ", folded)
    return _WHITESPACE_RE.sub(" ", folded).strip()


def security_text(text: str) -> str:
    """Structural form for safety rules: NFKC + lowercase + whitespace collapse
    only. Punctuation and path/credential markers (<, /, \\, ., -, _) survive so
    P0 patterns can detect injection tags, keys and path escapes."""
    folded = unicodedata.normalize("NFKC", str(text)).lower()
    return _WHITESPACE_RE.sub(" ", folded).strip()


def query_hash(text: str) -> str:
    """Stable, non-reversible hash of the normalized query (privacy)."""
    import hashlib

    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:24]
