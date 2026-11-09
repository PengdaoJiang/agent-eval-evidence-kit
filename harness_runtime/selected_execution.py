"""Exact-catalog copy and fresh-artifact build/run for this lab only.

This is a deterministic execution interface, not an OS sandbox for executable
code or other tools. Compiler/runtime system dependencies remain OS-managed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

LAB = Path(__file__).resolve().parent / "workspace"
WORKSPACE = "00000000-0000-4000-8000-000000000001"
JOB_RE = re.compile(r"DS1017-XPM-[A-Z0-9]+(?:-[A-Z0-9]+)*\Z")
SESSION_RE = re.compile(r"session-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z")


class Refused(Exception):
    pass


def require(ok, code):
    if not ok:
        raise Refused(code)


def key(path):
    return os.path.normcase(os.path.abspath(path))


def checked_path(path, *, missing=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "ABSOLUTE_CLEAN_PATH_REQUIRED")
    require(not path.drive.startswith("\\\\"), "NETWORK_PATH_FORBIDDEN")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            require(missing, "PATH_MISSING")
            continue
        require(not stat.S_ISLNK(info.st_mode)
                and not (getattr(info, "st_file_attributes", 0)
                         & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)), "REPARSE_PATH_FORBIDDEN")
    return path


def inside(path, root):
    try:
        return os.path.commonpath([key(path), key(root)]) == key(root)
    except ValueError:
        return False


def child(root, relative):
    require(isinstance(relative, str) and relative and not ntpath.isabs(relative)
            and not Path(relative).is_absolute() and ":" not in relative, "RELATIVE_PATH_REQUIRED")
    parts = re.split(r"[\\/]", relative)
    require(all(part not in ("", ".", "..") and not part.endswith((".", " ")) for part in parts), "PATH_ESCAPE_FORBIDDEN")
    require(all(not re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", part.split(".", 1)[0], re.I)
                for part in parts), "DEVICE_PATH_FORBIDDEN")
    path = Path(root).joinpath(*parts)
    require(inside(path, root), "PATH_ESCAPE_FORBIDDEN")
    return checked_path(path, missing=True)


def mkdir(path):
    path = checked_path(path, missing=True)
    path.mkdir(parents=True, exist_ok=True)
    checked_path(path)
    require(path.is_dir(), "DIRECTORY_REQUIRED")


def open_regular(path):
    path = checked_path(path)
    require(stat.S_ISREG(path.stat().st_mode), "SINGLE_FILE_REQUIRED")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    stream = os.fdopen(os.open(path, flags), "rb")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise Refused("SINGLE_FILE_REQUIRED")
    return stream


def identity(path):
    digest = hashlib.sha256()
    size = 0
    with open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        while block := stream.read(1024 * 1024):
            size += len(block)
            digest.update(block)
        after = os.fstat(stream.fileno())
    require((before.st_size, before.st_mtime_ns, before.st_ino) ==
            (after.st_size, after.st_mtime_ns, after.st_ino), "FILE_CHANGED_DURING_READ")
    return {"path": str(path), "bytes": size, "sha256": digest.hexdigest()}


def same_identity(actual, expected):
    return actual["bytes"] == expected["bytes"] and actual["sha256"] == expected["sha256"]


def load_json(path):
    with open_regular(path) as stream:
        data = stream.read()
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise Refused("INVALID_JSON") from None
    return value, hashlib.sha256(data).hexdigest()


def write_json(path, value):
    checked_path(path, missing=True)
    with open(path, "x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


class Context:
    def __init__(self, job, *, lab=LAB):
        require(isinstance(job, str) and len(job) <= 100 and JOB_RE.fullmatch(job), "INVALID_JOB_NAME")
        self.job = job
        self.lab = checked_path(Path(lab).absolute())
        self.root = checked_path(self.lab / "runs" / job)
        self.work = checked_path(self.root / "work")
        self.scratch = checked_path(self.root / "scratch")
        prepare_path = self.root / ".control" / "prepare.json"
        contract_path = self.root / "evidence" / "TASK_CONTRACT.json"
        catalog_path = self.root / "evidence" / "INPUT_CATALOG.json"
        prepare, prepare_hash = load_json(prepare_path)
        contract, contract_hash = load_json(contract_path)
        catalog, catalog_hash = load_json(catalog_path)
        require(all(isinstance(value, dict) for value in (prepare, contract, catalog)), "OBJECT_BINDING_REQUIRED")
        require(prepare.get("schema") == "rea.harness.task-storage.prepare.v1"
                and prepare.get("job") == job and prepare.get("job_id") == job
                and prepare.get("workspace_id") == WORKSPACE
                and isinstance(prepare.get("root"), str) and key(prepare["root"]) == key(self.root)
                and isinstance(prepare.get("target"), str) and key(prepare["target"]) == key(self.lab), "PREPARE_BINDING_MISMATCH")
        self.session = prepare.get("session_id")
        require(isinstance(self.session, str) and SESSION_RE.fullmatch(self.session), "SESSION_BINDING_MISMATCH")
        require(contract.get("job_id") == job and contract.get("session_id") == self.session
                and contract.get("workspace_id") == WORKSPACE, "CONTRACT_BINDING_MISMATCH")
        require(catalog.get("job_id") == job and isinstance(catalog.get("references"), list), "CATALOG_BINDING_MISMATCH")
        self.bindings = [(prepare_path, prepare_hash), (contract_path, contract_hash), (catalog_path, catalog_hash)]
        self.references = {}
        for ref in catalog["references"]:
            require(isinstance(ref, dict) and isinstance(ref.get("path"), str)
                    and type(ref.get("bytes")) is int and ref["bytes"] >= 0
                    and isinstance(ref.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]), "INVALID_CATALOG_REFERENCE")
            require(Path(ref["path"]).is_absolute() and ".." not in Path(ref["path"]).parts, "INVALID_CATALOG_PATH")
            require(key(ref["path"]) not in self.references, "DUPLICATE_CATALOG_PATH")
            self.references[key(ref["path"])] = ref

    def unchanged(self):
        for path, expected in self.bindings:
            require(identity(path)["sha256"] == expected, "JOB_BINDING_CHANGED")

    def selected(self, source):
        require(isinstance(source, str) and Path(source).is_absolute()
                and ".." not in Path(source).parts, "EXACT_ABSOLUTE_SOURCE_REQUIRED")
        ref = self.references.get(key(source))
        require(ref is not None, "SOURCE_NOT_SELECTED")
        path = checked_path(Path(source))
        actual = identity(path)
        require(same_identity(actual, ref), "SELECTED_SOURCE_IDENTITY_CHANGED")
        return path, actual

    def input_file(self, value):
        require(isinstance(value, str), "INPUT_PATH_REQUIRED")
        if Path(value).is_absolute():
            path = checked_path(Path(value)) if inside(value, self.work) else None
            if path is None:
                return self.selected(value)
        else:
            path = child(self.work, value)
        return path, identity(path)

    def new_record(self, operation):
        run_id = f"selected-{operation}-{uuid.uuid4().hex}"
        records = child(self.work, "execution_records")
        mkdir(records)
        retained = child(records, run_id)
        temporary = child(self.scratch, run_id)
        # UUID roots are exclusively created, never adopted from prior attempts.
        retained.mkdir()
        temporary.mkdir()
        write_json(temporary / "execution_record.json", {"run_id": run_id, "retained_root": str(retained),
                                                        "receipt": str(retained / "receipt.json")})
        return retained, temporary, {"schema": "dsh.lab.selected_execution.receipt.v1", "operation": operation,
            "job_id": self.job, "session_id": self.session, "workspace_id": WORKSPACE,
            "run_id": run_id, "retained_root": str(retained), "scratch_root": str(temporary),
            "binding_files": [{"path": str(path), "sha256": digest} for path, digest in self.bindings],
            "claim_boundary": "Execution evidence only. Raw exits, including consumer UNKNOWN=3 or mutation=4, require caller interpretation. No scientific acceptance or OS isolation is claimed."}


def copy_selected(context, items):
    require(items, "EXPLICIT_FILE_SELECTION_REQUIRED")
    plans = []
    destinations = set()
    for source, destination in items:
        source_path, expected = context.selected(source)
        dest = child(context.work, destination)
        require(not inside(dest, context.work / "execution_records"), "RETAINED_RECORDS_ARE_NOT_COPY_DESTINATIONS")
        require(key(dest) not in destinations, "DUPLICATE_DESTINATION")
        destinations.add(key(dest))
        if dest.exists():
            require(same_identity(identity(dest), expected), "DIFFERENT_DESTINATION_EXISTS")
        plans.append((source_path, dest, expected))
    context.unchanged()
    retained, temporary, record = context.new_record("copy")
    record["files"] = []
    code = 0
    try:
        for source, dest, expected in plans:
            context.unchanged()
            require(same_identity(identity(source), expected), "SELECTED_SOURCE_IDENTITY_CHANGED")
            mkdir(dest.parent)
            if dest.exists():
                require(same_identity(identity(dest), expected), "DIFFERENT_DESTINATION_EXISTS")
                status = "IDENTICAL_ALREADY_PRESENT"
            else:
                staged = temporary / f"copy-{uuid.uuid4().hex}.tmp"
                try:
                    with open_regular(source) as incoming, open(staged, "xb") as outgoing:
                        while block := incoming.read(1024 * 1024):
                            outgoing.write(block)
                    require(same_identity(identity(staged), expected), "SELECTED_SOURCE_CHANGED_DURING_COPY")
                    checked_path(dest, missing=True)
                    # Exclusive hard-link publication is atomic and cannot overwrite.
                    try:
                        os.link(staged, dest, follow_symlinks=False)
                        status = "COPIED"
                    except FileExistsError:
                        require(same_identity(identity(dest), expected), "DIFFERENT_DESTINATION_EXISTS")
                        status = "IDENTICAL_ALREADY_PRESENT"
                finally:
                    staged.unlink(missing_ok=True)
            record["files"].append({"source": expected, "destination": str(dest), "status": status})
        record["status"] = "COPY_COMPLETE"
    except (Refused, OSError) as exc:
        record.update(status="COPY_FAILED", failure=failure(exc))
        code = 1
    receipt = retained / "receipt.json"
    write_json(receipt, record)
    return {"status": record["status"], "receipt": str(receipt), "copied_or_identical": len(record["files"])}, code


PLACEHOLDER = re.compile(r"\{(artifact|scratch|input:([A-Za-z][A-Za-z0-9_]*))\}")


def expand_arg(value, inputs, scratch, artifact):
    require(isinstance(value, str) and "\0" not in value and "\n" not in value and "\r" not in value, "INVALID_ARGV_ITEM")
    matches = list(PLACEHOLDER.finditer(value))
    if not matches:
        require(not re.search(r"[\\/@:{}]", value) and value not in (".", ".."), "UNDECLARED_PATH_OR_RESPONSE_FILE")
        return value
    require(len(matches) == 1, "ONE_PATH_PLACEHOLDER_PER_ARGUMENT")
    match = matches[0]
    prefix, suffix = value[:match.start()], value[match.end():]
    require(not prefix or re.fullmatch(r"-[A-Za-z0-9_=,+.:-]*", prefix), "INVALID_PATH_OPTION_PREFIX")
    if match.group(1) == "scratch":
        require(not suffix or suffix[0] in "/\\", "INVALID_SCRATCH_SUFFIX")
        path = child(scratch, suffix[1:]) if suffix else scratch
    else:
        require(not suffix, "FILE_PLACEHOLDER_SUFFIX_FORBIDDEN")
        if match.group(1) == "artifact":
            path = artifact
        else:
            require(match.group(2) in inputs, "UNDECLARED_INPUT_ALIAS")
            path = inputs[match.group(2)]
    return prefix + str(checked_path(path, missing=True))


def failure(exc):
    return {"code": str(exc) if isinstance(exc, Refused) else "OS_OPERATION_FAILED",
            "type": type(exc).__name__, "errno": getattr(exc, "errno", None)}


def invoke(argv, scratch, retained, phase, record):
    stdout = retained / f"{phase}.stdout.bin"
    stderr = retained / f"{phase}.stderr.bin"
    row = {"argv": argv, "cwd": str(scratch), "stdout": str(stdout), "stderr": str(stderr), "exit": None}
    record[phase] = row
    checked_path(scratch)
    checked_path(stdout, missing=True)
    checked_path(stderr, missing=True)
    with open(stdout, "xb") as out, open(stderr, "xb") as err:
        result = subprocess.run(argv, cwd=scratch, shell=False, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, check=False)
    row["exit"] = result.returncode
    return result.returncode


def build_run(context, spec_relative):
    spec_path = child(context.work, spec_relative)
    spec, spec_hash = load_json(spec_path)
    require(isinstance(spec, dict) and spec.get("schema") == "dsh.lab.selected_execution.build-run.v1", "BUILD_SPEC_SCHEMA_REQUIRED")
    require(set(spec) <= {"schema", "compiler", "inputs", "compile_args", "artifact", "run_args"}, "UNKNOWN_BUILD_SPEC_FIELD")
    compiler, compiler_identity = context.selected(spec.get("compiler"))
    require(isinstance(spec.get("inputs"), dict) and isinstance(spec.get("compile_args"), list)
            and isinstance(spec.get("run_args", []), list), "EXPLICIT_ARGV_AND_INPUTS_REQUIRED")
    inputs, input_identities = {}, {}
    for name, value in spec["inputs"].items():
        require(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name), "INVALID_INPUT_ALIAS")
        inputs[name], input_identities[name] = context.input_file(value)
    # Validate the artifact name before creating any execution roots.
    child(context.scratch, spec.get("artifact"))
    context.unchanged()
    retained, scratch, record = context.new_record("build-run")
    record.update(spec={"path": str(spec_path), "sha256": spec_hash}, compiler=compiler_identity,
                  inputs=input_identities, compile=None, run=None, artifact=None)
    code = 1
    try:
        artifact = child(scratch, spec["artifact"])
        mkdir(artifact.parent)
        require(not artifact.exists(), "FRESH_ARTIFACT_REQUIRED")
        compile_argv = [str(compiler)] + [expand_arg(arg, inputs, scratch, artifact) for arg in spec["compile_args"]]
        run_argv = [str(artifact)] + [expand_arg(arg, inputs, scratch, artifact) for arg in spec.get("run_args", [])]
        record["planned_run_argv"] = run_argv
        context.unchanged()
        require(load_json(spec_path)[1] == spec_hash, "BUILD_SPEC_CHANGED")
        require(same_identity(identity(compiler), compiler_identity), "COMPILER_IDENTITY_CHANGED")
        for name, path in inputs.items():
            require(same_identity(identity(path), input_identities[name]), "BUILD_INPUT_CHANGED")
        code = invoke(compile_argv, scratch, retained, "compile", record)
        if code != 0:
            record["status"] = "COMPILE_FAILED_RUN_SKIPPED"
            if artifact.exists():
                try:
                    record["artifact"] = identity(artifact)
                    record["artifact"]["qualification"] = "Present after failed compilation; never executed"
                except (Refused, OSError) as exc:
                    record["failed_compile_artifact_error"] = failure(exc)
        else:
            record["artifact"] = identity(artifact)
            context.unchanged()
            require(same_identity(identity(artifact), record["artifact"]), "ARTIFACT_CHANGED_BEFORE_RUN")
            code = invoke(run_argv, scratch, retained, "run", record)
            record["status"] = "RUN_FINISHED"
    except (Refused, OSError) as exc:
        record.update(status="EXECUTION_REFUSED_OR_FAILED", failure=failure(exc))
        code = 1
    receipt = retained / "receipt.json"
    write_json(receipt, record)
    summary = {"status": record["status"], "receipt": str(receipt),
               "compile_exit": record["compile"]["exit"] if record["compile"] else None,
               "run_exit": record["run"]["exit"] if record["run"] else None,
               "artifact_sha256": record["artifact"]["sha256"] if record["artifact"] else None}
    return summary, code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    sub = parser.add_subparsers(dest="operation", required=True)
    copy = sub.add_parser("copy", help="Copy only exact selected files to new relative paths under this job's work")
    copy.add_argument("--item", nargs=2, action="append", required=True, metavar=("EXACT_SOURCE", "WORK_RELATIVE_DEST"))
    build = sub.add_parser("build-run", help="Compile in a unique scratch root and run only its newly produced artifact")
    build.add_argument("--spec", required=True, help="JSON path relative to this job's work directory")
    args = parser.parse_args(argv)
    try:
        context = Context(args.job)
        result, code = copy_selected(context, args.item) if args.operation == "copy" else build_run(context, args.spec)
    except (Refused, OSError) as exc:
        result, code = {"status": "REFUSED", "failure": failure(exc)}, 1
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
