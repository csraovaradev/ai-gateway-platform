"""Validate a policy file, and say whether it would start in production mode (ADR-0008).

    python -m aigw.check ../policies/gateway.example.yaml
    python -m aigw.check --production ../policies/gateway.production.example.yaml   # CI gate
"""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aigw.check",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("policy")
    parser.add_argument("--production", action="store_true",
                        help="fail unless the policy is safe for mode: production")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.policy)
    except (OSError, ValidationError) as exc:
        print(f"{args.policy}: invalid\n{exc}", file=sys.stderr)
        return 1
    issues = config.production_issues()
    sign_in = f", sign-in via {config.oidc.issuer}" if config.oidc else ""
    print(f"{args.policy}: valid, mode {config.mode}, {len(config.models)} models, "
          f"{len(config.teams)} teams{sign_in}")
    for issue in issues:
        print(f"  not production-ready: {issue}")
    return 1 if args.production and issues else 0


if __name__ == "__main__":
    sys.exit(main())
