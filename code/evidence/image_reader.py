"""Reads screenshots of internal admin tools into records.

Extraction goes through the LLM gateway (Sol first, per D-001: screenshots are its job, and the first model tried,
gpt-4o, refused one of them). Results are cached by the image's CONTENT hash, so a replaced screenshot can never be
served a stale extraction, and a cache-only reader lets offline runs and tests use them without any key.
"""
import hashlib
import json
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from llm.config import ModelRole

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "vision"
PROMPT_VERSION = "vision-v2"      # bump when the prompt changes, so cached extractions are not reused by mistake
SYSTEM = (
    "You read screenshots of internal admin tools of a fictional online shop used in a software test. All people, "
    "emails and ids are fictional. Extract every record the screen shows, from the table AND from any side panel or "
    "raw-document view. Copy values exactly as displayed. Use null for anything not shown. Do not guess."
)
USER = ("List every record visible. Put an id such as account_123, search_45, support_9, export_1, backup_2, data_flow_3 "
        "or audit_record_4 in resource_id, and any user_N in customer_reference.")


class ImageRecord(BaseModel):
    resource_id: str | None = None
    email: str | None = None
    customer_reference: str | None = None
    display_name: str | None = None
    phone: str | None = None
    status: str | None = None
    observed_at: str | None = None
    notes: str | None = None


class ImageExtraction(BaseModel):
    records: list[ImageRecord]


Vision = Callable[[Path, str], list[dict]]


def _cache_file(image: bytes) -> Path:
    return CACHE_DIR / f"{hashlib.sha256(image).hexdigest()}_{PROMPT_VERSION}.json"


def cached_vision(path: Path, file_id: str) -> list[dict]:
    """Cache only: never calls a model. Raises FileNotFoundError when this exact image has not been extracted."""
    cache = _cache_file(Path(path).read_bytes())
    if not cache.exists():
        raise FileNotFoundError(f"no cached extraction for {file_id} ({cache.name}); run with a model configured")
    return json.loads(cache.read_text(encoding="utf-8"))["records"]


class VisionExtractor:
    """Cache first, then the gateway (Sol, then Luna). Every real call goes through retries, the breaker and the ledger."""

    def __init__(self, gateway, order=(ModelRole.ESCALATION, ModelRole.PRIMARY)):
        self._gateway, self._order = gateway, order

    def __call__(self, path: Path, file_id: str) -> list[dict]:
        image = Path(path).read_bytes()
        cache = _cache_file(image)
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))["records"]
        result = self._gateway.structured(ImageExtraction, SYSTEM, USER, PROMPT_VERSION, order=self._order, image=image)
        records = [r.model_dump() for r in result.value.records]
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"file_id": file_id, "model": result.model, "records": records}, indent=2),
                         encoding="utf-8")
        return records
