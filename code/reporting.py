"""Plain-text views of a case, a run and model usage. Shared by the CLI and the chat tools so they cannot drift apart."""
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from config import DATASET


def short(text: str, width: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "..."


def render_case(final: dict) -> str:
    request, intent, result = final["request"], final["intent"], final["result"]
    lines = [
        f"{request.case_id}  {request.email}  audit_at={request.audit_at:%Y-%m-%d %H:%M}Z  "
        f"reported={request.reported_status}",
        f"text    : {short(request.request_text, 150)}",
        f"intent  : {intent.intent.value} by {intent.source.value}  needs_review={intent.needs_review}",
    ]
    subject = final.get("subject")
    if subject:
        lines.append(f"identity: {subject.user_id}  {subject.status}  via {subject.via_audit_id or 'current snapshot'}")
        lines.append(f"          emails: {', '.join(sorted(subject.emails))}  | {len(subject.hashes)} hashes")
    discovery = final.get("discovery")
    if discovery:
        by_resource = defaultdict(list)
        for record in discovery.records:
            by_resource[record.resource_id or "(no resource id)"].append(record)
        lines.append(f"evidence: {len(discovery.records)} records across {len(discovery.resource_ids)} resources; "
                     f"{len(discovery.unread_files)} files unread")
        for resource in sorted(by_resource)[:20]:
            records = by_resource[resource]
            files = sorted({r.file_id for r in records})
            lines.append(f"          {resource:24s} {len(records):3d} records  {','.join(files[:4])}")
    lines.append(f"result  : {result.status}  flags={list(result.flags)}")
    return "\n".join(lines)


def render_run(finals: list[dict]) -> str:
    review = [f["request"].case_id for f in finals if f["result"].flags]
    undecided = sum(f["result"].status == "UNDECIDED" for f in finals)
    lines = [
        f"cases    : {len(finals)}",
        f"statuses : {dict(Counter(f['result'].status for f in finals))}",
        f"answered : {dict(Counter(f['intent'].source.value for f in finals))}",
        f"flagged  : {len(review)} {review[:15]}{' ...' if len(review) > 15 else ''}",
    ]
    if undecided:
        lines.append(f"note     : {undecided} deletion cases are UNDECIDED because the state, policy and planner "
                     f"stages are not built yet; output.csv is not written.")
    return "\n".join(lines)


def render_usage(summary: dict) -> str:
    lines = []
    if not summary["per_model"]:
        lines.append("  no model calls recorded yet")
    for model, t in sorted(summary["per_model"].items()):
        lines.append(f"  {model:24s} calls={t['calls']:4d} cache_hits={t['cache_hits']:4d} "
                     f"in={t['input_tokens']:8d} out={t['output_tokens']:7d} cost=${t['cost_usd']:.4f}")
    o = summary["overall"]
    lines.append(f"  {'overall':24s} calls={o['calls']:4d} cache_hits={o['cache_hits']:4d} "
                 f"in={o['input_tokens']:8d} out={o['output_tokens']:7d} cost=${o['cost_usd']:.4f}")
    return "\n".join(lines)


def load_sample_answers(path: Path = DATASET / "sample_output.csv") -> dict[str, dict]:
    """The labeled answers for the 32 sample cases. For checking our work only, never for producing predictions."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {row["case_id"]: row for row in csv.DictReader(f)}


def render_sample_comparison(final: dict, expected: dict) -> str:
    """What the labeled answer expects for a sample case, against what our discovery found."""
    steps = json.loads(expected["steps_json"])
    found = set(final["discovery"].resource_ids) if final.get("discovery") else set()
    wanted: dict[tuple, list] = {}
    for step in steps:
        wanted.setdefault((step["system_id"], step["resource_id"]), []).append(step["operation"])
    lines = [f"EVALUATION ONLY (labeled sample): expected status {expected['deletion_status']}, "
             f"our result {final['result'].status}",
             "  (MISSING = our discovery did not find a resource the labeled answer expects: a gap in this tool, "
             "not the reason the case is incomplete)"]
    for (system, resource), operations in sorted(wanted.items()):
        mark = "FOUND  " if resource in found else "MISSING"
        lines.append(f"  {mark} {system:18s} {resource:20s} {', '.join(operations)}")
    extras = sorted(found - {r for _, r in wanted})
    lines.append(f"  found but not expected: {extras or 'none'}")
    cited = sorted({s['evidence_id'] for s in steps if s['evidence_id']})
    lines.append(f"  evidence ids the labeled answer cites: {cited or 'none'}")
    return "\n".join(lines)
