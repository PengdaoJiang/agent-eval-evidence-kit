#!/usr/bin/env python3
"""Collect one isolated Codex Agent-use evaluation call.

This collector is intentionally independent from benchmark_probe.py.  Agent
tasks are expected to call tools, and every JSONL event is retained verbatim
so a future CLI item type cannot silently disappear from the evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import locale
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
from typing import Any


SCHEMA_VERSION = 1
PROBE_VERSION = "1.3.1"
FIRST_ROUND_MODELS = (
    "config-a",
    "config-b",
    "config-c",
    "config-d",
)

FIXTURES: dict[str, dict[str, Any]] = {
    "f1_manifest_join_readonly": {
        "sandbox": "read-only",
        "schema": "final_schema.json",
        "allowed_writes": [],
        "required_changed_paths": [],
        "minimum_command_completions": 4,
        "prompt": """
Work only with the local fixture in the current directory. Do not use the
network and do not write or modify any file.

This is a deliberately staged tool-use task. Use only the exact cross-host
Python reader form `python -B read_one.py RELATIVE_PATH` and keep these
four reads as four separate tool calls; do not combine two input paths in one
command:
1. Run `python -B read_one.py manifest.json` only.
2. Run the reader for the jobs path named by the manifest only.
3. Run the reader for the owners path named by the manifest only.
4. Run the reader for the rules path named by the manifest only.

Select jobs using every rule, join owner_alias to its team, and sort exactly as
the rules request. Return the structured answer required by the supplied output
schema. team_counts must include only teams represented by selected jobs. Do
not echo the audit pair id and do not add prose.
""".strip(),
    },
    "f2_python_bugfix": {
        "sandbox": "read-only",
        "schema": "final_schema.json",
        "allowed_writes": [],
        "required_changed_paths": [],
        "minimum_command_completions": 3,
        "prompt": """
Work only with the local fixture in the current directory. Do not use the
network, Git, package managers, or write or modify any file.

Perform exactly these three stages with real tools. Keep them as three separate
tool calls, do not batch commands, and do not run any command twice:
1. Run `python -B -m unittest discover -s tests -v` to establish the expected red
   baseline. This command is expected to fail; do not treat that failure as a
   reason to rerun it.
2. Run `python -B read_one.py src/windowing.py`.
3. Run `python -B read_one.py tests/test_windowing.py` in a separate tool call.

Without editing the implementation, diagnose the two root causes and compute
the corrected merge result for every public test input. Use the machine-readable
root-cause codes admitted by the supplied schema. Return only the structured
final response required by that schema; do not add prose.
""".strip(),
    },
    "f3_incident_report": {
        "sandbox": "read-only",
        "schema": "final_schema.json",
        "allowed_writes": [],
        "required_changed_paths": [],
        "minimum_command_completions": 5,
        "prompt": """
Work only with the local fixture in the current directory. Do not use the
network, Git, package managers, or write or modify any file.

Use exactly five real tool calls. For every read use only
`python -B read_one.py RELATIVE_PATH`. Read manifest.json first, then read the
east log, west log, codebook, and rules paths named by the manifest in separate
tool calls and in that order. Never combine two input paths, repeat a command,
or run any other command. Apply every inclusion, deduplication, enrichment, and
ordering rule, then return the incident report directly in the structured
response required by the supplied schema.

