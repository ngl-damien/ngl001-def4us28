#!/usr/bin/env python3
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ledger_store import (
    LedgerIntegrityError,
    append_event,
    load_state,
    read_events,
    reconcile_state,
    write_state_atomic,
)

root = Path(__file__).resolve().parents[1]
state_path = root / "contact" / "state.json"
ledger_path = root / "contact" / "ledger.jsonl"

try:
    state = load_state(state_path)
    if state["enabled"] is not True:
        raise SystemExit("CONTACT disabled by founder")

    events = read_events(ledger_path)
    state, recovered = reconcile_state(state, events)
    if recovered:
        # A prior process may have durably appended an event and died before
        # replacing state.json. Reconcile before making another observation.
        write_state_atomic(state_path, state)
except LedgerIntegrityError as error:
    raise SystemExit(
        f"CONTACT state/ledger integrity check failed; no observation appended: {error}"
    ) from error

try:
    request = urllib.request.Request(
        "https://api.github.com/repos/No-Gas-Labs-Official/ngl001-def4us28",
        headers={"User-Agent": "NGL-CONTACT-001"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
        observation = {
            "ok": True,
            "status": response.status,
            "full_name": data.get("full_name"),
            "default_branch": data.get("default_branch"),
            "pushed_at": data.get("pushed_at"),
        }
except Exception as error:
    observation = {
        "ok": False,
        "error_type": type(error).__name__,
        "error": str(error)[:300],
    }

event = {
    "schema": "ngl.contact.event.v1",
    "type": "EXTERNAL_OBSERVATION",
    "actor": "contact-runtime",
    "authority": "observation",
    "claim": "GitHub repository API observation",
    "evidence": observation,
    "previous_hash": state.get("last_event_hash"),
    "timestamp": datetime.now(timezone.utc).isoformat(),
}

try:
    event = append_event(
        ledger_path,
        event,
        expected_previous=state.get("last_event_hash"),
    )
    events.append(event)
    state["run_count"] = len(events)
    state["last_event_hash"] = event["event_hash"]
    state["last_observation"] = event
    state["phase"] = "OBSERVED" if observation["ok"] else "OBSERVATION_FAILED"
    state["next_transition"] = (
        "AWAIT_FOUNDER_OR_NEXT_STEPWIRE"
        if observation["ok"]
        else "RETRY_OBSERVATION"
    )
    write_state_atomic(state_path, state)
except LedgerIntegrityError as error:
    raise SystemExit(
        f"CONTACT state/ledger commit failed; inspect the ledger before retrying: {error}"
    ) from error

print(json.dumps({
    "event_hash": event["event_hash"],
    "observation": observation,
    "next_transition": state["next_transition"],
}, sort_keys=True))
