#!/usr/bin/env python3
"""Fail-closed gate for publishing CONTACT observations to the repository."""

import json
import os
from pathlib import Path
from typing import Any


def publication_decision(
    state: Any,
    event_name: str,
    explicit_input: Any,
) -> tuple[bool, str]:
    """Allow a repository write only on an explicitly confirmed manual run.

    This enforces the existing founder_gate declaration; it is not a
    cryptographic identity verifier or a substitute for GitHub account security.
    """
    if not isinstance(state, dict):
        return False, "STATE_INVALID"
    if state.get("enabled") is not True:
        return False, "CONTACT_DISABLED"

    founder_gate = state.get("founder_gate")
    if (
        not isinstance(founder_gate, dict)
        or founder_gate.get("external_mutation")
        != "ALLOW_WITH_EXPLICIT_AUTHORIZATION"
    ):
        return False, "FOUNDER_GATE_DENY"

    if event_name != "workflow_dispatch":
        return False, "MANUAL_DISPATCH_REQUIRED"
    if str(explicit_input).strip().lower() != "true":
        return False, "EXPLICIT_CONFIRMATION_MISSING"

    return True, "EXPLICIT_MANUAL_AUTHORIZATION"


def main() -> int:
    state_path = Path(__file__).resolve().with_name("state.json")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = None

    allowed, reason = publication_decision(
        state,
        os.environ.get("GITHUB_EVENT_NAME", ""),
        os.environ.get("CONTACT_AUTHORIZE_PUBLICATION", "false"),
    )

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"allow={'true' if allowed else 'false'}\n")

    print(json.dumps({"allow": allowed, "reason": reason}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
