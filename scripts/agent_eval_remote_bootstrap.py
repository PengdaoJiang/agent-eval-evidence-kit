#!/usr/bin/env python3
"""Prepare and verify one remote Agent-eval experiment without model calls.

This helper is copied to remote as a frozen artifact.  Keeping the setup
logic in a file avoids sending a large PowerShell EncodedCommand through the
Windows OpenSSH/cmd command-line boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence


FIXED_INPUTS = {
    "probe": "agent_eval_probe.py",
    "archive": "fixtures.zip",
    "rules": "controlled.rules",
    "config": "isolated-config.toml",
}


class BootstrapError(RuntimeError):
    """Raised when the isolated remote environment cannot be prepared."""


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def first_line(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    label: str,
    preferred_prefix: str | None = None,
) -> str:
    try:
        completed = subprocess.run(
            list(command),
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError(f"{label} command failed: {type(exc).__name__}") from exc
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    if completed.returncode != 0 or not lines:
        raise BootstrapError(f"{label} command failed with exit {completed.returncode}")
    if preferred_prefix is not None:
        for line in lines:
            if line.startswith(preferred_prefix):
                return line
    return lines[0]


def safe_extract(archive: pathlib.Path, destination: pathlib.Path) -> None:
    """Replace the frozen fixture tree while rejecting archive traversal."""
    destination_parent = destination.parent.resolve()
    resolved_destination = destination.resolve()
    if resolved_destination.parent != destination_parent:
        raise BootstrapError("fixture destination is not a direct experiment child")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive, "r") as fixture_archive:
            for member in fixture_archive.infolist():
                target = (destination / member.filename).resolve()
                if target != resolved_destination and resolved_destination not in target.parents:
                    raise BootstrapError("fixture archive contains an unsafe path")
            fixture_archive.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise BootstrapError("fixture archive is invalid") from exc


def prepare_remote(
    experiment_dir: pathlib.Path,
    *,
    codex: str,
    language_mode: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    root = experiment_dir.resolve()
    if not root.is_dir():
        raise BootstrapError(f"experiment directory missing: {root}")
    inputs = {name: root / filename for name, filename in FIXED_INPUTS.items()}
    bootstrap = pathlib.Path(__file__).resolve()
    if bootstrap.parent != root:
        raise BootstrapError("bootstrap must run from the experiment directory")
    for name, path in {"bootstrap": bootstrap, **inputs}.items():
        if not path.is_file() or path.stat().st_size <= 0:
            raise BootstrapError(f"non-empty {name} artifact missing: {path}")

    fixture_root = root / "fixtures"
    codex_home = root / "codex-home"
    codex_rules_dir = codex_home / "rules"
    results_dir = root / "results"
    workdirs_dir = root / "workdirs"
    for directory in (codex_rules_dir, results_dir, workdirs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    safe_extract(inputs["archive"], fixture_root)

    runtime_environment = dict(os.environ if environ is None else environ)
    user_profile_text = runtime_environment.get("USERPROFILE", "").strip()
    if not user_profile_text:
        raise BootstrapError("USERPROFILE is not set")
    source_auth = pathlib.Path(user_profile_text) / ".codex" / "auth.json"
    if not source_auth.is_file() or source_auth.stat().st_size <= 0:
        raise BootstrapError("source auth.json missing or empty")
    destination_auth = codex_home / "auth.json"
    auth_action = "preserved"
    if not destination_auth.is_file() or destination_auth.stat().st_size <= 0:
        shutil.copy2(source_auth, destination_auth)
        auth_action = "copied"

    home_rules = codex_rules_dir / "default.rules"
    home_config = codex_home / "config.toml"
    shutil.copy2(inputs["rules"], home_rules)
    shutil.copy2(inputs["config"], home_config)

    runtime_environment["CODEX_HOME"] = str(codex_home)
    python_dir = str(pathlib.Path(sys.executable).resolve().parent)
    current_path = runtime_environment.get("PATH", "")
    runtime_environment["PATH"] = python_dir + (os.pathsep + current_path if current_path else "")
    python_on_path = shutil.which("python", path=runtime_environment["PATH"])
    if not python_on_path:
        raise BootstrapError("python is unavailable after PATH prepend")
    codex_path = shutil.which(codex, path=runtime_environment["PATH"])
    if not codex_path:
        candidate = pathlib.Path(codex)
        if not candidate.is_file():
            raise BootstrapError("Codex executable is unavailable")
        codex_path = str(candidate)

    return {
        "bootstrap": sha256_file(bootstrap),
        "probe": sha256_file(inputs["probe"]),
        "archive": sha256_file(inputs["archive"]),
        "staged_rule": sha256_file(inputs["rules"]),
        "home_rule": sha256_file(home_rules),
        "staged_config": sha256_file(inputs["config"]),
        "home_config": sha256_file(home_config),
        "auth": "present",
        "auth_action": auth_action,
        "python": first_line(
            [sys.executable, "--version"],
            environment=runtime_environment,
            label="remote Python",
        ),
        "python_on_path": first_line(
            [python_on_path, "--version"],
            environment=runtime_environment,
            label="PATH Python",
        ),
        "python_path": str(pathlib.Path(python_on_path).resolve()),
        "language_mode": language_mode,
        "codex": first_line(
            [codex_path, "--version"],
            environment=runtime_environment,
            label="isolated-home Codex",
        ),
        "login": first_line(
            [codex_path, "login", "status"],
            environment=runtime_environment,
            label="isolated-home login",
            preferred_prefix="Logged in",
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a frozen remote Codex Agent-eval environment."
    )
    parser.add_argument("--experiment-dir", required=True)
    parser.add_argument("--stage-local-auth", action="store_true")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--language-mode", required=True)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        if not args.stage_local_auth:
            raise BootstrapError("Host-local credential staging requires --stage-local-auth")
        result = prepare_remote(
            pathlib.Path(args.experiment_dir),
            codex=args.codex,
            language_mode=args.language_mode,
        )
        for key, value in result.items():
            if "\r" in value or "\n" in value:
                raise BootstrapError(f"multiline bootstrap value for {key}")
            print(f"{key}={value}")
        return 0
    except (BootstrapError, OSError, ValueError) as exc:
        print(f"agent_eval_remote_bootstrap: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
