"""Bounded Codex CLI reference. No service, scheduler, or acceptance engine."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid

ACTIVE = {}
LOCK = threading.Lock()
STOP = threading.Event()


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def environment():
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(name, None)
    return env


def command(args, job):
    return [str(args.exe), "--no-daemon", "--ask-for-approval", "never", "exec",
            "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", args.sandbox, "--model", args.model,
            "-c", "model_reasoning_effort=" + json.dumps(args.effort),
            "-c", "agents.enabled=false",
            "-c", "web_search=" + json.dumps(args.web_search),
            "--cd", str(job), "--output-last-message", str(job / "result.md"),
            "--json", "--color", "never", "-"]


def prompt_for(args, source_text, job):
    return (
        "Execute only the assigned task and authorized scope.\n"
        f"Write deliverables only under {job / 'artifacts'}.\n"
        "Do not change input files, formal outputs, others' drafts, or locked versions.\n"
        "Do not spawn helpers, message other tasks, create services, or change configuration.\n"
        "Do not publish, upload, purchase, or perform unrelated external writes.\n"
        "prompt.txt, request.json, *.tmp, stdout.jsonl, stderr.log, result.md are host-owned.\n"
        "Your actual identity comes from thread.started, never the coordinator's identity.\n"
        "Save meaningful partial work with its exact completed/unfinished boundary.\n"
        "A partial handoff or successful process exit does not mean acceptance.\n"
        "Report blockers honestly; do not relax permissions on your own.\n"
        "These instructions constrain scope; they are not an OS isolation guarantee.\n\n"
        "PROJECT POLICY (frozen for this invocation)\n" + args.policy_text +
        "\n\nASSIGNED TASK\n" + source_text)


def evidence(path):
    ids, completed, failure, usage = set(), False, False, []
    if path.exists():
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if event.get("type") == "thread.started":
                        ids.add(str(uuid.UUID(event["thread_id"])))
                    elif event.get("type") == "turn.completed":
                        completed = True
                        usage.append(event.get("usage", {}))
                    elif event.get("type") in ("error", "turn.failed"):
                        failure = True
                except (ValueError, KeyError, AttributeError):
                    continue
    return sorted(ids), completed, failure, usage


def stop_process(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
        proc.wait(timeout=10)


def run_one(args, source, source_text, job):
    if STOP.is_set():
        return {"status": "not_started", "prompt_source": str(source)}
    job.mkdir()
    (job / "artifacts").mkdir()
    prompt = prompt_for(args, source_text, job)
    (job / "prompt.txt").write_text(prompt, encoding="utf-8")
    record = {
        "status": "running", "coordinator": args.owner, "started_at": now(),
        "prompt_source": str(source), "prompt_sha256": sha(job / "prompt.txt"),
        "policy_sha256": args.policy_sha256, "task_id": None,
        "requested_model": args.model, "requested_effort": args.effort,
        "effective_model": "not independently verified by this adapter",
        "sandbox_requested": args.sandbox, "args": command(args, job),
        "acceptance": "unaccepted", "usage": []}
    save(job / "request.json", record)
    started, proc = time.monotonic(), None
    try:
        with (job / "stdout.jsonl").open("w", encoding="utf-8") as stdout, \
             (job / "stderr.log").open("w", encoding="utf-8") as stderr:
            with LOCK:
                if STOP.is_set():
                    raise RuntimeError("stopped before launch")
                proc = subprocess.Popen(
                    record["args"], stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                    cwd=job, env=environment(), text=True, encoding="utf-8",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt")
                ACTIVE[proc.pid] = proc
            record["pid"] = proc.pid
            save(job / "request.json", record)
            try:
                proc.communicate(prompt, timeout=args.timeout)
            except subprocess.TimeoutExpired:
                raise RuntimeError("timeout; partial files retained; no automatic retry")
            record["exit_code"] = proc.returncode
        if STOP.is_set():
            raise RuntimeError("controller interrupted; inspect partial outputs")
        ids, completed, failure, usage = evidence(job / "stdout.jsonl")
        record.update(observed_task_ids=ids, usage=usage)
        if len(ids) == 1:
            record["task_id"] = ids[0]
        result = job / "result.md"
        if (proc.returncode != 0 or failure or not completed or len(ids) != 1 or
                not result.is_file() or not result.read_text(encoding="utf-8").strip()):
            raise RuntimeError("missing valid completion/identity/result or CLI reported failure")
        record.update(status="completed", result_sha256=sha(result))
    except Exception as exc:
        record.update(status="failed", error=str(exc))
        if proc is not None:
            try:
                stop_process(proc)
            except Exception as stop_exc:
                record["stop_error"] = str(stop_exc)
    finally:
        if proc is not None:
            with LOCK:
                ACTIVE.pop(proc.pid, None)
        ids, _, _, usage = evidence(job / "stdout.jsonl")
        record.update(observed_task_ids=ids, usage=usage)
        if len(ids) == 1:
            record["task_id"] = ids[0]
        record.update(finished_at=now(), elapsed_seconds=round(time.monotonic() - started, 3))
        save(job / "request.json", record)
    return record


def probe(args):
    options = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=30, env=environment(),
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    general = subprocess.run([str(args.exe), "--help"], **options)
    execution = subprocess.run([str(args.exe), "exec", "--help"], **options)
    if (general.returncode or execution.returncode or
            any(f not in general.stdout for f in ("--no-daemon", "--ask-for-approval")) or
            any(f not in execution.stdout for f in
                ("--ignore-user-config", "--ephemeral", "--json", "--output-last-message",
                 "--sandbox", "--skip-git-repo-check"))):
        raise RuntimeError("CLI interface differs; inspect --help and adapt before execution")
    auth = subprocess.run([str(args.exe), "login", "status"], **options)
    if auth.returncode or "Logged in using ChatGPT" not in auth.stdout + auth.stderr:
        raise RuntimeError("existing ChatGPT login required; no API-key fallback")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("prompts", nargs="+", type=Path)
    ap.add_argument("--exe", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path, help="new batch directory, never reused")
    ap.add_argument("--policy", required=True, type=Path, help="short effective project rules")
    ap.add_argument("--owner", required=True, help="actual coordinator task ID or CI run identity")
    ap.add_argument("--model", required=True, help="model available to this account")
    ap.add_argument("--effort", required=True, help="effort supported by selected model")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"],
                    default="workspace-write")
    ap.add_argument("--allow-full-access", action="store_true",
                    help="acknowledge separate explicit authorization; not itself authorization")
    ap.add_argument("--web-search", choices=["disabled", "cached", "live"], default="disabled")
    ap.add_argument("--execute", action="store_true", help="run potentially billable CLI jobs")
    args = ap.parse_args(argv)
    try:
        args.exe = args.exe.resolve(strict=True)
        args.policy = args.policy.resolve(strict=True)
        args.prompts = [p.resolve(strict=True) for p in args.prompts]
        args.out = args.out.resolve()
        if not args.exe.is_file() or not args.policy.is_file():
            raise ValueError("exe and policy must be files")
        if not 1 <= len(args.prompts) <= args.workers or len(set(args.prompts)) != len(args.prompts):
            raise ValueError("distinct ready prompts only; 1 <= prompts <= workers; no hidden queue")
        if args.timeout < 1:
            raise ValueError("timeout must be positive")
        if args.out.exists():
            raise ValueError("output already exists; never overwrite or auto-resume")
        if args.sandbox == "danger-full-access" and not args.allow_full_access:
            raise ValueError("full access requires separate authorization and --allow-full-access")
        args.policy_text = args.policy.read_text(encoding="utf-8-sig")
        args.policy_sha256 = sha(args.policy)
        texts = [p.read_text(encoding="utf-8-sig") for p in args.prompts]
        if not args.policy_text.strip() or any(not text.strip() for text in texts):
            raise ValueError("empty input")
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    if not args.execute:
        print(json.dumps({
            "dry_run": True, "workers": args.workers,
            "capacity": "per invocation only; coordinator owns total concurrent capacity",
            "jobs": [{"prompt": str(p), "args": command(args, args.out / f"job-{i:02}")}
                     for i, p in enumerate(args.prompts, 1)]
        }, ensure_ascii=False, indent=2))
        return 0
    try:
        probe(args)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        ap.error(str(exc))
    STOP.clear()
    args.out.mkdir(parents=True, exist_ok=False)
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures, results, controller_error = {}, [], None
    try:
        for i, (source, source_text) in enumerate(zip(args.prompts, texts), 1):
            job = args.out / f"job-{i:02}"
            futures[pool.submit(run_one, args, source, source_text, job)] = job
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {"status": "failed", "error": str(exc)}
            notice = {k: result.get(k) for k in
                      ("status", "task_id", "requested_model", "requested_effort", "error")}
            notice["request_path"] = str(futures[future] / "request.json")
            print(json.dumps(notice), flush=True)
    except (KeyboardInterrupt, Exception) as exc:
        controller_error = type(exc).__name__ + ": " + str(exc)
        STOP.set()
        with LOCK:
            running = list(ACTIVE.values())
        for proc in running:
            try:
                stop_process(proc)
            except Exception as stop_exc:
                controller_error += "; stop error: " + str(stop_exc)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        for future, job in futures.items():
            if future.cancelled():
                row = {"status": "not_started"}
            elif future.exception() is not None:
                row = {"status": "failed", "error": str(future.exception())}
            else:
                row = future.result()
            results.append(dict(row, job_directory=str(job)))
        ids = [r.get("task_id") for r in results if r.get("task_id")]
        success = (not controller_error and len(results) == len(args.prompts) == len(set(ids))
                   and all(r["status"] == "completed" for r in results))
        save(args.out / "summary.json", {
            "coordinator": args.owner, "workers": args.workers, "finished_at": now(),
            "stopped": STOP.is_set(), "controller_error": controller_error,
            "execution_success": success, "jobs": results,
            "note": "Execution evidence only; not acceptance, coverage, or billing attribution."})
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