Do not create an output file, run a validator, echo the audit pair id, or add
prose.
""".strip(),
    },
}

POLICY_READ_PATHS = (
    "manifest.json",
    "data/jobs.csv",
    "data/owners.json",
    "data/rules.json",
    "src/windowing.py",
    "tests/test_windowing.py",
    "logs/east.jsonl",
    "logs/west.jsonl",
    "data/codebook.json",
)
WINDOWS_POWERSHELL = (
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def atomic_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05 * (attempt + 1))


def decode_output(value: bytes | None) -> str:
    if not value:
        return ""
    encodings = ("utf-8", locale.getpreferredencoding(False), "cp936", "utf-16le")
    seen: set[str] = set()
    for encoding in encodings:
        if not encoding or encoding.lower() in seen:
            continue
        seen.add(encoding.lower())
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


def capture_command(
    command: list[str],
    timeout_s: float = 30.0,
    *,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter_ns()
    try:
        completed = subprocess.run(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_s,
        )
        return {
            "command": command,
            "exit_code": completed.returncode,
            "timed_out": False,
            "duration_ms": (time.perf_counter_ns() - started) / 1_000_000,
            "stdout": decode_output(completed.stdout),
            "stderr": decode_output(completed.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "exit_code": None,
            "timed_out": True,
            "duration_ms": (time.perf_counter_ns() - started) / 1_000_000,
            "stdout": decode_output(exc.stdout),
            "stderr": decode_output(exc.stderr),
        }
    except OSError as exc:
        return {
            "command": command,
            "exit_code": None,
            "timed_out": False,
            "duration_ms": (time.perf_counter_ns() - started) / 1_000_000,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }


def codex_environment(codex_home: pathlib.Path) -> dict[str, str]:
    """Build the controlled CLI environment without exposing auth material."""
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    environment["NO_COLOR"] = "1"
    environment["TERM"] = "dumb"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    python_directory = str(pathlib.Path(sys.executable).resolve().parent)
    environment["PATH"] = python_directory + os.pathsep + environment.get("PATH", "")
    return environment


def execpolicy_cases() -> list[dict[str, Any]]:
    """Return exact runtime-lowered argv cases for the controlled rule set."""
    inner_cases: list[dict[str, Any]] = [
        {
            "name": f"allow_read_{path.replace('/', '_').replace('.', '_')}",
            "argv": ["python", "-B", "read_one.py", path],
            "expected_decision": "allow",
        }
        for path in POLICY_READ_PATHS
    ]
    inner_cases.extend(
        [
            {
                "name": "allow_unittest",
                "argv": [
                    "python",
                    "-B",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-v",
                ],
                "expected_decision": "allow",
            },
            {
                "name": "deny_python_code",
                "argv": ["python", "-c", "print('not allowed')"],
                "expected_decision": None,
            },
            {
                "name": "deny_pip",
                "argv": ["python", "-m", "pip", "--version"],
                "expected_decision": None,
            },
            {
                "name": "deny_unlisted_reader_path",
                "argv": ["python", "-B", "read_one.py", "secrets.txt"],
                "expected_decision": None,
            },
            {
                "name": "deny_removed_validator",
                "argv": ["python", "-B", "validate_report.py", "out/summary.json"],
                "expected_decision": None,
            },
            {
                "name": "deny_changed_unittest_arguments",
                "argv": [
                    "python",
                    "-B",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-q",
                ],
                "expected_decision": None,
            },
            {
                "name": "known_prefix_boundary_trailing_argument",
                "argv": [
                    "python",
                    "-B",
                    "read_one.py",
                    "manifest.json",
                    "unexpected-tail",
                ],
                "expected_decision": "allow",
                "known_prefix_boundary": True,
            },
        ]
    )
    cases: list[dict[str, Any]] = []
    for case in inner_cases:
        inner = {**case, "argv": list(case["argv"]), "argv_form": "inner"}
        cases.append(inner)
        payload = " ".join(str(value) for value in case["argv"])
        outer_is_trailing_boundary_probe = bool(
            case.get("known_prefix_boundary", False)
        )
        cases.append(
            {
                **case,
                "name": f"{case['name']}_outer_powershell",
                "argv": [WINDOWS_POWERSHELL, "-Command", payload],
                "argv_form": "outer_powershell",
                # The outer command payload is one argv token. Appending text
                # changes that token rather than extending the matched argv,
                # so the same trailing-argument probe must be denied here.
                "expected_decision": (
                    None
                    if outer_is_trailing_boundary_probe
                    else case["expected_decision"]
                ),
                "known_prefix_boundary": False,
            }
        )
        if outer_is_trailing_boundary_probe:
            allowed_payload = " ".join(
                str(value) for value in case["argv"][:-1]
            )
            cases.append(
                {
                    "name": "known_prefix_boundary_outer_fourth_argv",
                    "argv": [
                        WINDOWS_POWERSHELL,
                        "-Command",
                        allowed_payload,
                        str(case["argv"][-1]),
                    ],
                    "argv_form": "outer_powershell",
                    "expected_decision": "allow",
                    "known_prefix_boundary": True,
                }
            )
    return cases


def _policy_decision(payload: dict[str, Any]) -> str | None:
    decision = payload.get("decision")
    return str(decision) if isinstance(decision, str) else None


def _matched_prefix(payload: dict[str, Any]) -> list[str] | None:
    matches = payload.get("matchedRules")
    if not isinstance(matches, list) or not matches:
        return None
    first = matches[0]
    if not isinstance(first, dict):
        return None
    match = first.get("prefixRuleMatch")
    if not isinstance(match, dict):
        return None
    prefix = match.get("matchedPrefix")
    if not isinstance(prefix, list):
        return None
    return [str(value) for value in prefix]


_TEMP_PATH_ALIAS_WARNING_PREFIX = (
    "WARNING: proceeding, even though we could not create PATH aliases: "
    "Refusing to create helper binaries under temporary dir "
)


def _classify_execpolicy_stderr(
    stderr: str,
    *,
    codex_home: pathlib.Path,
) -> tuple[list[str], list[str]]:
    """Separate one precisely scoped Codex temp-home warning from real stderr."""
    expected_home = str(codex_home.resolve()).replace("/", "\\").casefold()
    expected_suffix = f'(codex_home: AbsolutePathBuf("{expected_home}"))'.casefold()
    ignored: list[str] = []
    unexpected: list[str] = []
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = line.replace("\\\\", "\\").replace("/", "\\").casefold()
        if (
            line.startswith(_TEMP_PATH_ALIAS_WARNING_PREFIX)
            and normalized.endswith(expected_suffix)
        ):
            ignored.append(line)
        else:
            unexpected.append(line)
    return ignored, unexpected


def run_execpolicy_preflight(
    *,
    codex: str,
    codex_home: pathlib.Path,
    rules: pathlib.Path,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Exercise every controlled policy case without starting a model call."""
    if not codex_home.is_dir():
        raise FileNotFoundError(f"CODEX_HOME not found: {codex_home}")
    if not rules.is_file():
        raise FileNotFoundError(f"Rules file not found: {rules}")
    environment = codex_environment(codex_home)
    results: list[dict[str, Any]] = []
    for resolve_host_executables in (False, True):
        for case in execpolicy_cases():
            command = [codex, "execpolicy", "check", "--pretty"]
            if resolve_host_executables:
                command.append("--resolve-host-executables")
            command.extend(["--rules", str(rules), "--", *case["argv"]])
            captured = capture_command(
                command,
                timeout_s=timeout_s,
                environment=environment,
            )
            parsed: dict[str, Any] | None = None
            parse_error: str | None = None
            try:
                candidate = json.loads(str(captured.get("stdout") or ""))
                if isinstance(candidate, dict):
                    parsed = candidate
                else:
                    parse_error = f"non-object JSON: {type(candidate).__name__}"
            except json.JSONDecodeError as exc:
                parse_error = f"{type(exc).__name__}: {exc}"
            actual_decision = _policy_decision(parsed or {})
            matched_prefix = _matched_prefix(parsed or {})
            expected_decision = case["expected_decision"]
            stderr = str(captured.get("stderr") or "")
            ignored_stderr, unexpected_stderr = _classify_execpolicy_stderr(
                stderr,
                codex_home=codex_home,
            )
            stderr_classification = (
                "unexpected"
                if unexpected_stderr
                else "known_temp_home_path_alias_warning"
                if ignored_stderr
                else "empty"
            )
            passed = (
                captured.get("exit_code") == 0
                and not captured.get("timed_out")
                and not unexpected_stderr
                and parse_error is None
                and actual_decision == expected_decision
                and (
                    matched_prefix is not None
                    if expected_decision is not None
                    else matched_prefix is None
                )
            )
            results.append(
                {
                    "name": case["name"],
                    "argv": list(case["argv"]),
                    "argv_form": case["argv_form"],
                    "resolve_host_executables": resolve_host_executables,
                    "expected_decision": expected_decision,
                    "actual_decision": actual_decision,
                    "matched_prefix": matched_prefix,
                    "known_prefix_boundary": bool(
                        case.get("known_prefix_boundary", False)
                    ),
                    "pass": passed,
                    "exit_code": captured.get("exit_code"),
                    "timed_out": captured.get("timed_out"),
                    "stderr": stderr,
                    "ignored_stderr_lines": ignored_stderr,
                    "unexpected_stderr_lines": unexpected_stderr,
                    "ignored_stderr_count": len(ignored_stderr),
                    "ignored_stderr_sha256": (
                        sha256_text("\n".join(ignored_stderr))
                        if ignored_stderr
                        else None
                    ),
                    "stderr_classification": stderr_classification,
                    "parse_error": parse_error,
                }
            )
    return {
        "kind": "codex_agent_eval_execpolicy_preflight",
        "probe_version": PROBE_VERSION,
        "codex_home": str(codex_home),
        "rules": str(rules),
        "rules_sha256": sha256_bytes(rules.read_bytes()),
        "case_count": len(results),
        "passed": all(bool(result["pass"]) for result in results),
        "known_prefix_boundary": (
            "prefix_rule is prefix-based, so a trailing argv remains allowed; "
            "fixture readers, frozen inputs, and JSONL command auditing provide "
            "the exactness backstop"
        ),
        "cases": results,
    }
