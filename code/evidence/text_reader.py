from pathlib import Path

COLUMNS = ("ts", "system", "resource_id", "record_type", "result", "subject_reference")


def read_text(path: Path):
    """Yield (ts_str, fields) for each '<ts> | system | resource | type | result | subject' line."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.lstrip("- ").split("|")]
        if len(parts) == len(COLUMNS) and parts[0][:2] == "20":
            fields = dict(zip(COLUMNS, parts))
            yield fields["ts"], fields
