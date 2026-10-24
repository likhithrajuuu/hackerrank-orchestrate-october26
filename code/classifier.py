"""Rule-based intent classifier: does request_text authorize account deletion?

The matching logic is language-agnostic. Languages and phrasings live in the
pattern lists below, so supporting a new one means adding patterns, not code.
"""
import re
import unicodedata
from dataclasses import dataclass, field

DELETION, NON_DELETION, UNCERTAIN = "DELETION", "NON_DELETION", "UNCERTAIN"


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower().replace("’", "'")


def _any(patterns, text):
    return [p for p in patterns if re.search(p, text)]


NEGATION = [
    r"\b(do not|don't|dont|never|without)\s+(\w+\s+){0,2}(delet|eras|remov|clos)",
    r"\bnot\s+(asking|requesting|an?\s+request)\b.{0,25}(delet|eras|remov|clos)",
]

MARKETING = [r"marketing", r"promotional", r"newsletter"]
MARKETING_OPTOUT = [r"\bonly\b", r"\bstop\b", r"unsubscribe", r"opt[- ]?out"]

# Enough on their own to mean "delete".
STRONG = [
    r"\bdelet(e|ed|es|ing|ion)\b", r"\berase[ds]?\b", r"\berasure\b",
    r"\bclos(e|ed|ing)\b.{0,30}\baccount\b", r"\baccount\b.{0,30}\bclos(e|ed|ing)\b",
    r"eliminar|eliminaci[oó]n|elimin[eé]",  # es
    r"suppression|supprim",                  # fr
    r"exclus[aã]o|excluir",                  # pt
    r"삭제", r"削除", r"消去",                 # ko, ja (no word boundaries)
]
# Ambiguous on their own; only count together with a SCOPE word.
WEAK = [r"\bremov(e|ed|al|ing)\b", r"\bgone\b", r"\bclean\b.*\bout\b", r"\bpermanent(ly)?\b"]
SCOPE = [r"account", r"profile", r"personal", r"\bdata\b", r"details", r"identifier",
         r"\bname\b", r"search result", r"\bcopy\b", r"records?", r"file", r"export"]

NON_DELETION_PATTERNS = [
    r"out of date|outdated|misspell|incorrect|wrong|typo|change the email|update (my|the)",   # correction
    r"(download|copy of|send me|portable|export)\b.{0,60}(information|data|account|invoice)",  # access/export
    r"what (personal )?information .{0,20}(hold|have)",                                       # access
    r"(can'?t|cannot|unable to) (access|log ?in)|reset the login|password",                   # login help
    r"invoice",
]


@dataclass
class Decision:
    intent: str
    reasons: list = field(default_factory=list)
    mixed: bool = False


def classify(text: str) -> Decision:
    t = _norm(text)
    non_del = _any(NON_DELETION_PATTERNS, t)

    if _any(NEGATION, t):
        return Decision(NON_DELETION, ["negated deletion"])

    strong = _any(STRONG, t)
    if _any(MARKETING, t) and _any(MARKETING_OPTOUT, t) and not strong:
        return Decision(NON_DELETION, ["marketing opt-out"])

    if strong:
        return Decision(DELETION, [f"strong:{p}" for p in strong], mixed=bool(non_del))

    weak = _any(WEAK, t)
    if weak and _any(SCOPE, t) and not non_del:
        return Decision(DELETION, [f"weak+scope:{p}" for p in weak])

    if non_del:
        return Decision(NON_DELETION, [f"non-deletion:{p}" for p in non_del])

    return Decision(UNCERTAIN, ["no decisive cue"])
