import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ledger_store import (
    LedgerIntegrityError,
    append_event,
    load_state,
    read_events,
    reconcile_state,
    seal_event,
    write_state_atomic,
)


def observation(previous_hash, ok=True, marker="first"):
    return {
        "schema": "ngl.contact.event.v1",
        "type": "EXTERNAL_OBSERVATION",
        "actor": "contact-runtime",
        "authority": "observation",
        "claim": "GitHub repository API observation",
        "evidence": {"ok": ok, "marker": marker},
        "previous_hash": previous_hash,
        "timestamp": "2026-10-30T00:00:00+00:00",
    }


def initial_state():
    return {
        "schema": "ngl.contact.state.v1",
        "enabled": True,
        "founder_gate": {
            "external_mutation": "DENY_WITHOUT_EXPLICIT_AUTHORIZATION",
            "mode": "sovereign",
        },
        "run_count": 0,
        "last_event_hash": None,
        "last_observation": None,
        "phase": "READY",
        "next_transition": "OBSERVE",
    }


def state_at(events):
    state = initial_state()
    if events:
        latest = events[-1]
        state["run_count"] = len(events)
        state["last_event_hash"] = latest["event_hash"]
        state["last_observation"] = latest
        state["phase"] = "OBSERVED" if latest["evidence"]["ok"] else "OBSERVATION_FAILED"
        state["next_transition"] = (
            "AWAIT_FOUNDER_OR_NEXT_STEPWIRE"
            if latest["evidence"]["ok"]
            else "RETRY_OBSERVATION"
        )
    return state


class ContactLedgerStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.ledger_path = self.root / "ledger.jsonl"
        self.state_path = self.root / "state.json"

    def tearDown(self):
        self.directory.cleanup()

    def append(self, previous_hash, ok=True, marker="first"):
        return append_event(
            self.ledger_path,
            observation(previous_hash, ok=ok, marker=marker),
            expected_previous=previous_hash,
        )

    def test_append_and_read_verify_the_complete_hash_chain(self):
        first = self.append(None)
        second = self.append(first["event_hash"], marker="second")
        self.assertEqual(read_events(self.ledger_path), [first, second])

    def test_recovers_valid_append_after_process_dies_before_state_replace(self):
        first = self.append(None)
        state = state_at([first])
        write_state_atomic(self.state_path, state)

        # Simulate the durable ledger append succeeding while state.json remains
        # at the preceding event, exactly the crash window in runtime.py.
        second = self.append(first["event_hash"], ok=False, marker="second")
        recovered, changed = reconcile_state(load_state(self.state_path), read_events(self.ledger_path))

        self.assertTrue(changed)
        self.assertEqual(recovered["run_count"], 2)
        self.assertEqual(recovered["last_event_hash"], second["event_hash"])
        self.assertEqual(recovered["last_observation"], second)
        self.assertEqual(recovered["phase"], "OBSERVATION_FAILED")
        self.assertEqual(recovered["next_transition"], "RETRY_OBSERVATION")

    def test_initial_state_can_recover_a_valid_ledger_after_first_append(self):
        self.append(None)
        recovered, changed = reconcile_state(initial_state(), read_events(self.ledger_path))
        self.assertTrue(changed)
        self.assertEqual(recovered["run_count"], 1)

    def test_unchanged_state_is_not_rewritten(self):
        first = self.append(None)
        state = state_at([first])
        recovered, changed = reconcile_state(state, read_events(self.ledger_path))
        self.assertFalse(changed)
        self.assertEqual(recovered, state)

    def test_rejects_modified_event_contents(self):
        event = self.append(None)
        event["evidence"]["marker"] = "tampered"
        self.ledger_path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(LedgerIntegrityError, "hash does not match"):
            read_events(self.ledger_path)

    def test_rejects_a_validly_hashed_but_forked_event(self):
        self.append(None)
        fork = seal_event(observation("f" * 64, marker="fork"), expected_previous="f" * 64)
        with self.ledger_path.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(fork, sort_keys=True) + "\n")
        with self.assertRaisesRegex(LedgerIntegrityError, "forks or breaks"):
            read_events(self.ledger_path)

    def test_rejects_truncated_final_line_instead_of_appending_after_it(self):
        self.append(None)
        with self.ledger_path.open("ab") as ledger:
            ledger.write(b'{"partial":')
        with self.assertRaisesRegex(LedgerIntegrityError, "incomplete line"):
            read_events(self.ledger_path)

    def test_rejects_duplicate_json_keys_in_ledger(self):
        self.ledger_path.write_text('{"actor":"a","actor":"b"}\n', encoding="utf-8")
        with self.assertRaisesRegex(LedgerIntegrityError, "duplicate JSON"):
            read_events(self.ledger_path)

    def test_load_state_rejects_duplicate_json_keys(self):
        self.state_path.write_text(
            '{"schema":"ngl.contact.state.v1","enabled":true,"enabled":false}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(LedgerIntegrityError, "duplicate JSON"):
            load_state(self.state_path)

    def test_load_state_rejects_wrong_schema(self):
        self.state_path.write_text(
            json.dumps({"schema": "ngl.contact.state.v0", "enabled": True}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(LedgerIntegrityError, "schema is invalid"):
            load_state(self.state_path)

    def test_rejects_state_pointer_outside_the_retained_ledger(self):
        first = self.append(None)
        state = state_at([first])
        state["last_event_hash"] = "0" * 64
        with self.assertRaisesRegex(LedgerIntegrityError, "points outside"):
            reconcile_state(state, [first])

    def test_rejects_non_boolean_enabled_state(self):
        state = initial_state()
        state["enabled"] = "false"
        with self.assertRaisesRegex(LedgerIntegrityError, "must be a boolean"):
            reconcile_state(state, [])

    def test_new_ledger_file_syncs_parent_directory(self):
        with patch("ledger_store.os.fsync", wraps=os.fsync) as sync:
            self.append(None)
        expected = 2 if os.name == "posix" else 1
        self.assertEqual(sync.call_count, expected)

    def test_append_rejects_a_stale_writer(self):
        self.append(None)
        with self.assertRaisesRegex(LedgerIntegrityError, "changed before append"):
            self.append(None, marker="stale")

    def test_atomic_state_write_round_trips_and_preserves_mode(self):
        self.state_path.write_text(json.dumps(initial_state()), encoding="utf-8")
        os.chmod(self.state_path, 0o640)
        write_state_atomic(self.state_path, initial_state())
        self.assertEqual(load_state(self.state_path), initial_state())
        self.assertEqual(os.stat(self.state_path).st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
