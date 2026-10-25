"""Cross-checks our Markdown files against the code, the data and each other, so documents cannot silently drift.

It verifies: file paths that the docs mention exist; every D-NNN and issue number the docs cite is defined; every command in
the docs parses with the real CLI; the README's list of test suites matches the files on disk; and the key numbers in the docs
match what the code and the data produce.
"""
import csv
import os
import re
import shlex
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "tests"))
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("LLM_API_KEY", None)

REPO = CODE.parent
DOCS = [REPO / "README.md", REPO / "PRD.md", REPO / "DECISIONS.md", CODE / "ISSUES.md",
        CODE / "evaluation" / "evaluation_report.md", CODE / "evaluation" / "usage_report.md"]
TEXT = {p: p.read_text(encoding="utf-8") for p in DOCS}
ALL = "\n".join(TEXT.values())
OURS = "\n".join(t for p, t in TEXT.items() if p.name != "README.md")        # README also carries the starter's text
problems: list[str] = []


def check(label, ok, detail=""):
    print(("OK  " if ok else "BAD "), label, "" if ok else f"-> {detail}")
    if not ok:
        problems.append(f"{label}: {detail}")


# 1) Paths mentioned in backticks exist.
EXT = (".py", ".md", ".csv", ".txt", ".json", ".jsonl", ".zip", ".sql", ".log")
mentioned = set()
for path, text in TEXT.items():
    for tok in re.findall(r"`([^`\s]+)`", text):
        tok = tok.rstrip(",.;:)")
        if re.search(r"[<>*${}|=]|^https?:|^-", tok) or tok.startswith("("):
            continue
        if re.fullmatch(r"\.?[a-z]+", tok) and tok.startswith("."):          # a bare extension such as .csv
            continue
        if re.fullmatch(r"[A-Z][A-Z_]*(/[A-Z]+)?", tok):                      # an environment variable pair such as X_PRICE_IN/OUT
            continue
        if "/" in tok or tok.endswith(EXT):
            mentioned.add((path.name, tok))
LOCATIONS = [REPO, CODE, REPO / "dataset", REPO / "dataset" / "evidence", CODE / "tests", CODE / "evaluation"] + \
            [REPO / "dataset" / "evidence" / d for d in ("records", "logs", "databases", "documents", "images")]
generated = {"output.csv", "code.zip", ".env", "usage.jsonl", "log.txt"}
missing = sorted((doc, tok) for doc, tok in mentioned
                 if tok not in generated and not any((loc / tok.rstrip("/")).exists() for loc in LOCATIONS)
                 and not re.fullmatch(r"(?:evidence_\d+|activity_\d+|audit_\d+|case_\d+)", tok))
check(f"every file path mentioned in the docs exists ({len(mentioned)} checked)", not missing, str(missing[:12]))

# 2) Decision and issue references are defined.
defined_d = set(re.findall(r"^## (D-\d{3}):", TEXT[REPO / "DECISIONS.md"], re.M))
cited_d = set(re.findall(r"\bD-\d{3}\b", ALL))
check("every D-NNN cited anywhere is defined in DECISIONS.md", cited_d <= defined_d, str(sorted(cited_d - defined_d)))
check("decisions are numbered without gaps", sorted(defined_d) == [f"D-{i:03d}" for i in range(1, len(defined_d) + 1)], str(sorted(defined_d)))
issues = [int(n) for n in re.findall(r"^### (\d+)\.", TEXT[CODE / "ISSUES.md"], re.M)]
check("issues are numbered without gaps", issues == list(range(1, len(issues) + 1)), str(issues))
cited_i = set()
for m in re.finditer(r"\bissues? (\d+)(?:(?:,| and| to) (\d+))?(?: and (\d+))?", ALL):
    cited_i |= {int(g) for g in m.groups() if g}
check("every issue number cited anywhere exists in ISSUES.md", cited_i <= set(issues), str(sorted(cited_i - set(issues))))

# 3) Commands in the docs parse with the real CLI.
import cli                                                                        # noqa: E402

parser = cli.build_parser()
commands = set()
for text in TEXT.values():
    commands |= set(re.findall(r"`(python code/(?:main|cli)\.py [^`]+)`", text))
    for block in re.findall(r"```(?:bash)?\n(.*?)```", text, re.S):
        commands |= {ln.split("#")[0].strip() for ln in block.splitlines() if ln.strip().startswith("python code/main.py")}
bad_cmd = []
for cmd in sorted(commands):
    if re.search(r"[<>]|a,b|\.\.\.", cmd):
        continue
    try:
        parser.parse_args(shlex.split(cmd)[2:])
    except SystemExit:
        bad_cmd.append(cmd)
check(f"every command in the docs parses with the real CLI ({len(commands)} found)", not bad_cmd, str(bad_cmd))

