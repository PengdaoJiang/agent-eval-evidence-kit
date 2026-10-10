"""Local result ingestion for p1_independent_review; no transport or engineering verdict."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

IDENTITY = ("request_id", "role", "batch_id", "snapshot_id", "chat_id")
FIELDS = ("id", "severity", "location", "mechanism", "trigger", "reachability",
          "minimum_verification", "status", "evidence")
CLASSES = {"conditional", "module_only", "integrated", "evidence_gap",
           "known_milestone", "telemetry"}
SEVERITY = {"critical": 0, "p0": 0, "high": 1, "p1": 1, "高": 1,
            "medium": 2, "p2": 2, "中": 2, "low": 3, "p3": 3, "低": 3, "info": 4}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _identity(value):
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or
                                         not value[k].strip() for k in IDENTITY):
        raise ValueError("missing or invalid review identity")
    return {k: value[k] for k in IDENTITY}


def _evidence(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return any(_evidence(v) for v in (value.values() if isinstance(value, dict) else value))
    return False


def _finding(row):
    if not isinstance(row, dict) or any(k not in row for k in FIELDS):
        raise ValueError("finding is missing required fields")
    if any(not isinstance(row[k], str) or not row[k].strip()
           for k in ("id", "severity", "mechanism", "status")):
        raise ValueError("invalid finding id/severity/mechanism/status")
    item = {k: row[k] for k in FIELDS}
    item["status"] = item["status"].lower()
    classification = row.get("classification", row["reachability"])
    if item["status"] in {"known", "known_milestone"}:
        classification = "known_milestone"
    item["classification"] = classification if isinstance(classification, str) and classification in CLASSES else "evidence_gap"
    if item["status"] == "closed" and not _evidence(item["evidence"]):
        raise ValueError("closed finding requires explicit evidence")
    return item


def _previous(value, role):
    if value is None:
        return None
    previous = value if isinstance(value, dict) else _json(value)
    if "result_path" in previous and "findings" not in previous:
        previous = _json(previous["result_path"])
    if _identity(previous)["role"] != role:
        raise ValueError("previous result belongs to another role")
    return previous


def _publish(path, data):
    """Publish without overwriting; even a partial retry cannot replace old bytes."""
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"immutable archive conflict: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(uuid4().hex + ".pending")
    try:
        with pending.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(pending, path)  # Atomic no-clobber publication on Windows and POSIX.
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError(f"immutable archive conflict: {path}") from None
    finally:
        pending.unlink(missing_ok=True)


def _brief(value, limit=85):
    text = " ".join((value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).split())
    text = text[:limit] + ("…" if len(text) > limit else "")
    for char in "\\`*_[]<>|":
        text = text.replace(char, "\\" + char)
    return text


def ingest_result(request_path, response_path, findings_path, inbox_root,
                  previous_result=None, coverage_path=None):
    """Ingest locally verified annotations, not a required Pro response format.

    Returns result_path/index_path/duplicate/counts. inbox_root is the shared
    passive inbox root. A response hash is bound once per role; corrections must
    not silently replace an earlier ingestion. Previous omitted findings remain
    unverified carry-forward records, including across snapshots.
    """
    request, supplied = _json(request_path), _json(findings_path)
    identity = _identity(request)
    if _identity(supplied) != identity:
        raise ValueError("stale or mismatched request/role/batch/snapshot/chat identity")
    role = {"rtl": "rtl", "arch": "arch", "architecture": "arch"}.get(identity["role"].lower())
    if role is None:
        raise ValueError("unsupported review role")
    if not isinstance(supplied.get("findings"), list):
        raise ValueError("findings must be an explicit list")
    current = [_finding(row) for row in supplied["findings"]]
    if len({row["id"] for row in current}) != len(current):
        raise ValueError("duplicate finding id")
    response = Path(response_path).read_bytes()
    if not response.strip():
        raise ValueError("empty response cannot be ingested")
    declared_coverage = supplied.get("coverage")
    if declared_coverage is not None and not isinstance(declared_coverage, dict):
        raise ValueError("coverage must be an object when supplied")
    coverage = Path(coverage_path).read_bytes() if coverage_path else _bytes(declared_coverage or {"status": "MISSING"})
    coverage_missing = not coverage.strip() if coverage_path else not declared_coverage or str(declared_coverage.get("status", "")).upper() == "MISSING"
    root = Path(inbox_root).resolve() / role
    if root.is_symlink() or (root.exists() and root.resolve().parent != Path(inbox_root).resolve()):
        raise ValueError("role inbox must not redirect outside inbox root")
    response_hash = _hash(response)
    archive = root / "_review_results" / response_hash
    result_path, index_path = archive / "result.json", root / ("review_" + response_hash + ".md")
    for path in (archive, result_path, index_path, archive / "response.original", archive / "coverage.original"):
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("archive path must not redirect outside role inbox")
    annotation_hash = _hash(_bytes({"identity": identity, "findings": current, "coverage_sha256": _hash(coverage)}))
    if result_path.exists():
        old = _json(result_path)
        if _identity(old) != identity or old["annotation_sha256"] != annotation_hash:
            raise ValueError("response hash already bound to different identity or annotations")
        if (archive / "response.original").read_bytes() != response or (archive / "coverage.original").read_bytes() != coverage:
            raise ValueError("archived source bytes changed")
        _publish(index_path, old["index_text"].encode("utf-8"))
        return {"result_path": str(result_path), "index_path": str(index_path), "duplicate": True, "counts": old["counts"]}
    previous = _previous(previous_result, identity["role"])
    prior = {row["id"]: row for row in previous["findings"]} if previous else {}
    changes = {key: [] for key in ("new", "changed", "closed", "unchanged")}
    merged = []
    for row in current:
        old = prior.pop(row["id"], None)
        same = old is not None and _finding(old) == row
        change = "unchanged" if same else "closed" if row["status"] == "closed" else "changed" if old else "unchanged" if row["classification"] == "known_milestone" else "new"
        changes[change].append(row["id"])
        merged.append({**row, "change": change, "present_in_response": True,
                       "observed_snapshot_id": identity["snapshot_id"]})
    for row in prior.values():
        changes["unchanged"].append(row["id"])
        merged.append({**row, "change": "unchanged", "present_in_response": False})
    merged.sort(key=lambda row: (SEVERITY.get(row["severity"].lower(), 99), row["id"]))
    counts = {key: len(ids) for key, ids in changes.items()}
    counts.update(total=len(merged), current=len(current), carried=len(prior),
                  known=sum(row["classification"] == "known_milestone" for row in current))
    lines = [f"# {_brief(identity['role'])} 审查结果（本地回收）", "",
             f"请求：{_brief(identity['request_id'])}；快照：{_brief(identity['snapshot_id'])}；批次：{_brief(identity['batch_id'])}",
             f"新增 {counts['new']}；变化 {counts['changed']}；明确关闭 {counts['closed']}；未变 {counts['unchanged']}；已知里程碑 {counts['known']}。",
             f"覆盖：{'MISSING' if coverage_missing else 'RECORDED（仅记录声明，不证明读完）'}；本轮未列 {counts['carried']} 项仅延续，不推断关闭或当前适用。",
             "此记录不声明工程 PASS；条件风险、模块边界、遥测、证据缺口及已知里程碑不自动升级为集成故障。", "",
             f"原文：{archive / 'response.original'}", f"覆盖：{archive / 'coverage.original'}", f"细目：{result_path}", ""]
    # A first intake shows the baseline once. Subsequent inbox messages carry
    # only real deltas; unchanged/omitted findings remain in result.json.
    visible = merged if previous is None else [row for row in merged if row["change"] != "unchanged"]
    if previous is not None:
        lines.append("本条仅列新增、变化及明确关闭项；未变/未重列项保留在细目，不重复展开。")
        if not visible:
            lines.append("本轮无问题增量；这不表示历史问题已关闭。")
        lines.append("")
    for row in visible:
        lines.append(f"- {_brief(row['id'])} [{_brief(row['severity'], 20)}/{row['classification']}/{row['change']}] "
                     f"{_brief(row['location'])} — {_brief(row['mechanism'])}；触发：{_brief(row['trigger'], 50)}；"
                     f"最小核验：{_brief(row['minimum_verification'], 60)}" + ("（本轮未列，未重新核验）" if not row["present_in_response"] else ""))
    index_text = "\n".join(lines) + "\n"
    result = {**identity, "schema": "p1.review-result.v1", "response_sha256": response_hash,
              "annotation_sha256": annotation_hash, "coverage_sha256": _hash(coverage),
              "coverage_status": "MISSING" if coverage_missing else "RECORDED",
              "response_path": str(archive / "response.original"), "coverage_path": str(archive / "coverage.original"),
              "findings": merged, "changes": changes, "counts": counts, "engineering_result": "NOT_ASSESSED",
              "previous_result_snapshot_id": previous["snapshot_id"] if previous else None, "index_text": index_text}
    _publish(archive / "response.original", response)
    _publish(archive / "coverage.original", coverage)
    _publish(result_path, _bytes(result))
    _publish(index_path, index_text.encode("utf-8"))
    return {"result_path": str(result_path), "index_path": str(index_path), "duplicate": False, "counts": counts}
