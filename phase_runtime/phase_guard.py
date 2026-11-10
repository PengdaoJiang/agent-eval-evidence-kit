"""Project-local formal-phase continuity. No native API, process, token or policy mutation.

Declared owners are consistency fields, not OS authentication. Keep the registry
under its controller's sole writer and each progress root under its worker's sole
writer. A completed native turn is never sufficient research acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import uuid

VERSION = "1.0.0"
SCHEMA = "rea.phase_guard."
OPERATIONS = ("register", "publish-progress", "recover", "validate-delivery", "hook-context")
SUCCESS_STATUSES = ("REGISTERED", "PROGRESS_PUBLISHED", "CURRENT_PHASE", "CURRENT_FORMAL_IDENTITY_VALID")
PENDING_STATUSES = ("CURRENT_FORMAL_ASSIGNMENT_REQUIRED", "CURRENT_FORMAL_ASSIGNMENT_NEEDS_CHECKPOINT")
FAILED_NATIVE_STATUSES = ("failed", "interrupted", "cancelled", "canceled")
PLATFORM_BLOCKED_STATES = ("WAITING_NATIVE_PLATFORM_ACCESS", "WAITING_USER_PLATFORM_ACCESS")


class Refused(ValueError):
    pass


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise Refused("Duplicate JSON key: " + key)
        result[key] = value
    return result


def parse(raw):
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
                           parse_constant=lambda value: _invalid_constant(value))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Refused("Malformed UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise Refused("JSON object required")
    return value


def _invalid_constant(value):
    raise Refused("Nonfinite JSON constant is not permitted: " + value)


def text(value, name):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise Refused(name + " must be nonempty text")
    return value


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise Refused(name + " must be an integer >= " + str(minimum))
    return value


def plain(value, *, exists=True, directory=False):
    p = Path(text(str(value), "path"))
    if not p.is_absolute() or ".." in p.parts:
        raise Refused("Path must be absolute without parent traversal")
    for part in [p, *p.parents]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Refused("Link/reparse paths are not permitted")
    if exists:
        if not p.exists() or (directory and not p.is_dir()) or (not directory and not p.is_file()):
            raise Refused("Expected " + ("directory: " if directory else "regular file: ") + str(p))
        if not directory and p.stat().st_nlink != 1:
            raise Refused("Aliased file is not permitted")
    return p.resolve(strict=exists)


def identity(value):
    if not isinstance(value, dict) or set(value) != {"path", "bytes", "sha256"}:
        raise Refused("Identity requires exactly path, bytes, sha256")
    p = plain(value["path"])
    size = integer(value["bytes"], "identity bytes")
    sha = value["sha256"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise Refused("Invalid identity sha256")
    raw = p.read_bytes()
    if len(raw) != size or digest(raw) != sha:
        raise Refused("Identity changed: " + str(p))
    return {"path": str(p), "bytes": size, "sha256": sha}, raw


def file_identity(path):
    p = plain(path)
    raw = p.read_bytes()
    return {"path": str(p), "bytes": len(raw), "sha256": digest(raw)}


def _mkdir(path):
    path = plain(path, exists=False)
    path.mkdir(parents=True, exist_ok=True)
    return plain(path, directory=True)


@contextmanager
def locked(root):
    root = _mkdir(root)
    lock = root / ".phase-writer.lock"
    plain(lock, exists=False)
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise Refused("Writer lock exists; original owner must reconcile it") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded({"pid": os.getpid(), "purpose": "bounded_phase_publication"}))
            handle.flush()
            os.fsync(handle.fileno())
        yield root
    finally:
        lock.unlink()


def publish(path, value):
    """Single publication under the caller's root lock; never overwrite receipts."""
    path = plain(path, exists=False)
    _mkdir(path.parent)
    raw = encoded(value)
    temp = path.parent / (".pending-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # os.rename refuses replacement on Windows; POSIX needs an explicit
        # existence check under the sole-writer lock before atomic publication.
        if path.exists():
            raise Refused("Immutable receipt already exists")
        os.rename(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return {"path": str(path), "bytes": len(raw), "sha256": digest(raw)}


def _read(path):
    return parse(plain(path).read_bytes())


def _key(thread_id):
    return digest(text(thread_id, "thread_id").encode("utf-8"))


def _stream(root, folder, name, expected):
    p = plain(root, directory=True) / folder
    if not p.exists():
        return []
    plain(p, directory=True)
    rows, previous = [], None
    for index, member in enumerate(sorted(p.glob("*.json")), 1):
        if member.name != f"{index:08d}.json":
            raise Refused("Noncontiguous or malformed " + name + " stream")
        ident = file_identity(member)
        row = _read(member)
        if row.get("schema") != SCHEMA + name + ".v1" or type(row.get("number")) is not int or row.get("number") != index:
            raise Refused("Invalid " + name + " record")
        if row.get("previous_sha256") != previous:
            raise Refused("Broken " + name + " hash chain")
        for key, value in expected.items():
            if type(row.get(key)) is not type(value) or row.get(key) != value:
                raise Refused("Mismatched " + name + " " + key)
        rows.append((row, ident))
        previous = ident["sha256"]
    return rows


def current(registry_root, thread_id):
    root = plain(registry_root, exists=False)
    if not root.exists():
        return None
    plain(root, directory=True)
    owner = _read(root / "OWNER.json")
    if owner.get("schema") != SCHEMA + "registry_owner.v1":
        raise Refused("Invalid registry owner record")
    rows = _stream(root, "threads/" + _key(thread_id), "registration", {
        "thread_id": thread_id, "controller_owner": owner.get("controller_owner")})
    return rows[-1] if rows else None


def _embedded_checkpoint(ident, reg):
    ident, raw = identity(ident)
    cp = parse(raw)
    if "schema" in cp:
        text(cp["schema"], "checkpoint schema")
    if text(cp.get("job"), "checkpoint job") != reg["job_id"] + "/" + reg["attempt"]:
        raise Refused("Checkpoint job mismatch")
    owners = [cp[k] for k in ("owner", "owner_thread_id", "native_thread_id", "thread_id") if k in cp]
    if not owners or any(text(owner, "checkpoint owner") != reg["thread_id"] for owner in owners):
        raise Refused("Checkpoint owner mismatch or missing")
    if "formal_work_order" in cp:
        formal = cp["formal_work_order"]
        if isinstance(formal, dict):
            # Normalize path spelling while verifying every supplied descriptor.
            checked, _ = identity(formal)
            if checked != reg["formal_work_order"]:
                raise Refused("Checkpoint formal order mismatch")
        elif isinstance(formal, str):
            if plain(formal) != Path(reg["formal_work_order"]["path"]):
                raise Refused("Checkpoint formal order path mismatch")
        else:
            raise Refused("Checkpoint formal_work_order must be a verified descriptor or absolute path")
    if "formal_work_order_sha256" in cp:
        supplied = text(cp["formal_work_order_sha256"], "checkpoint formal order sha256")
        if supplied != reg["formal_work_order"]["sha256"]:
            raise Refused("Checkpoint formal order sha256 mismatch")
    return ident


def _same_binding(request, reg, rid):
    integer(request.get("epoch"), "epoch", 1)
    if request.get("epoch") != reg["number"] or request.get("binding_id") != rid["sha256"]:
        raise Refused("Stale or mismatched formal binding")


def _continuity(value, thread_id):
    if not isinstance(value, dict):
        raise Refused("continuity must be an object")
    _json_tree(value, "continuity")
    if "owner_thread_id" in value and value["owner_thread_id"] != thread_id:
        raise Refused("Continuity owner mismatch")
    # Evidence only. This module never acts on or releases these values.
    return value


def _json_tree(value, location):
    if isinstance(value, dict):
        for key, item in value.items():
            text(key, location + " key")
            _json_tree(item, location + "." + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _json_tree(item, location + "[" + str(index) + "]")
    elif type(value) not in (str, bool, int, float, type(None)):
        raise Refused("Invalid JSON evidence value at " + location)
    elif type(value) is float and not math.isfinite(value):
        raise Refused("Nonfinite evidence value at " + location)


def _same_json(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_json(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right))
    return left == right


def _append_only(previous, proposed, location="continuity"):
    """Retain every resource reference; closure is additional evidence, not deletion."""
    if type(previous) is not type(proposed):
        raise Refused("Continuity evidence type cannot change at " + location)
    if isinstance(previous, dict):
        if not previous.keys() <= proposed.keys():
            raise Refused("Continuity evidence keys cannot be removed at " + location)
        for key, value in previous.items():
            _append_only(value, proposed[key], location + "." + key)
    elif isinstance(previous, list):
        if len(proposed) < len(previous) or not all(_same_json(value, proposed[index]) for index, value in enumerate(previous)):
            raise Refused("Continuity evidence lists must retain their exact previous prefix at " + location)
    elif not _same_json(previous, proposed):
        raise Refused("Continuity evidence value cannot change at " + location)
    return proposed


def _continuity_update(previous, proposed, thread_id):
    return _append_only(_continuity(previous, thread_id), _continuity(proposed, thread_id))


def latest_progress(reg, rid):
    root = Path(reg["progress_root"])
    if not root.exists():
        return None
    plain(root, directory=True)
    owner_path = root / "OWNER.json"
    if not owner_path.exists():
        if any(root.iterdir()):
            raise Refused("Nonempty progress root lacks owner record")
        return None
    owner = _read(owner_path)
    if owner != {"schema": SCHEMA + "progress_owner.v1", "thread_id": reg["thread_id"],
                 "job_id": reg["job_id"], "attempt": reg["attempt"]}:
        raise Refused("Progress root belongs to another assignment")
    rows = _stream(root, f"epochs/{reg['number']:08d}", "progress", {
        "thread_id": reg["thread_id"], "epoch": reg["number"],
        "job_id": reg["job_id"], "attempt": reg["attempt"],
        "binding_id": rid["sha256"],
        "formal_work_order_sha256": reg["formal_work_order"]["sha256"]})
    return rows[-1] if rows else None


def _anchor(reg, rid):
    latest = latest_progress(reg, rid)
    if latest:
        row, ident = latest
        return {"sequence": row["number"], "progress_receipt": ident,
                "checkpoint": _embedded_checkpoint(row["checkpoint"], reg),
                "stage": row["stage"], "next_action": row["next_action"],
                "continuity": row["continuity"]}
    initial = reg["initial_anchor"]
    if initial.get("checkpoint"):
        _embedded_checkpoint(initial["checkpoint"], reg)
    return initial


def native_observation(value, controller_owner, thread_id, turn_id, *, completed=False):
    ident, raw = identity(value)
    observed = parse(raw)
    if observed.get("schema") != SCHEMA + "native_observation.v1":
        raise Refused("Native observation must use the documented selected-observation schema")
    if observed.get("controller_owner") != controller_owner or observed.get("source") != "supervisor_native_tool":
        raise Refused("Native observation must be independently retained by the controller from its native tool result")
    if observed.get("thread_id") != thread_id or observed.get("turn_id") != turn_id:
        raise Refused("Native observation thread/turn mismatch")
    status = text(observed.get("status"), "native observation status")
    if completed and status != "completed":
        raise Refused("Native observation is not completed")
    text(observed.get("native_tool"), "native_tool")
    # Source ownership/provenance is the controller's responsibility; labels
    # and local paths are not OS authentication or an independent native query.
    return ident, observed


def register(req):
    root = plain(req["registry_root"], exists=False)
    owner = text(req["controller_owner"], "controller_owner")
    tid = text(req["thread_id"], "thread_id")
    job, attempt = text(req["job_id"], "job_id"), text(req["attempt"], "attempt")
    if any(x in job or x in attempt for x in ("/", "\\")):
        raise Refused("job_id and attempt must be separate identifiers")
    expected = integer(req["expected_epoch"], "expected_epoch")
    turn = text(req["formal_turn_id"], "formal_turn_id")
    formal, _ = identity(req["formal_work_order"])
    assignment, raw = identity(req["assignment"])
    actual = parse(raw)
    if actual.get("job") != job + "/" + attempt:
        raise Refused("Assignment job mismatch")
    tids = [actual[k] for k in ("native_thread_id", "thread_id") if k in actual]
    if not tids or any(v != tid for v in tids):
        raise Refused("Assignment thread mismatch or missing")
    turns = [actual[k] for k in ("native_turn_id", "formal_turn_id") if k in actual]
    if not turns or any(not isinstance(v, str) or not v for v in turns) or len(set(turns)) != 1:
        raise Refused("Assignment must explicitly bind its original formal turn")
    reason = req.get("registration_reason", "initial")
    if reason not in ("initial", "continuation", "migration"):
        raise Refused("Invalid registration_reason")
    if turns[0] != turn and reason == "initial":
        raise Refused("A different current turn requires explicit continuation or migration evidence")
    observation, observed = native_observation(req["native_observation"], owner, tid, turn)
    assigned_formal, _ = identity(actual.get("formal_work_order"))
    if assigned_formal != formal:
        raise Refused("Assignment formal order mismatch")
    progress = plain(req["progress_root"], exists=False)
    if progress == root or progress.is_relative_to(root) or root.is_relative_to(progress):
        raise Refused("Registry and private progress roots must be disjoint")
    if Path(observation["path"]).is_relative_to(progress):
        raise Refused("Controller native observation must be outside the worker progress root")
    if progress.exists():
        plain(progress, directory=True)
    with locked(root):
        owner_path = root / "OWNER.json"
        if owner_path.exists():
            if _read(owner_path) != {"schema": SCHEMA + "registry_owner.v1", "controller_owner": owner}:
                raise Refused("Registry controller owner mismatch")
        else:
            if any(p.name != ".phase-writer.lock" for p in root.iterdir()):
                raise Refused("Nonempty registry root lacks owner record")
            publish(owner_path, {"schema": SCHEMA + "registry_owner.v1", "controller_owner": owner})
        prior = current(root, tid)
        epoch = prior[0]["number"] if prior else 0
        if epoch != expected:
            raise Refused("Stale expected_epoch")
        initial = {"sequence": 0, "progress_receipt": None, "checkpoint": None,
                   "stage": "CURRENT_FORMAL_ASSIGNMENT_NEEDS_CHECKPOINT",
                   "next_action": "Read the registered current formal work order and publish its current checkpoint.",
                   "continuity": _continuity(req.get("continuity", {}), tid)}
        same = prior and all(prior[0][k] == v for k, v in (("job_id", job), ("attempt", attempt), ("formal_work_order", formal)))
        if prior and prior[0]["formal_turn_id"] == turn and prior[0]["native_status"] in FAILED_NATIVE_STATUSES and observed["status"] != prior[0]["native_status"]:
            raise Refused("Contradictory status for the same failed native turn; retain it and bind a distinct observed continuation turn")
        if reason == "continuation" and not same:
            raise Refused("Continuation requires a prior matching formal assignment")
        if reason == "migration" and prior:
            raise Refused("Migration is only for a previously unregistered thread")
        if same:
            if prior[0]["progress_root"] != str(progress):
                raise Refused("Same-assignment continuation must retain its private progress root")
            initial = dict(_anchor(prior[0], prior[1]))
            initial["carried_from_epoch"] = epoch
            initial["sequence"] = 0
            initial["progress_receipt"] = None
            if "continuity" in req:
                initial["continuity"] = _continuity_update(initial["continuity"], req["continuity"], tid)
        reg = {"schema": SCHEMA + "registration.v1", "number": epoch + 1,
               "previous_sha256": prior[1]["sha256"] if prior else None,
               "controller_owner": owner, "thread_id": tid, "job_id": job, "attempt": attempt,
               "formal_turn_id": turn, "formal_work_order": formal, "assignment": assignment,
               "original_formal_turn_id": turns[0], "native_observation": observation,
               "native_status": observed["status"], "registration_reason": reason,
               "run_state": text(req.get("run_state", prior[0].get("run_state", "FORMAL_ASSIGNED") if same else "FORMAL_ASSIGNED"), "run_state"),
               "progress_root": str(progress), "initial_anchor": initial}
        if req.get("initial_checkpoint") is not None:
            checkpoint = _embedded_checkpoint(req["initial_checkpoint"], reg)
            if same and initial["checkpoint"] and checkpoint != initial["checkpoint"]:
                raise Refused("Continuation cannot replace the latest checkpoint with another initial checkpoint")
            initial["checkpoint"] = checkpoint
            if not same:
                initial["stage"] = text(req.get("stage", "CURRENT_CHECKPOINT_REGISTERED"), "stage")
                initial["next_action"] = text(req.get("next_action", "Resume the current checkpoint within the registered formal order."), "next_action")
        # Recheck selected files immediately before publication.
        identity(formal)
        identity(assignment)
        identity(observation)
        latest_progress(reg, {"sha256": digest(encoded(reg))})
        receipt = publish(root / "threads" / _key(tid) / f"{epoch + 1:08d}.json", reg)
    return {"status": "REGISTERED", "epoch": reg["number"], "binding_id": receipt["sha256"], "registration": receipt}


def publish_progress(req):
    selected = current(req["registry_root"], req["thread_id"])
    if not selected:
        raise Refused("CURRENT_FORMAL_ASSIGNMENT_REQUIRED")
    reg, rid = selected
    _same_binding(req, reg, rid)
    if req.get("caller_owner") != reg["thread_id"]:
        raise Refused("Progress caller owner mismatch")
    expected = integer(req["expected_sequence"], "expected_sequence")
    cp = _embedded_checkpoint(req["checkpoint"], reg)
    stage, action = text(req["stage"], "stage"), text(req["next_action"], "next_action")
    with locked(Path(reg["progress_root"])) as root:
        owner_path = root / "OWNER.json"
        if not owner_path.exists():
            if any(p.name != ".phase-writer.lock" for p in root.iterdir()):
                raise Refused("Nonempty progress root lacks owner record")
            publish(owner_path, {"schema": SCHEMA + "progress_owner.v1", "thread_id": reg["thread_id"],
                                 "job_id": reg["job_id"], "attempt": reg["attempt"]})
        prior = latest_progress(reg, rid)
        seq = prior[0]["number"] if prior else 0
        if seq != expected:
            raise Refused("Stale expected_sequence")
        previous_anchor = _anchor(reg, rid)
        continuity = _continuity_update(previous_anchor["continuity"],
                                        req.get("continuity", previous_anchor["continuity"]), reg["thread_id"])
        record = {"schema": SCHEMA + "progress.v1", "number": seq + 1,
                  "previous_sha256": prior[1]["sha256"] if prior else None,
                  "thread_id": reg["thread_id"], "job_id": reg["job_id"], "attempt": reg["attempt"],
                  "epoch": reg["number"], "binding_id": rid["sha256"],
                  "formal_work_order_sha256": reg["formal_work_order"]["sha256"],
                  "checkpoint": cp, "stage": stage, "next_action": action, "continuity": continuity}
        if current(req["registry_root"], req["thread_id"])[1] != rid:
            raise Refused("Formal binding changed during progress publication")
        identity(cp)
        receipt = publish(root / "epochs" / f"{reg['number']:08d}" / f"{seq + 1:08d}.json", record)
    if current(req["registry_root"], req["thread_id"])[1] != rid:
        raise Refused("Formal binding changed; retained old progress is not current")
    return {"status": "PROGRESS_PUBLISHED", "epoch": reg["number"], "sequence": seq + 1,
            "binding_id": rid["sha256"], "progress_receipt": receipt}


def recover(req):
    selected = current(req["registry_root"], req["thread_id"])
    if not selected:
        return {"status": "CURRENT_FORMAL_ASSIGNMENT_REQUIRED", "thread_id": req["thread_id"],
                "next_action": "The current controller must register this thread's current formal assignment.",
                "metadata_pending": True, "new_research_qualification_gate": False,
                "startup_fallback": False, "owner_transfer": False, "tokens_released": False}
    reg, rid = selected
    for key in ("job_id", "attempt"):
        if key in req and req[key] != reg[key]:
            raise Refused("Requested " + key + " is not current")
    identity(reg["formal_work_order"])
    identity(reg["assignment"])
    identity(reg["native_observation"])
    anchor = _anchor(reg, rid)
    if current(req["registry_root"], req["thread_id"])[1] != rid:
        raise Refused("Formal binding changed during recovery")
    return {"status": "CURRENT_PHASE" if anchor["checkpoint"] else "CURRENT_FORMAL_ASSIGNMENT_NEEDS_CHECKPOINT",
            "thread_id": reg["thread_id"], "job_id": reg["job_id"], "attempt": reg["attempt"],
            "epoch": reg["number"], "binding_id": rid["sha256"], "formal_turn_id": reg["formal_turn_id"],
            "run_state": reg["run_state"], "native_status": reg["native_status"],
            "metadata_pending": not bool(anchor["checkpoint"]), "new_research_qualification_gate": False,
            "formal_work_order": reg["formal_work_order"], "assignment": reg["assignment"],
            "progress_root": reg["progress_root"], "registration": rid, "anchor": anchor,
            "startup_fallback": False, "owner_transfer": False, "tokens_released": False,
            "boundary": "Current project facts only; preserve original owners, process records, open tokens and completed work. No restart, release, safety override or research acceptance is authorized."}


def validate_delivery(req):
    view = recover(req)
    if view["status"] != "CURRENT_PHASE":
        raise Refused(view["status"])
    if view["run_state"] in PLATFORM_BLOCKED_STATES:
        raise Refused("Current phase remains " + view["run_state"])
    if view["native_status"] in FAILED_NATIVE_STATUSES:
        raise Refused("Registered current native turn is terminal unsuccessful; a distinct observed continuation turn needs a new controller epoch")
    delivery_identity, raw = identity(req["delivery"])
    delivery = parse(raw)
    if delivery.get("schema") != SCHEMA + "formal_delivery.v1" or delivery.get("kind") != "formal":
        raise Refused("NON_DELIVERY_STALE_STARTUP_OR_UNTYPED_OUTPUT")
    expected = {key: view[key] for key in ("thread_id", "job_id", "attempt", "epoch", "binding_id", "formal_turn_id")}
    expected.update(formal_work_order_sha256=view["formal_work_order"]["sha256"],
                    progress_sequence=view["anchor"]["sequence"], checkpoint_sha256=view["anchor"]["checkpoint"]["sha256"])
    for key, value in expected.items():
        if type(delivery.get(key)) is not type(value) or delivery.get(key) != value:
            raise Refused("Stale or mismatched delivery " + key)
    terminal = delivery.get("native_terminal")
    if terminal != {"thread_id": view["thread_id"], "turn_id": view["formal_turn_id"], "status": "completed"}:
        raise Refused("Native terminal must match the explicitly registered formal turn")
    reg, _ = current(req["registry_root"], req["thread_id"])
    native_id, _ = native_observation(req["native_observation"], reg["controller_owner"],
                                      view["thread_id"], view["formal_turn_id"], completed=True)
    if Path(native_id["path"]).is_relative_to(Path(reg["progress_root"])):
        raise Refused("Controller native observation must be outside the worker progress root")
    if recover(req) != view:
        raise Refused("Current phase changed during delivery validation")
    identity(delivery_identity)
    identity(native_id)
    return {"status": "CURRENT_FORMAL_IDENTITY_VALID", "delivery": delivery_identity,
            "binding_id": view["binding_id"], "epoch": view["epoch"],
            "native_observation": native_id, "research_accepted": False,
            "native_terminal_matches_controller_observation": True,
            "requires_existing_scientific_and_resource_acceptance": True,
            "boundary": "Identity guard only. The controller owns native observation provenance; this module does not authenticate OS owners or query the platform. Original scientific, process-release and storage acceptance remain authoritative."}


def hook_context(req):
    """Optional advisory only: shared parent session IDs cannot identify a subagent."""
    event = req.get("event", {})
    if not isinstance(event, dict):
        raise Refused("Hook event must be an object")
    if event.get("hook_event_name") != "SessionStart" or event.get("source") not in ("compact", "resume"):
        return {}
    if not isinstance(event.get("session_id"), str) or not isinstance(event.get("cwd"), str):
        return {}
    if plain(event["cwd"], directory=True) != plain(req["project_root"], directory=True):
        return {}
    # Never interpret event.session_id as a trusted worker identity. The hint is
    # deliberately general and contains no job, private checkpoint or token.
    entry = str(plain(Path(__file__)))
    message = ("Project phase continuity: compaction/resume does not authorize replaying an old startup receipt. "
               "Use your own actual thread/job identity with the fixed read-only recovery entry " + entry +
               " recover --request <your scoped recovery request>. Do not infer your identity from a shared parent session_id. "
               "Follow only the registered current formal order and checkpoint; if absent, report CURRENT_FORMAL_ASSIGNMENT_REQUIRED. "
               "Preserve original owners, processes, open tokens and completed work; do not restart or bypass a platform block.")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message}}


def run(operation, request=None):
    if isinstance(operation, dict) and request is None:
        request = dict(operation)
        operation = request.pop("operation", None)
    if operation not in OPERATIONS or not isinstance(request, dict):
        raise Refused("Unknown operation or invalid request")
    try:
        return globals()[operation.replace("-", "_")](request)
    except (KeyError, TypeError) as exc:
        raise Refused("Missing or malformed request field: " + str(exc)) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--request")
    parser.add_argument("--registry-root")
    parser.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    args = parser.parse_args(argv)
    try:
        if args.request:
            request = _read(args.request)
        elif args.operation == "recover" and args.registry_root and args.thread_id:
            request = {"registry_root": args.registry_root, "thread_id": args.thread_id}
        else:
            raise Refused("Provide --request, or recover with --registry-root and CODEX_THREAD_ID/--thread-id")
        if args.operation == "hook-context" and "event" not in request:
            request["event"] = parse(sys.stdin.buffer.read())
        result = run(args.operation, request)
        code = 0 if args.operation == "hook-context" or result.get("status") in SUCCESS_STATUSES else 1
    except (Refused, OSError) as exc:
        result, code = {"status": "REFUSED", "error": str(exc), "startup_fallback": False,
                        "owner_transfer": False, "tokens_released": False}, 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
