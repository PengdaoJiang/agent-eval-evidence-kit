"""Derive execution counts and recoverable input identities from one exact freeze.

No directory discovery, model calls, execution of candidates, or scientific exit
interpretation. The result is architecture bookkeeping, not worker delivery.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys

from selected_execution import LAB, WORKSPACE, JOB_RE, SESSION_RE, Refused, checked_path, identity, inside, key, mkdir, open_regular, require


def descriptor(value):
    require(isinstance(value, dict) and isinstance(value.get("path"), str)
            and Path(value["path"]).is_absolute() and ".." not in Path(value["path"]).parts
            and type(value.get("bytes")) is int and value["bytes"] >= 0
            and isinstance(value.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]), "INVALID_FILE_IDENTITY")
    return {name: value[name] for name in ("path", "bytes", "sha256")}


def version_key(value):
    return f"{value['bytes']}:{value['sha256']}"


def read_json(path, expected=None):
    with open_regular(path) as stream:
        raw = stream.read()
    observed = {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if expected is not None:
        require(observed["bytes"] == expected["bytes"] and observed["sha256"] == expected["sha256"], "FROZEN_FILE_IDENTITY_DRIFT")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise Refused("INVALID_JSON") from None
    require(isinstance(value, dict), "OBJECT_JSON_REQUIRED")
    return value, observed


def build_ledger(job, freeze_path, *, lab=LAB):
    require(isinstance(job, str) and len(job) <= 100 and JOB_RE.fullmatch(job), "INVALID_JOB_NAME")
    lab = checked_path(Path(lab).absolute())
    root = checked_path(lab / "runs" / job)
    expected_freeze = lab / "receipts" / "freeze" / f"{job}.json"
    require(Path(freeze_path).is_absolute() and key(freeze_path) == key(expected_freeze), "EXACT_JOB_FREEZE_REQUIRED")
    frozen, freeze_identity = read_json(checked_path(Path(freeze_path)))
    session = frozen.get("session_id")
    require(frozen.get("schema") == "rea.harness.constructor-freeze.v1" and frozen.get("job_id") == job
            and frozen.get("workspace_id") == WORKSPACE and isinstance(session, str)
            and SESSION_RE.fullmatch(session), "FREEZE_BINDING_MISMATCH")
    require(frozen.get("owner_turn_terminal") is True and frozen.get("all_recorded_tools_ended") is True, "TERMINAL_FREEZE_REQUIRED")
    catalog_ref = descriptor(frozen.get("catalog"))
    catalog_path = root / "evidence" / "INPUT_CATALOG.json"
    require(key(catalog_ref["path"]) == key(catalog_path), "EXACT_JOB_CATALOG_REQUIRED")
    catalog, catalog_identity = read_json(catalog_path, catalog_ref)
    require(catalog.get("job_id") == job and isinstance(catalog.get("references"), list), "CATALOG_BINDING_MISMATCH")
    require(isinstance(frozen.get("candidate_files"), list), "EXPLICIT_FREEZE_CANDIDATES_REQUIRED")

    # Only the explicit freeze and catalog descriptors form the recovery universe.
    # No parent directories are listed, and no other root is searched.
    selected = []
    candidates = []
    for row in frozen["candidate_files"]:
        ref = descriptor(row)
        require(inside(ref["path"], root), "FREEZE_CANDIDATE_OUTSIDE_JOB")
        candidates.append(ref)
        selected.append((ref, "freeze.candidate_files"))
    selected.extend((descriptor(row), "catalog.references") for row in catalog["references"])
    by_version = defaultdict(list)
    by_path = defaultdict(list)
    for ref, origin in selected:
        by_version[version_key(ref)].append((ref, origin))
        by_path[key(ref["path"])].append((ref, origin))
    observations = {}

    def observed_file(path):
        normalized = key(path)
        if normalized not in observations:
            try:
                actual = identity(Path(path))
                observations[normalized] = {"observed": actual}
            except (Refused, OSError) as exc:
                observations[normalized] = {"error": str(exc) if isinstance(exc, Refused) else "SELECTED_FILE_UNREADABLE"}
        return observations[normalized]

    def recovery(ref):
        matching = {}
        for selected_ref, origin in by_version.get(version_key(ref), []):
            row = matching.setdefault(key(selected_ref["path"]), {"identity": selected_ref, "selected_by": []})
            if origin not in row["selected_by"]:
                row["selected_by"].append(origin)
        available, unavailable = [], []
        for normalized in sorted(matching):
            row = matching[normalized]
            observation = observed_file(row["identity"]["path"])
            actual = observation.get("observed")
            if actual is not None and version_key(actual) == version_key(ref):
                available.append(row)
            else:
                unavailable.append({**row, **observation})
        return {"status": "AVAILABLE_IN_SELECTED_EVIDENCE" if available else "UNAVAILABLE_IN_SELECTED_EVIDENCE",
                "recoverable_files": available, "matching_selected_files_not_verified": unavailable,
                "boundary": "Search is limited to this freeze's explicit candidates plus this job's exact catalog. Absence is not a whole-machine nonexistence claim."}

    execution_root = root / "work" / "execution_records"
    receipt_refs = {}
    for ref in candidates:
        path = Path(ref["path"])
        if not inside(path, execution_root):
            continue
        relative = Path(key(path)).relative_to(Path(key(execution_root)))
        if len(relative.parts) != 2 or relative.name != "receipt.json":
            continue
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", relative.parts[0]), "INVALID_EXECUTION_RECORD_NAME")
        require(key(path) not in receipt_refs, "DUPLICATE_FROZEN_RECEIPT")
        receipt_refs[key(path)] = ref

    versions = {}
    items = []
    operations = Counter()
    compile_exits, run_exits = Counter(), Counter()
    compile_unrecorded = run_unrecorded = 0

    def bind_input(ref, item, role, alias):
        ref = descriptor(ref)
        version = version_key(ref)
        row = versions.setdefault(version, {"version_key": version, "bytes": ref["bytes"], "sha256": ref["sha256"],
                                           "recorded_paths": [], "uses": [], "recovery": recovery(ref)})
        if ref["path"] not in row["recorded_paths"]:
            row["recorded_paths"].append(ref["path"])
        row["uses"].append({"run_id": item["run_id"], "role": role, "alias": alias})
        selected_original = [{"identity": candidate, "selected_by": origin} for candidate, origin in by_path.get(key(ref["path"]), [])]
        item["input_bindings"].append({"role": role, "alias": alias, "recorded_identity": ref, "version_key": version,
                                       "selected_descriptors_at_recorded_path": selected_original})

    for normalized in sorted(receipt_refs):
        ref = receipt_refs[normalized]
        path = Path(ref["path"])
        row, actual = read_json(path, ref)  # Verify bytes/SHA before parsing any receipt.
        require(row.get("schema") == "dsh.lab.selected_execution.receipt.v1" and row.get("job_id") == job
                and row.get("workspace_id") == WORKSPACE and row.get("session_id") == session, "EXECUTION_RECEIPT_BINDING_MISMATCH")
        run_id = path.parent.name
        require(row.get("run_id") == run_id and isinstance(row.get("retained_root"), str)
                and key(row["retained_root"]) == key(path.parent) and isinstance(row.get("scratch_root"), str)
                and key(row["scratch_root"]) == key(root / "scratch" / run_id), "EXECUTION_ROOT_BINDING_MISMATCH")
        operation = row.get("operation")
        require(operation in ("copy", "build-run"), "UNSUPPORTED_EXECUTION_OPERATION")
        operations[operation] += 1
        item = {"run_id": run_id, "operation": operation, "source_receipt": actual,
                "source_receipt_freeze_verified": True, "recorded_status": row.get("status"), "input_bindings": []}
        if operation == "copy":
            require(isinstance(row.get("files"), list), "COPY_FILES_REQUIRED")
            item["copy_files"] = []
            for index, copied in enumerate(row["files"]):
                require(isinstance(copied, dict) and isinstance(copied.get("destination"), str)
                        and inside(copied["destination"], root / "work"), "COPY_DESTINATION_OUTSIDE_JOB_WORK")
                bind_input(copied.get("source"), item, "copy_source", str(index))
                item["copy_files"].append({"destination": copied["destination"], "recorded_status": copied.get("status")})
        else:
            require(isinstance(row.get("inputs"), dict), "BUILD_INPUTS_REQUIRED")
            for alias, input_ref in sorted(row["inputs"].items()):
                bind_input(input_ref, item, "build_input", alias)
            bind_input(row.get("compiler"), item, "compiler", "compiler")
            for phase, histogram in (("compile", compile_exits), ("run", run_exits)):
                phase_row = row.get(phase)
                require(phase_row is None or isinstance(phase_row, dict), "INVALID_EXECUTION_PHASE")
                code = phase_row.get("exit") if phase_row is not None else None
                require(code is None or type(code) is int, "INTEGER_RAW_EXIT_REQUIRED")
                item[f"{phase}_exit_raw"] = code
                if code is None:
                    if phase == "compile":
                        compile_unrecorded += 1
                    else:
                        run_unrecorded += 1
                else:
                    histogram[str(code)] += 1
            artifact = row.get("artifact")
            if artifact is not None:
                artifact = descriptor(artifact)
                require(inside(artifact["path"], root / "scratch" / run_id), "ARTIFACT_OUTSIDE_OWN_EXECUTION_SCRATCH")
                item["artifact"] = {"recorded_identity": artifact, "recovery": recovery(artifact)}
            else:
                item["artifact"] = None
            spec = row.get("spec")
            require(isinstance(spec, dict) and isinstance(spec.get("path"), str)
                    and inside(spec["path"], root / "work") and isinstance(spec.get("sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", spec["sha256"]), "INVALID_BUILD_SPEC_IDENTITY")
            spec_matches = [candidate for candidate, _ in selected if candidate["sha256"] == spec["sha256"]]
            item["spec_binding"] = {"recorded_path": spec["path"], "recorded_sha256": spec["sha256"],
                                     "original_bytes_not_recorded": True,
                                     "selected_identity_recovery": [recovery(candidate) for candidate in
                                        {version_key(candidate): candidate for candidate in spec_matches}.values()],
                                     "qualification": "Original receipt records spec SHA only; original byte count is not invented."}
        items.append(item)

    # Freeze/catalog/receipts must still match the selected versions at output.
    require(identity(expected_freeze)["sha256"] == freeze_identity["sha256"], "FREEZE_CHANGED_DURING_DERIVATION")
    require(version_key(identity(catalog_path)) == version_key(catalog_identity), "CATALOG_CHANGED_DURING_DERIVATION")
    for ref in receipt_refs.values():
        require(version_key(identity(ref["path"])) == version_key(ref), "FROZEN_FILE_IDENTITY_DRIFT")
    ordered_versions = [versions[name] for name in sorted(versions)]
    report = {"schema": "dsh.lab.execution_ledger.v1", "job_id": job, "session_id": session, "workspace_id": WORKSPACE,
              "provenance": {"role": "ARCHITECTURE_DERIVED_BOOKKEEPING", "constructor_original_delivery": False,
                             "does_not_repair_frozen_worker_ledger": True, "generator": identity(Path(__file__).absolute()),
                             "freeze": freeze_identity, "catalog": catalog_identity},
              "selection": {"receipt_rule": "Only explicit freeze candidate work/execution_records/<one-id>/receipt.json entries",
                            "recovery_universe": ["freeze.candidate_files", "exact job INPUT_CATALOG.references"],
                            "directory_scans": 0, "api_calls": 0, "model_calls": 0},
              "counts": {"receipts": len(items), "copy": operations["copy"], "build_run": operations["build-run"],
                         "compile_exit_raw_histogram": dict(sorted(compile_exits.items())),
                         "run_exit_raw_histogram": dict(sorted(run_exits.items())),
                         "compile_exit_not_recorded_or_not_executed": compile_unrecorded,
                         "run_exit_not_recorded_or_not_executed": run_unrecorded,
                         "unique_input_versions": len(ordered_versions),
                         "unavailable_input_versions_in_selected_evidence": sum(v["recovery"]["status"] == "UNAVAILABLE_IN_SELECTED_EVIDENCE" for v in ordered_versions)},
              "semantic_boundary": "Exit values, including 3 and 4, are raw execution observations only. No automatic scientific PASS/FAIL, no whole-machine absence claim, and no correction of the constructor's original delivery is asserted.",
              "executions": items, "input_versions": ordered_versions}
    return report


def publish(job, freeze_path, *, lab=LAB):
    report = build_ledger(job, freeze_path, lab=lab)
    destination = Path(lab).absolute() / "receipts" / "measurements" / f"{job}.execution-ledger.json"
    checked_path(destination, missing=True)
    if destination.exists():
        existing, _ = read_json(destination)
        require(existing == report, "EXISTING_LEDGER_DIFFERS_HISTORY_PRESERVED")
        disposition = "IDENTICAL_EXISTING_REPORT"
    else:
        mkdir(destination.parent)
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        disposition = "DERIVED_REPORT_CREATED"
    return {"status": disposition, "report": identity(destination), "counts": report["counts"],
            "provenance_role": "ARCHITECTURE_DERIVED_BOOKKEEPING"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--freeze", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(publish(args.job, args.freeze), ensure_ascii=False))
        return 0
    except (Refused, OSError) as exc:
        print(json.dumps({"status": "REFUSED", "code": str(exc) if isinstance(exc, Refused) else "IO_ERROR"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
