"""Generate a team API key and the hash to put in the gateway config.

    python -m aigw.keys platform
"""

from __future__ import annotations

import secrets
import sys

from .auth import hash_key


def main() -> None:
    team = sys.argv[1] if len(sys.argv) > 1 else "team"
    key = f"aigw_{team}_{secrets.token_urlsafe(32)}"
    print(f"API key (give to the team, shown once): {key}")
    print(f"key_sha256 (put in config):            {hash_key(key)}")


if __name__ == "__main__":
    main()
