import json
import re
from pathlib import Path

LINE_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s+(.*)$")
TRACE_RE = re.compile(r"\b((?:tr|rb)-[\w-]+)\b")
KV = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
KEYS_OF_INTEREST = ("resource_id", "lookup_key", "match_key", "subject_reference",
                    "correlation_key", "customer_reference")


def _pairs(text: str) -> dict:
    out = {}
    for key, raw in KV.findall(text):
        out[key] = raw[1:-1].replace('\\"', '"') if raw.startswith('"') else raw
    if "message" in out:  # cloud runtime: message="k=\"v\" k2=\"v2\""
        out.update({k: v for k, v in _pairs(out["message"]).items() if k not in out})
    return out


def read_log(path: Path):
    """Yield (ts_str, fields, line_no) for every line that mentions a resource or identifier."""
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        m = LINE_TS.match(line)
        if not m:
            continue
        ts, rest = m.groups()
        fields = {}
        if "{" in rest and rest.rstrip().endswith("}"):  # JSON-after-timestamp format
            try:
                fields.update(json.loads(rest[rest.index("{"):]))
            except ValueError:
                pass
        fields.update({k: v for k, v in _pairs(rest).items() if k not in fields})
        if "event" not in fields:  # [DEBUG] [svc] [trace] event: ...
            em = re.search(r"\]\s*([a-z_]+):", rest)
            if em:
                fields["event"] = em.group(1)
        if "event" not in fields:  # | LEVEL | service | trace | event_name | k="v" ...
            pm = re.search(r"\|\s*([a-z_]+)\s*\|\s*[A-Za-z_]+=", rest)
            if pm:
                fields["event"] = pm.group(1)
        trace = fields.get("trace_id") or fields.get("requestId") or fields.get("trace")
        if not trace:
            tm = TRACE_RE.search(rest)
            trace = tm.group(1) if tm else None
        if trace:
            fields["trace_id"] = trace  # normalized: lines of one lookup share a trace
        fields["raw"] = rest
        if any(fields.get(k) for k in KEYS_OF_INTEREST) or "@" in rest or "user_" in rest:
            yield ts, fields, n
