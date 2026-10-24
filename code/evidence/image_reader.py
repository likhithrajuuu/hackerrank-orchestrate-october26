import base64
import json
import os
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "images"
MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o")  # any vision-capable chat model
PROMPT = (
    "This is a screenshot of an internal admin tool. Extract every record it shows as JSON: "
    '{"records":[{"resource_id":..., "email":..., "customer_reference":..., "display_name":..., '
    '"phone":..., "status":..., "observed_at":..., "notes":...}]}. Use null for anything not shown. '
    "Copy values exactly as displayed. Output JSON only."
)


def read_image(path: Path, file_id: str):
    """Return (records, usage). Uses the cache; calls the model only on a cache miss."""
    cache = CACHE_DIR / f"{file_id}.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        return data["records"], data.get("usage", {})
    from openai import OpenAI  # imported lazily: only needed on a cache miss

    client = OpenAI()  # key comes from OPENAI_API_KEY
    b64 = base64.b64encode(Path(path).read_bytes()).decode()
    resp = client.chat.completions.create(
        model=MODEL, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}])
    records = json.loads(resp.choices[0].message.content)["records"]
    usage = {"model": MODEL, "input_tokens": resp.usage.prompt_tokens,
             "output_tokens": resp.usage.completion_tokens}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"records": records, "usage": usage}, indent=2), encoding="utf-8")
    return records, usage