def snapshot_workspace(root: pathlib.Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories.sort()
        filenames.sort()
        current_path = pathlib.Path(current)
        for name in filenames:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                files[relative] = {
                    "type": "symlink",
                    "target": os.readlink(path),
                }
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                files[relative] = {
                    "type": "unreadable",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                continue
            record: dict[str, Any] = {
                "type": "file",
                "size_bytes": len(raw),
                "sha256": sha256_bytes(raw),
            }
            if len(raw) <= 262_144:
                try:
                    record["content_utf8"] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    record["content_utf8"] = None
            files[relative] = record
    identity = [
        {
            "path": path,
            **{key: value for key, value in record.items() if key != "content_utf8"},
        }
        for path, record in sorted(files.items())
    ]
    return {
        "tree_sha256": sha256_text(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        ),
        "file_count": len(files),
        "files": files,
    }


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_files = before["files"]
    after_files = after["files"]
    before_paths = set(before_files)
    after_paths = set(after_files)
    added = sorted(after_paths - before_paths)
    removed = sorted(before_paths - after_paths)
    modified = sorted(
        path
        for path in before_paths & after_paths
        if {
            key: value
            for key, value in before_files[path].items()
            if key != "content_utf8"
        }
        != {
            key: value
            for key, value in after_files[path].items()
            if key != "content_utf8"
        }
    )
    return {
        "added": added,
        "modified": modified,
        "removed": removed,
        "changed_paths": sorted(set(added + modified + removed)),
    }


def parse_json_lines(lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parsed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for record in lines:
        try:
            event = json.loads(record["line"])
        except (json.JSONDecodeError, TypeError) as exc:
            rejected.append(
                {
                    "t_ms": record.get("t_ms"),
                    "line": record.get("line"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if not isinstance(event, dict):
            rejected.append(
                {
                    "t_ms": record.get("t_ms"),
                    "line": record.get("line"),
                    "error": f"non-object JSON event: {type(event).__name__}",
                }
            )
            continue
        parsed.append({"t_ms": record["t_ms"], "event": event})
    return parsed, rejected


def read_stream(stream: Any, destination: list[dict[str, Any]], start_ns: int) -> None:
    try:
        for line in iter(stream.readline, ""):
            destination.append(
                {
                    "t_ms": (time.perf_counter_ns() - start_ns) / 1_000_000,
                    "line": line.rstrip("\r\n"),
                }
            )
    finally:
        stream.close()


def build_prompt(fixture_id: str, pair_id: str) -> str:
    base = str(FIXTURES[fixture_id]["prompt"])
    return f"[PAIR_ID:{pair_id}]\n{base}"


def build_codex_command(
    *,
    codex: str,
    fixture_id: str,
    fixture_root: pathlib.Path,
    model: str,
    effort: str,
) -> list[str]:
    config = FIXTURES[fixture_id]
    return [
        codex,
        "--ask-for-approval",
        "never",
        "--sandbox",
        str(config["sandbox"]),
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        'cli_auth_credentials_store="file"',
        "exec",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "--json",
        "--output-schema",
        str((fixture_root / str(config["schema"])).resolve()),
        "-",
    ]


def audit_codex_home(codex_home: pathlib.Path) -> dict[str, Any]:
    """Verify the isolated home while never reading auth bytes into evidence."""
    if not codex_home.is_dir():
        raise FileNotFoundError(f"CODEX_HOME not found: {codex_home}")
    auth = codex_home / "auth.json"
    config = codex_home / "config.toml"
    rules = codex_home / "rules" / "default.rules"
    if not auth.is_file() or auth.stat().st_size <= 0:
        raise FileNotFoundError(f"Non-empty isolated auth.json not found: {auth}")
    if not config.is_file():
        raise FileNotFoundError(f"Isolated config.toml not found: {config}")
    if not rules.is_file():
        raise FileNotFoundError(f"Isolated default.rules not found: {rules}")
    return {
        "path": str(codex_home),
        "auth_present": True,
        "config_sha256": sha256_bytes(config.read_bytes()),
        "rules_sha256": sha256_bytes(rules.read_bytes()),
    }


def run(args: argparse.Namespace) -> int:
    required = {
        "fixture_id": args.fixture_id,
        "fixture_root": args.fixture_root,
        "output": args.output,
        "host_label": args.host_label,
        "sequence_index": args.sequence_index,
        "block_id": args.block_id,
        "pair_id": args.pair_id,
        "host_order": args.host_order,
        "model": args.model,
    }
    missing = sorted(key for key, value in required.items() if value is None)
    if missing:
        raise ValueError("Missing collector arguments: " + ", ".join(missing))
    if args.fixture_id not in FIXTURES:
        raise ValueError(f"Unknown fixture: {args.fixture_id}")
    if args.timeout_s <= 0:
        raise ValueError("--timeout-s must be positive")
    fixture_root = pathlib.Path(args.fixture_root).resolve()
    if not fixture_root.is_dir():
        raise FileNotFoundError(f"Fixture root not found: {fixture_root}")
    config = FIXTURES[args.fixture_id]
    schema_path = fixture_root / str(config["schema"])
    if not schema_path.is_file():
        raise FileNotFoundError(f"Output schema not found: {schema_path}")
    codex_home = pathlib.Path(args.codex_home).resolve()
    codex_home_audit = audit_codex_home(codex_home)
    environment = codex_environment(codex_home)

    output_path = pathlib.Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt(args.fixture_id, args.pair_id)
    original_schema_sha256 = sha256_bytes(schema_path.read_bytes())
    before = snapshot_workspace(fixture_root)
    codex_version = capture_command(
        [args.codex, "--version"],
        timeout_s=20,
        environment=environment,
    )
    powershell_language_mode = capture_command(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ExecutionContext.SessionState.LanguageMode",
        ],
        timeout_s=20,
    )
    command = build_codex_command(
        codex=args.codex,
        fixture_id=args.fixture_id,
        fixture_root=fixture_root,
        model=args.model,
        effort=args.effort,
    )

    stdout_lines: list[dict[str, Any]] = []
    stderr_lines: list[dict[str, Any]] = []
    started_utc = utc_now()
    start_ns = time.perf_counter_ns()
    process: subprocess.Popen[str] | None = None
    timed_out = False
    harness_error: str | None = None
    return_code: int | None = None
    kill_tree_result: dict[str, Any] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=str(fixture_root),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=read_stream,
            args=(process.stdout, stdout_lines, start_ns),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=read_stream,
            args=(process.stderr, stderr_lines, start_ns),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        process.stdin.write(prompt)
        process.stdin.close()
        try:
            process.wait(timeout=args.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                kill_tree_result = capture_command(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    timeout_s=20,
                )
            else:
                process.kill()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        return_code = process.returncode
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
    except Exception as exc:
        harness_error = f"{type(exc).__name__}: {exc}"
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        return_code = process.returncode if process is not None else None

    wall_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
    completed_utc = utc_now()
    after = snapshot_workspace(fixture_root)
    workspace_diff = diff_snapshots(before, after)
    allowed = set(str(value).casefold() for value in config["allowed_writes"])
    required = set(str(value).casefold() for value in config["required_changed_paths"])
    changed = set(str(value).casefold() for value in workspace_diff["changed_paths"])
    write_audit = {
        "allowed_paths": sorted(allowed),
        "required_changed_paths": sorted(required),
        "unexpected_changed_paths": sorted(changed - allowed),
        "missing_required_changes": sorted(required - changed),
        "passed": not (changed - allowed) and not (required - changed),
    }

    parsed_events, rejected_lines = parse_json_lines(stdout_lines)
    event_types: list[str] = []
    item_types: list[str] = []
    agent_messages: list[str] = []
    usage: dict[str, Any] | None = None
    for record in parsed_events:
        event = record["event"]
        event_type = str(event.get("type", "")) if isinstance(event, dict) else ""
        event_types.append(event_type)
        if event_type.startswith("item.") and isinstance(event, dict):
            item = event.get("item")
            if isinstance(item, dict):
                item_type = str(item.get("type", ""))
                item_types.append(item_type)
                if event_type == "item.completed" and item_type == "agent_message":
                    agent_messages.append(str(item.get("text", "")))
        if event_type == "turn.completed" and isinstance(event, dict):
            candidate = event.get("usage")
            if isinstance(candidate, dict):
                usage = candidate

    final_response_json: Any = None
    final_response_error: str | None = None
    if agent_messages:
        try:
            final_response_json = json.loads(agent_messages[-1])
        except json.JSONDecodeError as exc:
            final_response_error = f"{type(exc).__name__}: {exc}"

    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "codex_agent_eval_call",
        "probe_version": PROBE_VERSION,
        "host_label": args.host_label,
        "hostname": socket.gethostname(),
        "python_version": sys.version,
        "tool_python_directory": str(pathlib.Path(sys.executable).resolve().parent),
        "sequence_index": args.sequence_index,
        "block_id": args.block_id,
        "pair_id": args.pair_id,
        "host_order": args.host_order,
        "fixture_id": args.fixture_id,
        "requested_model": args.model,
        "reasoning_effort": args.effort,
        "sandbox": config["sandbox"],
        "started_utc": started_utc,
        "completed_utc": completed_utc,
        "wall_ms": wall_ms,
        "exit_code": return_code,
        "timed_out": timed_out,
        "harness_error": harness_error,
        "timeout_kill_tree": kill_tree_result,
        "prompt_sha256": sha256_text(prompt),
        "schema_sha256": original_schema_sha256,
        "codex_home_audit": codex_home_audit,
        "codex_version": codex_version,
        "powershell_language_mode": powershell_language_mode,
        "command": command,
        "event_types": event_types,
        "item_types": item_types,
        "jsonl_events": parsed_events,
        "stdout_lines": stdout_lines,
        "non_json_stdout_lines": rejected_lines,
        "stderr_lines": stderr_lines,
        "agent_message_count": len(agent_messages),
        "agent_messages": agent_messages,
        "final_response_json": final_response_json,
        "final_response_error": final_response_error,
        "usage": usage,
        "workspace": {
            "before": before,
            "after": after,
            "diff": workspace_diff,
            "write_audit": write_audit,
        },
    }
    atomic_json(output_path, result)
    turn_completed = "turn.completed" in event_types
    return 0 if return_code == 0 and not timed_out and turn_completed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--policy-preflight", action="store_true")
    parser.add_argument("--rules")
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--fixture-id", choices=sorted(FIXTURES))
    parser.add_argument("--fixture-root")
    parser.add_argument("--output")
    parser.add_argument("--host-label", choices=("local", "remote"))
    parser.add_argument("--sequence-index", type=int)
    parser.add_argument("--block-id")
    parser.add_argument("--pair-id")
    parser.add_argument("--host-order")
    parser.add_argument("--model")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--timeout-s", type=float, default=420.0)
    parser.add_argument("--codex", default="codex")
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        if args.policy_preflight:
            if not args.rules:
                raise ValueError("--rules is required with --policy-preflight")
            summary = run_execpolicy_preflight(
                codex=args.codex,
                codex_home=pathlib.Path(args.codex_home).resolve(),
                rules=pathlib.Path(args.rules).resolve(),
            )
            print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
            return 0 if summary["passed"] else 2
        if not args.allow_live:
            raise ValueError("Model execution requires --allow-live; tests and planning are offline")
        return run(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"agent_eval_probe: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
