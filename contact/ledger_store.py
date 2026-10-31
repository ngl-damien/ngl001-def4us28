"""Crash-consistent persistence for CONTACT's append-only observation ledger.

The ledger is authoritative. state.json is a recoverable projection of that ledger.
A process death after an event append but before the state replacement is repaired
on the next run; malformed, forked, or truncated history fails closed.
"""
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

STATE_SCHEMA = "ngl.contact.state.v1"
EVENT_SCHEMA = "ngl.contact.event.v1"
EVENT_FIELDS = frozenset({
    "actor", "authority", "claim", "event_hash", "evidence",
    "previous_hash", "schema", "timestamp", "type",
})
UNSEALED_EVENT_FIELDS = EVENT_FIELDS - {"event_hash"}


class LedgerIntegrityError(RuntimeError):
    """Raised when CONTACT state or history cannot be trusted."""


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LedgerIntegrityError("duplicate JSON object key")
        result[key] = value
    return result


def _canonical_bytes(value):
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise LedgerIntegrityError("CONTACT data is not canonical JSON") from error


def _event_digest(event):
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    return hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def _validate_event(event, expected_previous, line_number):
    label = f"CONTACT ledger line {line_number}"
    if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
        raise LedgerIntegrityError(f"{label} has an unexpected schema")
    if (
        event["schema"] != EVENT_SCHEMA
        or event["type"] != "EXTERNAL_OBSERVATION"
        or event["actor"] != "contact-runtime"
        or event["authority"] != "observation"
        or not isinstance(event["claim"], str)
        or not isinstance(event["timestamp"], str)
        or not isinstance(event["evidence"], dict)
        or type(event["evidence"].get("ok")) is not bool
    ):
        raise LedgerIntegrityError(f"{label} has invalid event fields")
    if event["previous_hash"] != expected_previous:
        raise LedgerIntegrityError(f"{label} forks or breaks the previous-hash chain")
    supplied = event["event_hash"]
    if not isinstance(supplied, str) or len(supplied) != 64:
        raise LedgerIntegrityError(f"{label} has an invalid event hash")
    if any(character not in "0123456789abcdef" for character in supplied):
        raise LedgerIntegrityError(f"{label} has an invalid event hash")
    if _event_digest(event) != supplied:
        raise LedgerIntegrityError(f"{label} hash does not match its contents")


def load_state(path):
    path = Path(path)
    if path.is_symlink():
        raise LedgerIntegrityError("CONTACT state must not be a symlink")
    try:
        state = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LedgerIntegrityError("CONTACT state cannot be read as valid JSON") from error
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        raise LedgerIntegrityError("CONTACT state schema is invalid")
    if type(state.get("enabled")) is not bool:
        raise LedgerIntegrityError("CONTACT enabled flag must be a boolean")
    return state


def read_events(path):
    path = Path(path)
    if path.is_symlink():
        raise LedgerIntegrityError("CONTACT ledger must not be a symlink")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise LedgerIntegrityError("CONTACT ledger cannot be read") from error

    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise LedgerIntegrityError("CONTACT ledger ends with an incomplete line")

    events = []
    previous = None
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise LedgerIntegrityError(f"CONTACT ledger line {line_number} is empty")
        try:
            event = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (UnicodeError, json.JSONDecodeError) as error:
            raise LedgerIntegrityError(
                f"CONTACT ledger line {line_number} is not valid JSON"
            ) from error
        _validate_event(event, previous, line_number)
        events.append(event)
        previous = event["event_hash"]
    return events


def reconcile_state(state, events):
    """Recover state.json when a valid ledger append outlived its state update."""
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        raise LedgerIntegrityError("CONTACT state schema is invalid")
    if type(state.get("enabled")) is not bool:
        raise LedgerIntegrityError("CONTACT enabled flag must be a boolean")

    run_count = state.get("run_count")
    if type(run_count) is not int or run_count < 0:
        raise LedgerIntegrityError("CONTACT run_count is invalid")

    last_hash = state.get("last_event_hash")
    if last_hash is None:
        prefix_length = 0
    else:
        matches = [
            index for index, event in enumerate(events)
            if event["event_hash"] == last_hash
        ]
        if len(matches) != 1:
            raise LedgerIntegrityError("CONTACT state points outside the retained ledger")
        prefix_length = matches[0] + 1

    if run_count != prefix_length:
        raise LedgerIntegrityError("CONTACT state count and ledger position disagree")
    if prefix_length:
        if state.get("last_observation") != events[prefix_length - 1]:
            raise LedgerIntegrityError("CONTACT state projection does not match its ledger event")
    elif state.get("last_observation") is not None:
        raise LedgerIntegrityError("CONTACT initial state unexpectedly contains an observation")

    if len(events) < prefix_length:
        raise LedgerIntegrityError("CONTACT ledger is shorter than its state projection")
    if len(events) == prefix_length:
        return dict(state), False

    recovered = dict(state)
    latest = events[-1]
    recovered["run_count"] = len(events)
    recovered["last_event_hash"] = latest["event_hash"]
    recovered["last_observation"] = latest
    if latest["evidence"]["ok"] is True:
        recovered["phase"] = "OBSERVED"
        recovered["next_transition"] = "AWAIT_FOUNDER_OR_NEXT_STEPWIRE"
    else:
        recovered["phase"] = "OBSERVATION_FAILED"
        recovered["next_transition"] = "RETRY_OBSERVATION"
    return recovered, True


def seal_event(event, expected_previous):
    if not isinstance(event, dict) or set(event) != UNSEALED_EVENT_FIELDS:
        raise LedgerIntegrityError("CONTACT new event has an unexpected schema")
    if event["previous_hash"] != expected_previous:
        raise LedgerIntegrityError("CONTACT new event does not extend the current ledger")
    sealed = dict(event)
    sealed["event_hash"] = _event_digest(sealed)
    _validate_event(sealed, expected_previous, "new")
    return sealed


def append_event(path, event, expected_previous):
    """Append one fully hashed event and fsync it before state can advance."""
    path = Path(path)
    if path.is_symlink():
        raise LedgerIntegrityError("CONTACT ledger must not be a symlink")
    existing = read_events(path)
    actual_previous = existing[-1]["event_hash"] if existing else None
    if actual_previous != expected_previous:
        raise LedgerIntegrityError("CONTACT ledger changed before append")

    sealed = seal_event(event, expected_previous)
    payload = (json.dumps(sealed, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o644)
    except OSError as error:
        raise LedgerIntegrityError("CONTACT ledger could not be opened for append") from error
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short CONTACT ledger write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return sealed


def write_state_atomic(path, state):
    """Replace state.json atomically and sync both file contents and directory entry."""
    path = Path(path)
    if path.is_symlink():
        raise LedgerIntegrityError("CONTACT state must not be a symlink")
    try:
        mode = stat.S_IMODE(os.lstat(path).st_mode)
    except FileNotFoundError:
        mode = 0o644
    except OSError as error:
        raise LedgerIntegrityError("CONTACT state metadata cannot be read") from error

    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as error:
        raise LedgerIntegrityError("CONTACT state could not be committed atomically") from error
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
