from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config import DATASET, parse_ts
from loaders import load_manifest

from .image_reader import cached_vision
from .json_reader import read_json
from .log_reader import read_log
from .model import EvidenceRecord, tokens_of
from .pdf_reader import read_pdf
from .sql_reader import read_sql
from .text_reader import read_text


def _ts(value) -> datetime | None:
    try:
        return parse_ts(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


class EvidenceStore:
    def __init__(self, vision=cached_vision):
        self._vision = vision                      # (path, file_id) -> records; the default never calls a model
        self.records: list[EvidenceRecord] = []
        self.by_resource = defaultdict(list)
        self.by_token = defaultdict(list)
        self.by_trace = defaultdict(list)  # (file_id, trace_id) -> log lines of one lookup
        self.unread: dict[str, str] = {}  # file_id -> reason

    @classmethod
    def load(cls, vision=cached_vision) -> "EvidenceStore":
        store = cls(vision)
        for ef in sorted(load_manifest(), key=lambda e: (e.captured_at, e.file_id)):
            try:
                if store._read_file(ef) == 0:
                    store.unread[ef.file_id] = "no records extracted"
            except Exception as exc:  # never drop a file silently
                store.unread[ef.file_id] = f"{type(exc).__name__}: {exc}"
        return store

    def _add(self, rec: EvidenceRecord):
        self.records.append(rec)
        if rec.resource_id:
            self.by_resource[rec.resource_id].append(rec)
        for tok in tokens_of(rec.fields):
            self.by_token[tok].append(rec)
        if rec.kind == "log" and rec.fields.get("trace_id"):
            self.by_trace[(rec.file_id, rec.fields["trace_id"])].append(rec)

    def _read_file(self, ef) -> int:
        suffix = Path(ef.file_path).suffix.lower()
        path = DATASET / ef.file_path
        count = 0

        def emit(kind, observed, resource_id, system_id, table, fields):
            nonlocal count
            count += 1
            self._add(EvidenceRecord(ef.file_id, kind, ef.captured_at, _ts(observed),
                                     resource_id, system_id, table, fields, count))

        if suffix == ".sql":
            for topology, table, row in read_sql(path):
                row["_topology"] = topology
                # A snapshot is only true as of its capture time; row updated_at is not an observation time.
                emit("sql", None, row.get("resource_id"), row.get("origin_system"), table, row)
        elif suffix == ".json":
            for system, table, ts, fields in read_json(path):
                emit("json", ts, fields.get("resource_id"), system, table, fields)
        elif suffix == ".log":
            for ts, fields, _line in read_log(path):
                system = fields.get("service") or fields.get("system") or fields.get("logger") \
                    or fields.get("function")
                emit("log", ts, fields.get("resource_id"), system, fields.get("event"), fields)
        elif suffix == ".txt":
            for ts, fields in read_text(path):
                emit("text", ts, fields["resource_id"], fields["system"], fields["record_type"], fields)
        elif suffix == ".pdf":
            for page_no, text in read_pdf(path):
                emit("pdf", None, None, None, f"page_{page_no}", {"text": text})
        elif suffix == ".png":
            for rec in self._vision(path, ef.file_id):
                emit("image", rec.get("observed_at"), rec.get("resource_id"), None, None,
                     {k: v for k, v in rec.items() if v is not None})
        return count

    @staticmethod
    def visible(rec: EvidenceRecord, audit_at: datetime) -> bool:
        """Can we KNOW this at audit_at? An event is known from its own timestamp (a log line written
        before audit_at is valid even if the file was exported later); anything without one is only
        known as of the file's capture time."""
        return (rec.observed_at or rec.captured_at) <= audit_at

    @staticmethod
    def citable(rec: EvidenceRecord, audit_at: datetime) -> bool:
        """May we CITE this file? Never evidence captured after audit_at."""
        return rec.captured_at <= audit_at

    def for_resource(self, resource_id: str, audit_at: datetime) -> list[EvidenceRecord]:
        return [r for r in self.by_resource.get(resource_id, []) if self.visible(r, audit_at)]

    def for_tokens(self, tokens: set[str], audit_at: datetime, expand_traces: bool = True) -> list[EvidenceRecord]:
        """Records mentioning any token. With expand_traces, also the other log lines of the same lookup
        (same file and trace_id): one line carries the email, a sibling carries the resource_id."""
        seen, out = set(), []

        def take(r):
            key = (r.file_id, r.table, r.ordinal)
            if key not in seen and self.visible(r, audit_at):
                seen.add(key)
                out.append(r)

        for tok in sorted(tokens):
            for r in self.by_token.get(tok.lower(), []):
                take(r)
                if expand_traces and r.kind == "log" and r.fields.get("trace_id"):
                    for sib in self.by_trace[(r.file_id, r.fields["trace_id"])]:
                        take(sib)
        return sorted(out, key=lambda r: (r.captured_at, r.file_id, r.ordinal))