# 4) The README's test list matches the files on disk.
on_disk = sorted(p.stem[len("check_"):] for p in (CODE / "tests").glob("check_*.py"))
listed = sorted(re.search(r"for t in ([a-z ]+); do", TEXT[REPO / "README.md"]).group(1).split())
check("the README's list of check suites equals the check_*.py files", listed == on_disk, f"{listed} vs {on_disk}")
count = len(on_disk)
check(f"the docs say {count} check scripts wherever they give a number",
      all(int(n) == count for n in re.findall(r"\b(\d+) check scripts\b", ALL)), str(re.findall(r"\b(\d+) check scripts\b", ALL)))

# 5) Numbers in the docs match the code and the data.
from agents.intent import IntentRouter                                             # noqa: E402
from classifier import classify                                                    # noqa: E402
from loaders import load_requests                                                  # noqa: E402

requests = load_requests()
router = IntentRouter()
routed = [r for r in requests if not router.is_strong(classify(r.request_text))]
n, strong, distinct = len(requests), len(requests) - len(routed), len({r.request_text for r in routed})
check("518 requests, 490 strong, 28 routed, 23 distinct texts are what the code produces",
      (n, strong, len(routed), distinct) == (518, 490, 28, 23), str((n, strong, len(routed), distinct)))
claims_of = [(int(a), int(b)) for a, b in re.findall(r"(\d+) of (\d+) requests", OURS) if b == "518"]
check("every 'X of 518 requests' in our docs is the strong or the routed count", all(a in (strong, len(routed)) for a, _ in claims_of), str(claims_of))
check("the docs say 28 routed requests are 23 distinct texts wherever both appear",
      all((int(a), int(b)) == (len(routed), distinct) for a, b in re.findall(r"(\d+) requests \((\d+) distinct", OURS)))

report = TEXT[CODE / "evaluation" / "evaluation_report.md"]
from collections import Counter                                                    # noqa: E402

if (REPO / "output.csv").exists():                                                 # not part of code.zip, so optional here
    rows = list(csv.reader(open(REPO / "output.csv", newline="", encoding="utf-8")))[1:]
    counts = Counter(r[1] for r in rows)
    check("output.csv has 518 rows", len(rows) == 518, str(len(rows)))
    for status in ("INCOMPLETE", "COMPLETE", "NOT_APPLICABLE"):
        check(f"the evaluation report states the real count of {status} rows ({counts[status]})", re.search(rf"\b{counts[status]}\b `?{status}", report) is not None)
else:
    print("OK   (skipped: output.csv is submitted separately and is not in this copy)")

from evaluate_output import run_samples                                            # noqa: E402

results = run_samples()
status_ok = sum(w == g for _, w, g, *_ in results)
check(f"the report's case status accuracy is the measured {status_ok} of {len(results)}", f"{status_ok} of {len(results)}" in report)
fields = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")
tp = fp = fn = 0
for _, _, _, want, got, _ in results:
    w, g = {tuple(s[f] for f in fields) for s in want}, {tuple(s[f] for f in fields) for s in got}
    tp, fp, fn = tp + len(w & g), fp + len(g - w), fn + len(w - g)
check(f"the report's step counts are the measured {tp} of {tp + fn} found and {tp} of {tp + fp} right",
      f"{tp} of {tp + fn}" in report and f"{tp} of {tp + fp}" in report and f"{tp} of {tp + fn}" in TEXT[REPO / "DECISIONS.md"], f"tp={tp} fn={fn} fp={fp}")

# 6) Facts about the build that several documents state.
env = (REPO / ".env.example").read_text(encoding="utf-8")
models = re.findall(r"^(?:LUNA|SOL)_MODEL=(\S+)", env, re.M)
check("the model names in .env.example appear in the README and the usage report",
      all(m in TEXT[REPO / "README.md"] or m in TEXT[CODE / "evaluation" / "usage_report.md"] for m in models), str(models))
vision = len(list((CODE / "cache" / "vision").glob("*.json")))
check(f"{vision} cached screenshot extractions, and the docs say 11 screenshots", vision == 11 and "11 screenshots" in ALL, str(vision))
llm_cache = len(list((CODE / "cache" / "llm").glob("*.json")))
check("the response cache holds at least the 23 distinct intent answers the final run needs", llm_cache >= 23, str(llm_cache))
check("the docs do not claim the validator or planner are still unbuilt",
      not re.search(r"(planner|validator|step builder)[^.\n]{0,60}(not built|not built yet|\*\*not built)", ALL, re.I))
check("no document still says provider prompt caching is not happening or not triggering",
      not re.search(r"(is not happening|does not happen|not triggering|Enabled but \*\*not)", ALL), "")
check("the old cache directory is gone", not (CODE / "cache" / "images").exists())

print()
print("docs cross-check:", "all consistent" if not problems else f"{len(problems)} problem(s)")
sys.exit(1 if problems else 0)
