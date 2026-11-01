"""API-key authentication. The gateway never stores plaintext keys, only SHA-256 hashes."""

from __future__ import annotations

import hashlib
import hmac

from .config import TeamSpec


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Authenticator:
    def __init__(self, teams: list[TeamSpec]) -> None:
        self._entries = [(h.lower(), team) for team in teams for h in team.key_sha256]

    def authenticate(self, key: str) -> TeamSpec | None:
        presented = hash_key(key)
        match = None
        # Compare against every entry in constant time so timing doesn't leak which hash matched.
        for stored, team in self._entries:
            if hmac.compare_digest(presented, stored):
                match = team
        return match
