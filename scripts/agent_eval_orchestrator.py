#!/usr/bin/env python3
"""Run the fixed paired local vs remote Codex Agent-use evaluation.

The first-round design is intentionally fixed at four models, three fixtures,
and two paired repetitions.  Every model x fixture condition has exactly one
main->02 pair and one 02->main pair.  Calls are strictly serial.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from typing import Any

import agent_eval_probe as probe


SCHEMA_VERSION = 1
ORCHESTRATOR_VERSION = "1.2.0"
HOSTS = ("local", "remote")
HOST_ORDER_LABELS = {
    "local_then_remote": HOSTS,
    "remote_then_local": tuple(reversed(HOSTS)),
}
MODELS = probe.FIRST_ROUND_MODELS
FIXTURE_IDS = tuple(probe.FIXTURES)
REPETITIONS = 2
DEFAULT_SEED = 20260716
DEFAULT_EFFORT = "high"
DEFAULT_TIMEOUT_S = 420.0
DEFAULT_REMOTE_PYTHON = (
    "C:/Python/python.exe"
)
DEFAULT_REMOTE_ROOT = (
    "C:/agent-eval/experiments"
)
SAFE_REMOTE_PATH = re.compile(r"^[A-Za-z]:/[A-Za-z0-9._/-]+$")
ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_FIXTURES = ROOT / "fixtures" / "agent_eval"
SOURCE_CONTROLLED_RULES = ROOT / "agent_eval_rules" / "controlled.rules"
SOURCE_REMOTE_BOOTSTRAP = ROOT / "scripts" / "agent_eval_remote_bootstrap.py"
ISOLATED_CONFIG_TEXT = 'cli_auth_credentials_store = "file"\n'
WINDOWS_REMOTE_COMMAND_LIMIT = 8191
# Leave headroom for the Windows OpenSSH server's command-shell wrapping.
WINDOWS_REMOTE_COMMAND_BUDGET = 7600


class OrchestratorError(RuntimeError):
    """Raised for resumable experiment-administration failures."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def compact_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._") or "item"


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def require_remote_path(value: str, label: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    if " " in normalized or not SAFE_REMOTE_PATH.fullmatch(normalized):
        raise OrchestratorError(
            f"{label} must be an absolute no-space Windows path; got {value!r}"
        )
    return normalized


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def encoded_powershell(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]


def remote_powershell_command_chars(script: str) -> int:
    """Measure the command line delivered after the SSH host argument."""
    return len(subprocess.list2cmdline(encoded_powershell(script)))


def output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def command_result(
    command: list[str],
    *,
    timeout_s: float,
    cwd: pathlib.Path | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter_ns()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {
            "exit_code": completed.returncode,
            "timed_out": False,
            "duration_ms": (time.perf_counter_ns() - started) / 1_000_000,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "timed_out": True,
            "duration_ms": (time.perf_counter_ns() - started) / 1_000_000,
            "stdout": output_text(exc.stdout),
            "stderr": output_text(exc.stderr),
        }
    except OSError as exc:
        return {
            "exit_code": None,
            "timed_out": False,
            "duration_ms": (time.perf_counter_ns() - started) / 1_000_000,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }


def summarize_command(result: dict[str, Any]) -> dict[str, Any]:
    limit = 8000
    return {
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out"),
        "duration_ms": result.get("duration_ms"),
        "stdout_tail": str(result.get("stdout") or "")[-limit:],
        "stderr_tail": str(result.get("stderr") or "")[-limit:],
        **(
            {"remote_command_chars": result["remote_command_chars"]}
            if "remote_command_chars" in result
            else {}
        ),
    }


def ssh_prefix(args: argparse.Namespace) -> list[str]:
    return [
        args.ssh,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={args.connect_timeout_s}",
        args.ssh_host,
    ]


def scp_prefix(args: argparse.Namespace) -> list[str]:
    return [
        args.scp,
        "-q",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={args.connect_timeout_s}",
    ]


def remote_command(
    args: argparse.Namespace,
    script: str,
    *,
    timeout_s: float,
) -> dict[str, Any]:
    remote_argv = encoded_powershell(script)
    remote_chars = len(subprocess.list2cmdline(remote_argv))
    if remote_chars > WINDOWS_REMOTE_COMMAND_BUDGET:
        raise OrchestratorError(
            "remote PowerShell command exceeds safe Windows command-line budget: "
            f"{remote_chars} > {WINDOWS_REMOTE_COMMAND_BUDGET}; stage a helper file instead"
        )
    result = command_result(
        [*ssh_prefix(args), *remote_argv],
        timeout_s=timeout_s,
    )
    result["remote_command_chars"] = remote_chars
    return result


def make_fixture_archive(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
    os.replace(temporary, destination)


def fixture_hashes(root: pathlib.Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for fixture_id in FIXTURE_IDS:
        path = root / fixture_id
        if not path.is_dir():
            raise OrchestratorError(f"Missing fixture template: {path}")
        hashes[fixture_id] = probe.snapshot_workspace(path)["tree_sha256"]
    return hashes


def schema_hashes(root: pathlib.Path) -> dict[str, str]:
    return {
        fixture_id: sha256_file(
            root / fixture_id / str(probe.FIXTURES[fixture_id]["schema"])
        )
        for fixture_id in FIXTURE_IDS
    }


def prepare_isolated_codex_home(
    codex_home: pathlib.Path,
    controlled_rules: pathlib.Path,
    isolated_config: pathlib.Path,
    *,
    auth_source: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Create one host-private Codex home without exposing credential bytes."""
    source = auth_source or (pathlib.Path.home() / ".codex" / "auth.json")
    if not source.is_file() or source.stat().st_size <= 0:
        raise OrchestratorError(f"Non-empty source auth.json not found: {source}")
    if codex_home.exists():
        if not codex_home.is_dir() or any(codex_home.iterdir()):
            raise OrchestratorError(
                f"Refusing to replace non-empty CODEX_HOME: {codex_home}"
            )
    rules_dir = codex_home / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, codex_home / "auth.json")
    shutil.copy2(isolated_config, codex_home / "config.toml")
    shutil.copy2(controlled_rules, rules_dir / "default.rules")
    return audit_isolated_codex_home(
        codex_home,
        expected_rules_sha256=sha256_file(controlled_rules),
        expected_config_sha256=sha256_file(isolated_config),
    )


def audit_isolated_codex_home(
    codex_home: pathlib.Path,
    *,
    expected_rules_sha256: str,
    expected_config_sha256: str,
) -> dict[str, Any]:
    auth = codex_home / "auth.json"
    config = codex_home / "config.toml"
    rules = codex_home / "rules" / "default.rules"
    if not auth.is_file() or auth.stat().st_size <= 0:
        raise OrchestratorError(f"Isolated auth.json is missing or empty: {auth}")
    if not config.is_file() or sha256_file(config) != expected_config_sha256:
        raise OrchestratorError(f"Isolated config.toml hash mismatch: {config}")
    if not rules.is_file() or sha256_file(rules) != expected_rules_sha256:
        raise OrchestratorError(f"Isolated default.rules hash mismatch: {rules}")
    return {
        "codex_home": str(codex_home),
        "auth": "present",
        "config_sha256": expected_config_sha256,
        "rules_sha256": expected_rules_sha256,
    }


def build_schedule(
    *,
    seed: int,
    effort: str,
    timeout_s: float,
    experiment_id: str,
    probe_sha256: str,
    orchestrator_sha256: str,
    analyzer_sha256: str,
    fixture_tree_hashes: dict[str, str],
    fixture_schema_hashes: dict[str, str],
    fixture_archive_sha256: str,
    controlled_rules_sha256: str,
    isolated_config_sha256: str,
    remote_bootstrap_sha256: str,
    local_python: str,
    codex: str,
    ssh_host: str,
    remote_python: str,
    remote_codex: str,
    remote_root: str,
) -> dict[str, Any]:
    rng = random.Random(seed)
    raw_blocks: list[dict[str, Any]] = []
    for model in MODELS:
        for fixture_id in FIXTURE_IDS:
            orders = [list(HOSTS), list(reversed(HOSTS))]
            rng.shuffle(orders)
            for rep, host_order in enumerate(orders, start=1):
                raw_blocks.append(
                    {
                        "model": model,
                        "fixture_id": fixture_id,
                        "rep": rep,
                        "host_order": host_order,
                    }
                )
    rng.shuffle(raw_blocks)
    remote_experiment_dir = f"{remote_root}/{experiment_id}"
    blocks: list[dict[str, Any]] = []
    sequence_index = 1
    for block_index, raw in enumerate(raw_blocks, start=1):
        identity = (
            f"{experiment_id}|{raw['model']}|{raw['fixture_id']}|{raw['rep']}"
        )
        pair_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        prompt_text = probe.build_prompt(raw["fixture_id"], pair_id)
        block_id = (
            f"agent-b{block_index:03d}-{safe_slug(raw['model'])}-"
            f"{safe_slug(raw['fixture_id'])}-r{raw['rep']:02d}"
        )
        order_label = "_then_".join(raw["host_order"])
        calls: list[dict[str, Any]] = []
        for host in raw["host_order"]:
            filename = (
                f"{sequence_index:03d}_{block_id}_{safe_slug(host)}.json"
            )
            call_slug = pathlib.PurePosixPath(filename).stem
            calls.append(
                {
                    "sequence_index": sequence_index,
                    "host_label": host,
                    "host_order": order_label,
                    "filename": filename,
                    "call_slug": call_slug,
                    "local_output_relative": f"raw/agent_runs/{host}/{filename}",
                    "remote_output": (
                        f"{remote_experiment_dir}/results/{filename}"
                        if host == "remote"
                        else None
                    ),
                }
            )
            sequence_index += 1
        blocks.append(
            {
                "block_index": block_index,
                "block_id": block_id,
                "pair_id": pair_id,
                "prompt": prompt_text,
                "prompt_sha256": probe.sha256_text(prompt_text),
                "model": raw["model"],
                "fixture_id": raw["fixture_id"],
                "rep": raw["rep"],
                "host_order": raw["host_order"],
                "host_order_label": order_label,
                "calls": calls,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "codex_agent_eval_schedule",
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "created_utc": utc_now(),
        "experiment_id": experiment_id,
        "parameters": {
            "models": list(MODELS),
            "fixtures": list(FIXTURE_IDS),
            "repetitions": REPETITIONS,
            "seed": seed,
            "effort": effort,
            "timeout_s": timeout_s,
            "paired": True,
            "concurrent_model_calls": 1,
            "host_order_method": (
                "seeded randomized blocks; exactly one local_then_remote and "
                "one remote_then_local pair per model x fixture"
            ),
            "probe_sha256": probe_sha256,
            "orchestrator_sha256": orchestrator_sha256,
            "analyzer_sha256": analyzer_sha256,
            "fixture_tree_hashes": fixture_tree_hashes,
            "fixture_schema_hashes": fixture_schema_hashes,
            "fixture_archive_sha256": fixture_archive_sha256,
            "controlled_rules_sha256": controlled_rules_sha256,
            "isolated_config_sha256": isolated_config_sha256,
            "remote_bootstrap_sha256": remote_bootstrap_sha256,
            "execpolicy_case_count": len(probe.execpolicy_cases()) * 2,
            "execpolicy_argv_forms": ["inner", "outer_powershell"],
        },
        "execution": {
            "local_python": local_python,
            "codex": codex,
            "ssh_host": ssh_host,
            "remote_python": remote_python,
            "remote_codex": remote_codex,
            "remote_root": remote_root,
            "remote_experiment_dir": remote_experiment_dir,
            "remote_probe": f"{remote_experiment_dir}/agent_eval_probe.py",
            "remote_bootstrap": (
                f"{remote_experiment_dir}/agent_eval_remote_bootstrap.py"
            ),
            "remote_fixture_archive": f"{remote_experiment_dir}/fixtures.zip",
            "remote_fixture_root": f"{remote_experiment_dir}/fixtures",
            "remote_controlled_rules": f"{remote_experiment_dir}/controlled.rules",
            "remote_isolated_config": f"{remote_experiment_dir}/isolated-config.toml",
            "remote_codex_home": f"{remote_experiment_dir}/codex-home",
            "remote_codex_rules": f"{remote_experiment_dir}/codex-home/rules/default.rules",
            "local_work_root": str(
                pathlib.Path(tempfile.gettempdir())
                / "codex-agent-eval"
                / experiment_id
                / "workdirs"
            ),
            "local_codex_home": str(
                pathlib.Path(tempfile.gettempdir())
                / "codex-agent-eval"
                / experiment_id
                / "codex-home"
            ),
        },
        "block_count": len(blocks),
        "call_count": sequence_index - 1,
        "blocks": blocks,
    }


def all_calls(schedule: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (block, call)
        for block in schedule["blocks"]
        for call in block["calls"]
    ]


def expected_result_fields(
    schedule: dict[str, Any], block: dict[str, Any], call: dict[str, Any]
) -> dict[str, Any]:
    return {
        "kind": "codex_agent_eval_call",
        "host_label": call["host_label"],
        "sequence_index": call["sequence_index"],
        "block_id": block["block_id"],
        "pair_id": block["pair_id"],
        "host_order": block["host_order_label"],
        "fixture_id": block["fixture_id"],
        "requested_model": block["model"],
        "reasoning_effort": schedule["parameters"]["effort"],
        "schema_sha256": schedule["parameters"]["fixture_schema_hashes"][
            block["fixture_id"]
        ],
    }


def validate_result(
    path: pathlib.Path,
    schedule: dict[str, Any],
    block: dict[str, Any],
    call: dict[str, Any],
) -> tuple[bool, str]:
    if not path.is_file():
        return False, "result file does not exist"
    try:
        payload = read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"cannot read result: {type(exc).__name__}: {exc}"
    for key, expected in expected_result_fields(schedule, block, call).items():
        if payload.get(key) != expected:
            return False, f"{key}: expected {expected!r}, got {payload.get(key)!r}"
    observed_tree = (
        payload.get("workspace", {}).get("before", {}).get("tree_sha256")
    )
    expected_tree = schedule["parameters"]["fixture_tree_hashes"][
        block["fixture_id"]
    ]
    if observed_tree != expected_tree:
        return False, f"fixture tree hash: expected {expected_tree}, got {observed_tree}"
    if payload.get("prompt_sha256") != block["prompt_sha256"]:
        return False, "prompt hash mismatch"
    codex_home_audit = payload.get("codex_home_audit")
    if not isinstance(codex_home_audit, dict):
        return False, "missing codex_home_audit"
    if codex_home_audit.get("auth_present") is not True:
        return False, "isolated auth.json was not present"
    if (
        codex_home_audit.get("rules_sha256")
        != schedule["parameters"]["controlled_rules_sha256"]
    ):
        return False, "isolated rules hash mismatch"
    if (
        codex_home_audit.get("config_sha256")
        != schedule["parameters"]["isolated_config_sha256"]
    ):
        return False, "isolated config hash mismatch"
    command = payload.get("command")
    if not isinstance(command, list):
        return False, "collector did not record the Codex command argv"
    if "--ignore-user-config" not in command:
        return False, "Codex command did not isolate user config"
    if "--ignore-rules" in command:
        return False, "Codex command disabled controlled rules"
    return True, "ok"


def probe_arguments(
    schedule: dict[str, Any],
    block: dict[str, Any],
    call: dict[str, Any],
    *,
    fixture_root: str,
    output: str,
    codex: str,
    codex_home: str,
) -> list[str]:
    return [
        "--allow-live",
        "--fixture-id",
        block["fixture_id"],
        "--fixture-root",
        fixture_root,
        "--output",
        output,
        "--host-label",
        call["host_label"],
        "--sequence-index",
        str(call["sequence_index"]),
        "--block-id",
        block["block_id"],
        "--pair-id",
        block["pair_id"],
        "--host-order",
        block["host_order_label"],
        "--model",
        block["model"],
        "--effort",
        schedule["parameters"]["effort"],
        "--timeout-s",
        str(schedule["parameters"]["timeout_s"]),
        "--codex",
        codex,
        "--codex-home",
        codex_home,
    ]


def new_manifest(schedule_path: pathlib.Path, schedule: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "codex_agent_eval_manifest",
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "experiment_id": schedule["experiment_id"],
        "schedule_sha256": sha256_file(schedule_path),
        "created_utc": utc_now(),
        "updated_utc": utc_now(),
        "state": "prepared",
        "call_count": schedule["call_count"],
        "completed_call_count": 0,
        "failed_call_count": 0,
        "local_preflights": [],
        "remote_staging": [],
        "sessions": [],
        "calls": {
            str(call["sequence_index"]): {"status": "pending", "attempts": []}
            for _, call in all_calls(schedule)
        },
    }


def initialize(
    args: argparse.Namespace,
) -> tuple[
    pathlib.Path,
    dict[str, Any],
    pathlib.Path,
    pathlib.Path,
    pathlib.Path,
    pathlib.Path,
    dict[str, Any],
]:
    run_dir = pathlib.Path(args.run_dir).resolve()
    schedule_path = run_dir / "schedule.json"
    manifest_path = run_dir / "manifest.json"
    if args.resume:
        if not schedule_path.is_file():
            raise OrchestratorError(f"Cannot resume without {schedule_path}")
        schedule = read_json(schedule_path)
        manifest = read_json(manifest_path)
        if manifest.get("schedule_sha256") != sha256_file(schedule_path):
            raise OrchestratorError("manifest does not match schedule")
        parameters = schedule["parameters"]
        for label, actual, expected in (
            ("seed", args.seed, parameters["seed"]),
            ("effort", args.effort, parameters["effort"]),
            ("timeout", args.timeout_s, parameters["timeout_s"]),
            ("ssh host", args.ssh_host, schedule["execution"]["ssh_host"]),
        ):
            if actual != expected:
                raise OrchestratorError(
                    f"Resume {label} {actual!r} does not match {expected!r}"
                )
        probe_snapshot = run_dir / "artifacts" / "agent_eval_probe.py"
        orchestrator_snapshot = run_dir / "artifacts" / "agent_eval_orchestrator.py"
        analyzer_snapshot = run_dir / "artifacts" / "analyze_agent_eval.py"
        controlled_rules_snapshot = run_dir / "artifacts" / "controlled.rules"
        isolated_config_snapshot = run_dir / "artifacts" / "isolated-config.toml"
        remote_bootstrap_snapshot = (
            run_dir / "artifacts" / "agent_eval_remote_bootstrap.py"
        )
        fixtures_snapshot = run_dir / "artifacts" / "fixtures"
        archive = run_dir / "artifacts" / "fixtures.zip"
        if sha256_file(probe_snapshot) != parameters["probe_sha256"]:
            raise OrchestratorError("probe snapshot hash mismatch")
        if sha256_file(orchestrator_snapshot) != parameters["orchestrator_sha256"]:
            raise OrchestratorError("orchestrator snapshot hash mismatch")
        if sha256_file(analyzer_snapshot) != parameters["analyzer_sha256"]:
            raise OrchestratorError("analyzer snapshot hash mismatch")
        if (
            sha256_file(controlled_rules_snapshot)
            != parameters["controlled_rules_sha256"]
        ):
            raise OrchestratorError("controlled rules snapshot hash mismatch")
        if (
            sha256_file(isolated_config_snapshot)
            != parameters["isolated_config_sha256"]
        ):
            raise OrchestratorError("isolated config snapshot hash mismatch")
        if (
            sha256_file(remote_bootstrap_snapshot)
            != parameters["remote_bootstrap_sha256"]
        ):
            raise OrchestratorError("remote bootstrap snapshot hash mismatch")
        if sha256_file(pathlib.Path(__file__).resolve()) != parameters["orchestrator_sha256"]:
            raise OrchestratorError(
                "current orchestrator differs from the frozen run snapshot; "
                "resume with artifacts/agent_eval_orchestrator.py"
            )
        if sha256_file(pathlib.Path(probe.__file__).resolve()) != parameters["probe_sha256"]:
            raise OrchestratorError(
                "current probe differs from the frozen run snapshot; resume with "
                "artifacts/agent_eval_orchestrator.py"
            )
        if len(probe.execpolicy_cases()) * 2 != parameters["execpolicy_case_count"]:
            raise OrchestratorError("frozen execpolicy case count mismatch")
        if sha256_file(archive) != parameters["fixture_archive_sha256"]:
            raise OrchestratorError("fixture archive hash mismatch")
        if fixture_hashes(fixtures_snapshot) != parameters["fixture_tree_hashes"]:
            raise OrchestratorError("fixture snapshot tree hash mismatch")
        if list(MODELS) != parameters["models"]:
            raise OrchestratorError("Resume models do not match the frozen schedule")
        if not getattr(args, "prepare_only", False):
            isolated_home = pathlib.Path(schedule["execution"]["local_codex_home"])
            if not isolated_home.exists():
                prepare_isolated_codex_home(
                    isolated_home, controlled_rules_snapshot, isolated_config_snapshot
                )
            audit_isolated_codex_home(
                isolated_home,
                expected_rules_sha256=parameters["controlled_rules_sha256"],
                expected_config_sha256=parameters["isolated_config_sha256"],
            )
        return (
            run_dir,
            schedule,
            probe_snapshot,
            archive,
            controlled_rules_snapshot,
            isolated_config_snapshot,
            manifest,
        )

    if schedule_path.exists() or manifest_path.exists():
        raise OrchestratorError(
            f"Run directory already initialized: {run_dir}; use --resume"
        )
    current_probe = pathlib.Path(probe.__file__).resolve()
    current_orchestrator = pathlib.Path(__file__).resolve()
    current_analyzer = current_orchestrator.with_name("analyze_agent_eval.py")
    if not current_analyzer.is_file():
        raise OrchestratorError(f"Analyzer source not found: {current_analyzer}")
    if not SOURCE_FIXTURES.is_dir():
        raise OrchestratorError(f"Fixture source directory not found: {SOURCE_FIXTURES}")
    if not SOURCE_CONTROLLED_RULES.is_file():
        raise OrchestratorError(
            f"Controlled rules source not found: {SOURCE_CONTROLLED_RULES}"
        )
    if not SOURCE_REMOTE_BOOTSTRAP.is_file():
        raise OrchestratorError(
            f"Remote bootstrap source not found: {SOURCE_REMOTE_BOOTSTRAP}"
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    probe_snapshot = artifact_dir / "agent_eval_probe.py"
    shutil.copy2(current_probe, probe_snapshot)
    orchestrator_snapshot = artifact_dir / "agent_eval_orchestrator.py"
    shutil.copy2(current_orchestrator, orchestrator_snapshot)
    analyzer_snapshot = artifact_dir / "analyze_agent_eval.py"
    shutil.copy2(current_analyzer, analyzer_snapshot)
    controlled_rules_snapshot = artifact_dir / "controlled.rules"
    shutil.copy2(SOURCE_CONTROLLED_RULES, controlled_rules_snapshot)
    isolated_config_snapshot = artifact_dir / "isolated-config.toml"
    isolated_config_snapshot.write_text(ISOLATED_CONFIG_TEXT, encoding="utf-8")
    remote_bootstrap_snapshot = artifact_dir / "agent_eval_remote_bootstrap.py"
    shutil.copy2(SOURCE_REMOTE_BOOTSTRAP, remote_bootstrap_snapshot)
    fixtures_snapshot = artifact_dir / "fixtures"
    shutil.copytree(SOURCE_FIXTURES, fixtures_snapshot)
    archive = artifact_dir / "fixtures.zip"
    make_fixture_archive(fixtures_snapshot, archive)
    remote_root = require_remote_path(args.remote_root, "--remote-root")
    experiment_id = f"agent-eval-{compact_utc_now()}-{uuid.uuid4().hex[:8]}"
    schedule = build_schedule(
        seed=args.seed,
        effort=args.effort,
        timeout_s=args.timeout_s,
        experiment_id=experiment_id,
        probe_sha256=sha256_file(probe_snapshot),
        orchestrator_sha256=sha256_file(orchestrator_snapshot),
        analyzer_sha256=sha256_file(analyzer_snapshot),
        fixture_tree_hashes=fixture_hashes(fixtures_snapshot),
        fixture_schema_hashes=schema_hashes(fixtures_snapshot),
        fixture_archive_sha256=sha256_file(archive),
        controlled_rules_sha256=sha256_file(controlled_rules_snapshot),
        isolated_config_sha256=sha256_file(isolated_config_snapshot),
        remote_bootstrap_sha256=sha256_file(remote_bootstrap_snapshot),
        local_python=str(pathlib.Path(args.local_python).resolve()),
        codex=args.codex,
        ssh_host=args.ssh_host,
        remote_python=require_remote_path(args.remote_python, "--remote-python"),
        remote_codex=args.remote_codex,
        remote_root=remote_root,
    )
    if not getattr(args, "prepare_only", False):
        prepare_isolated_codex_home(
            pathlib.Path(schedule["execution"]["local_codex_home"]),
            controlled_rules_snapshot,
            isolated_config_snapshot,
        )
    atomic_json(schedule_path, schedule)
    manifest = new_manifest(schedule_path, schedule)
    atomic_json(manifest_path, manifest)
    return (
        run_dir,
        schedule,
        probe_snapshot,
        archive,
        controlled_rules_snapshot,
        isolated_config_snapshot,
        manifest,
    )


def preflight_local(schedule: dict[str, Any]) -> dict[str, Any]:
    """Validate main's isolated home and policy without starting a model call."""
    execution = schedule["execution"]
    parameters = schedule["parameters"]
    codex_home = pathlib.Path(execution["local_codex_home"])
    home_audit = audit_isolated_codex_home(
        codex_home,
        expected_rules_sha256=parameters["controlled_rules_sha256"],
        expected_config_sha256=parameters["isolated_config_sha256"],
    )
    environment = probe.codex_environment(codex_home)
    python_version = command_result(
        [execution["local_python"], "--version"],
        timeout_s=20.0,
        environment=environment,
    )
    codex_version = command_result(
        [execution["codex"], "--version"],
        timeout_s=20.0,
        environment=environment,
    )
    login_status = command_result(
        [execution["codex"], "login", "status"],
        timeout_s=20.0,
        environment=environment,
    )
    python_text = (
        str(python_version.get("stdout") or "")
        + str(python_version.get("stderr") or "")
    ).strip()
    codex_text = (
        str(codex_version.get("stdout") or "")
        + str(codex_version.get("stderr") or "")
    ).strip()
    login_text = (
        str(login_status.get("stdout") or "")
        + str(login_status.get("stderr") or "")
    ).strip()
    if python_version.get("exit_code") != 0 or not python_text.startswith("Python "):
        raise OrchestratorError("local Python preflight failed")
    if codex_version.get("exit_code") != 0 or not codex_text.startswith("codex-cli "):
        raise OrchestratorError("local isolated-home Codex preflight failed")
    if login_status.get("exit_code") != 0 or not login_text.startswith("Logged in"):
        raise OrchestratorError("local isolated-home login preflight failed")
    rules = codex_home / "rules" / "default.rules"
    policy = probe.run_execpolicy_preflight(
        codex=execution["codex"],
        codex_home=codex_home,
        rules=rules,
    )
    if not policy.get("passed"):
        raise OrchestratorError("local controlled execpolicy preflight failed")
    if policy.get("rules_sha256") != parameters["controlled_rules_sha256"]:
        raise OrchestratorError("local execpolicy preflight used unexpected rules")
    if policy.get("case_count") != parameters["execpolicy_case_count"]:
        raise OrchestratorError("local execpolicy preflight case count mismatch")
    return {
        "checked_utc": utc_now(),
        "home": home_audit,
        "python": summarize_command(python_version),
        "codex": summarize_command(codex_version),
        "login": summarize_command(login_status),
        "policy": policy,
    }


def parse_policy_preflight_output(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(output.strip())
    except json.JSONDecodeError as exc:
        raise OrchestratorError(
            f"remote execpolicy preflight did not emit JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise OrchestratorError("remote execpolicy preflight emitted non-object JSON")
    return payload


def stage_remote(
    args: argparse.Namespace,
    schedule: dict[str, Any],
    probe_snapshot: pathlib.Path,
    archive: pathlib.Path,
    controlled_rules_snapshot: pathlib.Path,
    isolated_config_snapshot: pathlib.Path,
) -> dict[str, Any]:
    execution = schedule["execution"]
    remote_dir = execution["remote_experiment_dir"]
    remote_bootstrap_snapshot = probe_snapshot.with_name(
        "agent_eval_remote_bootstrap.py"
    )
    if not remote_bootstrap_snapshot.is_file():
        raise OrchestratorError(
            f"Frozen remote bootstrap not found: {remote_bootstrap_snapshot}"
        )
    if (
        sha256_file(remote_bootstrap_snapshot)
        != schedule["parameters"]["remote_bootstrap_sha256"]
    ):
        raise OrchestratorError("frozen remote bootstrap hash mismatch")
    mkdir = remote_command(
        args,
        "$ErrorActionPreference='Stop'; "
        f"New-Item -ItemType Directory -Force -Path {ps_quote(remote_dir)} | Out-Null; "
        f"New-Item -ItemType Directory -Force -Path {ps_quote(remote_dir + '/results')} | Out-Null; "
        f"New-Item -ItemType Directory -Force -Path {ps_quote(remote_dir + '/workdirs')} | Out-Null; "
        f"New-Item -ItemType Directory -Force -Path {ps_quote(execution['remote_codex_home'] + '/rules')} | Out-Null",
        timeout_s=max(45.0, args.connect_timeout_s + 30.0),
    )
    if mkdir["exit_code"] != 0:
        raise OrchestratorError("could not create remote Agent eval directory")
    copied: list[dict[str, Any]] = []
    for local_path, remote_path in (
        (probe_snapshot, execution["remote_probe"]),
        (remote_bootstrap_snapshot, execution["remote_bootstrap"]),
        (archive, execution["remote_fixture_archive"]),
        (controlled_rules_snapshot, execution["remote_controlled_rules"]),
        (isolated_config_snapshot, execution["remote_isolated_config"]),
    ):
        result = command_result(
            [*scp_prefix(args), str(local_path), f"{args.ssh_host}:{remote_path}"],
            timeout_s=max(90.0, args.connect_timeout_s + 60.0),
        )
        copied.append(summarize_command(result))
        if result["exit_code"] != 0:
            raise OrchestratorError(f"could not stage {local_path.name} on remote")
    bootstrap_arguments = [
        execution["remote_python"],
        execution["remote_bootstrap"],
        "--stage-local-auth",
        "--experiment-dir",
        remote_dir,
        "--codex",
        execution["remote_codex"],
    ]
    bootstrap_invocation = " ".join(ps_quote(value) for value in bootstrap_arguments)
    bootstrap_script = (
        "$ErrorActionPreference='Stop'; "
        "$languageMode=$ExecutionContext.SessionState.LanguageMode; "
        f"& {bootstrap_invocation} --language-mode $languageMode; "
        "$bootstrapExit=$LASTEXITCODE; "
        "if ($null -eq $bootstrapExit) {$bootstrapExit=1}; exit $bootstrapExit"
    )
    bootstrapped = remote_command(
        args,
        bootstrap_script,
        timeout_s=max(120.0, args.connect_timeout_s + 90.0),
    )
    if bootstrapped["exit_code"] != 0:
        raise OrchestratorError("remote staged bootstrap or version preflight failed")
    lines = [
        line.strip()
        for line in str(bootstrapped.get("stdout") or "").splitlines()
        if line.strip()
    ]
    preflight: dict[str, str] = {}
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            if key in {
                "bootstrap",
                "probe",
                "archive",
                "staged_rule",
                "home_rule",
                "staged_config",
                "home_config",
                "auth",
                "auth_action",
                "python",
                "python_on_path",
                "python_path",
                "language_mode",
                "codex",
                "login",
            }:
                preflight[key] = value.strip()
    expected_preflight_keys = {
        "bootstrap",
        "probe",
        "archive",
        "staged_rule",
        "home_rule",
        "staged_config",
        "home_config",
        "auth",
        "auth_action",
        "python",
        "python_on_path",
        "python_path",
        "language_mode",
        "codex",
        "login",
    }
    if set(preflight) != expected_preflight_keys:
        raise OrchestratorError(
            "remote preflight key=value output incomplete: "
            + ", ".join(sorted(preflight))
        )
    if (
        preflight.get("bootstrap")
        != schedule["parameters"]["remote_bootstrap_sha256"]
    ):
        raise OrchestratorError("remote bootstrap hash mismatch")
    if preflight.get("probe") != schedule["parameters"]["probe_sha256"]:
        raise OrchestratorError("remote probe hash mismatch")
    if preflight.get("archive") != schedule["parameters"]["fixture_archive_sha256"]:
        raise OrchestratorError("remote fixture archive hash mismatch")
    expected_rule_hash = schedule["parameters"]["controlled_rules_sha256"]
    if any(
        preflight.get(key) != expected_rule_hash
        for key in ("staged_rule", "home_rule")
    ):
        raise OrchestratorError("remote controlled rules hash mismatch")
    expected_config_hash = schedule["parameters"]["isolated_config_sha256"]
    if any(
        preflight.get(key) != expected_config_hash
        for key in ("staged_config", "home_config")
    ):
        raise OrchestratorError("remote isolated config hash mismatch")
    if preflight.get("auth") != "present":
        raise OrchestratorError("remote isolated auth.json preflight failed")
    if preflight.get("auth_action") not in {"copied", "preserved"}:
        raise OrchestratorError("remote isolated auth.json staging state is invalid")
    if not preflight.get("python_on_path", "").startswith("Python "):
        raise OrchestratorError("remote PATH-prepended python command is not executable")
    if not preflight.get("python", "").startswith("Python "):
        raise OrchestratorError("remote explicit Python command is not executable")
    if not preflight.get("codex", "").startswith("codex-cli "):
        raise OrchestratorError("remote isolated-home Codex command is not executable")
    if not preflight.get("login", "").startswith("Logged in"):
        raise OrchestratorError("remote isolated-home login preflight failed")
    policy_arguments = [
        execution["remote_python"],
        execution["remote_probe"],
        "--policy-preflight",
        "--codex-home",
        execution["remote_codex_home"],
        "--rules",
        execution["remote_codex_rules"],
        "--codex",
        execution["remote_codex"],
    ]
    policy_invocation = " ".join(ps_quote(value) for value in policy_arguments)
    policy_command = remote_command(
        args,
        "$ErrorActionPreference='Stop'; "
        f"$env:CODEX_HOME={ps_quote(execution['remote_codex_home'])}; "
        f"$pythonDir=Split-Path -Parent {ps_quote(execution['remote_python'])}; "
        "$env:Path=$pythonDir + ';' + $env:Path; "
        f"& {policy_invocation}; "
        "$policyExit=$LASTEXITCODE; "
        "if ($null -eq $policyExit) {$policyExit=1}; exit $policyExit",
        timeout_s=max(180.0, args.connect_timeout_s + 150.0),
    )
    if policy_command["exit_code"] != 0:
        raise OrchestratorError("remote controlled execpolicy preflight failed")
    policy = parse_policy_preflight_output(str(policy_command.get("stdout") or ""))
    if not policy.get("passed"):
        raise OrchestratorError("remote controlled execpolicy cases did not pass")
    if policy.get("rules_sha256") != expected_rule_hash:
        raise OrchestratorError("remote execpolicy preflight used unexpected rules")
    expected_case_count = schedule["parameters"]["execpolicy_case_count"]
    if policy.get("case_count") != expected_case_count:
        raise OrchestratorError("remote execpolicy preflight case count mismatch")
    return {
        "staged_utc": utc_now(),
        "mkdir": summarize_command(mkdir),
        "copies": copied,
        "bootstrap_transport": "staged_python_helper",
        "expand_and_verify": summarize_command(bootstrapped),
        "preflight": preflight,
        "policy_command": summarize_command(policy_command),
        "policy": policy,
    }


def copy_remote_result(
    args: argparse.Namespace,
    schedule: dict[str, Any],
    block: dict[str, Any],
    call: dict[str, Any],
    run_dir: pathlib.Path,
    attempt_number: int,
) -> dict[str, Any]:
    target = run_dir / call["local_output_relative"]
    attempt_path = (
        run_dir
        / "raw"
        / "agent_attempts"
        / "remote"
        / f"{call['call_slug']}.attempt{attempt_number:02d}.json"
    )
    attempt_path.parent.mkdir(parents=True, exist_ok=True)
    partial = attempt_path.with_name(attempt_path.name + ".part")
    if partial.exists():
        partial.unlink()
    remote_path = remote_attempt_output(call, attempt_number)
    copied = command_result(
        [
            *scp_prefix(args),
            f"{args.ssh_host}:{remote_path}",
            str(partial),
        ],
        timeout_s=max(90.0, args.connect_timeout_s + 60.0),
    )
    if copied["exit_code"] == 0 and partial.is_file():
        os.replace(partial, attempt_path)
    valid, reason = validate_result(attempt_path, schedule, block, call)
    if valid:
        promote_result(attempt_path, target)
    return {
        "remote_output": remote_path,
        "local_attempt_result": str(attempt_path),
        "copy": summarize_command(copied),
        "result_valid": valid,
        "validation": reason,
        "result_sha256": sha256_file(target) if valid else None,
    }


def remote_attempt_output(call: dict[str, Any], attempt_number: int) -> str:
    base = pathlib.PurePosixPath(str(call["remote_output"]))
    return str(base.with_name(f"{base.stem}.attempt{attempt_number:02d}{base.suffix}"))


def promote_result(source: pathlib.Path, target: pathlib.Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".promote")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def run_local_call(
    args: argparse.Namespace,
    schedule: dict[str, Any],
    block: dict[str, Any],
    call: dict[str, Any],
    run_dir: pathlib.Path,
    probe_snapshot: pathlib.Path,
    attempt_number: int,
) -> dict[str, Any]:
    fixture_source = run_dir / "artifacts" / "fixtures" / block["fixture_id"]
    workdir = (
        pathlib.Path(schedule["execution"]["local_work_root"])
        / "local"
        / f"{call['call_slug']}-attempt{attempt_number:02d}"
    )
    shutil.copytree(fixture_source, workdir)
    output = (
        run_dir
        / "raw"
        / "agent_attempts"
        / "local"
        / f"{call['call_slug']}.attempt{attempt_number:02d}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        schedule["execution"]["local_python"],
        str(probe_snapshot),
        *probe_arguments(
            schedule,
            block,
            call,
            fixture_root=str(workdir),
            output=str(output),
            codex=schedule["execution"]["codex"],
            codex_home=schedule["execution"]["local_codex_home"],
        ),
    ]
    collected = command_result(
        command,
        timeout_s=schedule["parameters"]["timeout_s"] + args.timeout_padding_s,
    )
    valid, reason = validate_result(output, schedule, block, call)
    final_output = run_dir / call["local_output_relative"]
    if valid:
        promote_result(output, final_output)
    return {
        "workdir": str(workdir),
        "collector": summarize_command(collected),
        "result_valid": valid,
        "validation": reason,
        "attempt_result": str(output),
        "result_sha256": sha256_file(final_output) if valid else None,
    }


def run_remote_call(
    args: argparse.Namespace,
    schedule: dict[str, Any],
    block: dict[str, Any],
    call: dict[str, Any],
    run_dir: pathlib.Path,
    attempt_number: int,
) -> dict[str, Any]:
    execution = schedule["execution"]
    template = f"{execution['remote_fixture_root']}/{block['fixture_id']}"
    workdir = (
        f"{execution['remote_experiment_dir']}/workdirs/"
        f"{call['call_slug']}-attempt{attempt_number:02d}"
    )
    remote_output = remote_attempt_output(call, attempt_number)
    remote_args = probe_arguments(
        schedule,
        block,
        call,
        fixture_root=workdir,
        output=remote_output,
        codex=execution["remote_codex"],
        codex_home=execution["remote_codex_home"],
    )
    invocation = " ".join(
        ps_quote(value)
        for value in [execution["remote_python"], execution["remote_probe"], *remote_args]
    )
    script = (
        "$ErrorActionPreference='Stop'; "
        f"New-Item -ItemType Directory -Path {ps_quote(workdir)} | Out-Null; "
        f"Copy-Item -Path (Join-Path {ps_quote(template)} '*') -Destination {ps_quote(workdir)} -Recurse -Force; "
        f"& {invocation}; "
        "$collectorExit=$LASTEXITCODE; "
        "if ($null -eq $collectorExit) {$collectorExit=1}; exit $collectorExit"
    )
    foreground = remote_command(
        args,
        script,
        timeout_s=schedule["parameters"]["timeout_s"] + args.timeout_padding_s,
    )
    copied = copy_remote_result(
        args, schedule, block, call, run_dir, attempt_number
    )
    return {
        "remote_workdir": workdir,
        "remote_attempt_output": remote_output,
        "collector_over_foreground_ssh": summarize_command(foreground),
        "copy_after_collector": copied,
        "result_valid": copied["result_valid"],
        "validation": copied["validation"],
        "result_sha256": copied.get("result_sha256"),
    }


def execute(args: argparse.Namespace) -> int:
    if not args.prepare_only:
        if not getattr(args, "allow_live", False):
            raise OrchestratorError(
                "Use --prepare-only for offline planning. Live staging, including "
                "host-local credential copies, requires --allow-live."
            )
        if any(model.startswith("config-") for model in MODELS):
            raise OrchestratorError("Supply four accessible model IDs with --models")
    (
        run_dir,
        schedule,
        probe_snapshot,
        archive,
        controlled_rules_snapshot,
        isolated_config_snapshot,
        manifest,
    ) = initialize(args)
    manifest_path = run_dir / "manifest.json"
    if args.prepare_only:
        manifest["state"] = "prepared"
        manifest["updated_utc"] = utc_now()
        atomic_json(manifest_path, manifest)
        print(f"Prepared {schedule['call_count']} calls in {run_dir}")
        return 0

    local_preflight = preflight_local(schedule)
    manifest.setdefault("local_preflights", []).append(local_preflight)
    session = {"started_utc": utc_now(), "resume": bool(args.resume), "argv": sys.argv}
    manifest.setdefault("sessions", []).append(session)
    manifest["state"] = "preflight"
    manifest["updated_utc"] = utc_now()
    atomic_json(manifest_path, manifest)
    staging = stage_remote(
        args,
        schedule,
        probe_snapshot,
        archive,
        controlled_rules_snapshot,
        isolated_config_snapshot,
    )
    manifest.setdefault("remote_staging", []).append(staging)
    if args.preflight_only:
        manifest["state"] = "preflight_passed"
        manifest["updated_utc"] = utc_now()
        session["completed_utc"] = utc_now()
        atomic_json(manifest_path, manifest)
        print(
            f"Remote preflight passed for {schedule['call_count']} planned calls; "
            "no model calls were started."
        )
        return 0
    manifest["state"] = "running"
    atomic_json(manifest_path, manifest)

    interrupted = False
    try:
        for block, call in all_calls(schedule):
            key = str(call["sequence_index"])
            state = manifest["calls"][key]
            local_output = run_dir / call["local_output_relative"]
            valid, reason = validate_result(local_output, schedule, block, call)
            if valid:
                manifest["calls"][key]["status"] = "completed"
                continue
            if local_output.exists():
                raise OrchestratorError(
                    f"Refusing to overwrite invalid result {local_output}: {reason}"
                )
            if (
                args.resume
                and call["host_label"] == "remote"
                and state.get("attempts")
            ):
                recovered = copy_remote_result(
                    args,
                    schedule,
                    block,
                    call,
                    run_dir,
                    len(state["attempts"]),
                )
                if recovered["result_valid"]:
                    manifest["calls"][key].update(
                        {
                            "status": "completed",
                            "recovered_remote_result_utc": utc_now(),
                            "recovery": recovered,
                        }
                    )
                    atomic_json(manifest_path, manifest)
                    print(f"[{call['sequence_index']:03d}/048] recovered remote result")
                    continue
            if args.resume and state.get("status") in {"running", "ambiguous_running"}:
                if not args.retry_ambiguous_running:
                    state["status"] = "ambiguous_running"
                    state["ambiguity_preserved_utc"] = utc_now()
                    atomic_json(manifest_path, manifest)
                    print(
                        f"[{call['sequence_index']:03d}/048] preserve ambiguous "
                        "in-flight call; wait/recover or explicitly use "
                        "--retry-ambiguous-running",
                        flush=True,
                    )
                    continue
            if (
                args.resume
                and state.get("status") == "failed"
                and not args.retry_administrative_failures
            ):
                print(f"[{call['sequence_index']:03d}/048] preserve administrative failure")
                continue
            attempt_number = len(state.get("attempts", [])) + 1
            attempt: dict[str, Any] = {
                "attempt_number": attempt_number,
                "started_utc": utc_now(),
            }
            state["status"] = "running"
            state.setdefault("attempts", []).append(attempt)
            atomic_json(manifest_path, manifest)
            print(
                f"[{call['sequence_index']:03d}/048] {call['host_label']} "
                f"{block['model']} {block['fixture_id']} ({block['host_order_label']})",
                flush=True,
            )
            if call["host_label"] == "local":
                outcome = run_local_call(
                    args,
                    schedule,
                    block,
                    call,
                    run_dir,
                    probe_snapshot,
                    attempt_number,
                )
            else:
                outcome = run_remote_call(
                    args,
                    schedule,
                    block,
                    call,
                    run_dir,
                    attempt_number,
                )
            attempt["completed_utc"] = utc_now()
            attempt["outcome"] = outcome
            state["status"] = "completed" if outcome["result_valid"] else "failed"
            if outcome["result_valid"]:
                state["result_sha256"] = outcome["result_sha256"]
            atomic_json(manifest_path, manifest)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        completed = sum(
            value.get("status") == "completed" for value in manifest["calls"].values()
        )
        failed = sum(
            value.get("status") == "failed" for value in manifest["calls"].values()
        )
        manifest["completed_call_count"] = completed
        manifest["failed_call_count"] = failed
        manifest["updated_utc"] = utc_now()
        session["completed_utc"] = utc_now()
        if interrupted:
            manifest["state"] = "interrupted"
        elif completed == schedule["call_count"]:
            manifest["state"] = "completed"
            manifest["completed_utc"] = utc_now()
        else:
            manifest["state"] = "partial"
        atomic_json(manifest_path, manifest)
    if interrupted:
        return 130
    return 0 if manifest["state"] == "completed" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs=4, default=list(MODELS), metavar="MODEL")
    parser.add_argument(
        "--allow-live", action="store_true",
        help="Allow credential staging, SSH and model execution (may consume quota)",
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Create and validate the frozen 48-call schedule without SSH or model calls",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Freeze the schedule and verify remote staging/hashes/versions, then exit before calls",
    )
    parser.add_argument("--retry-administrative-failures", action="store_true")
    parser.add_argument(
        "--retry-ambiguous-running",
        action="store_true",
        help="Explicitly create a new attempt after an unrecovered in-flight call",
    )
    parser.add_argument("--ssh-host", default="remote")
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--scp", default="scp")
    parser.add_argument("--connect-timeout-s", type=int, default=15)
    parser.add_argument("--timeout-padding-s", type=float, default=180.0)
    parser.add_argument("--local-python", default=sys.executable)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--remote-python", default=DEFAULT_REMOTE_PYTHON)
    parser.add_argument("--remote-codex", default="codex")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    return parser


def main() -> int:
    global MODELS
    try:
        args = build_parser().parse_args()
        MODELS = tuple(args.models)
        if len(set(MODELS)) != 4:
            raise OrchestratorError("The paired design requires four distinct model IDs")
        return execute(args)
    except (OrchestratorError, OSError, json.JSONDecodeError) as exc:
        print(f"agent_eval_orchestrator: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
