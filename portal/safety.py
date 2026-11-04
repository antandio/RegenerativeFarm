import re
# Conservative safety screen: blocks clearly prohibited content. Explicitly NOT a completeness guarantee.
PATTERNS = [
    r"\b\d+\s*(kg/ha|l/ha|ml/l|g/l|dkg/ha|liter/hektár|kg hektáronként)\b",
    r"(növényvédőszer|műtrágya|gyomirtó|rovarirtó).{0,40}(adag|dózis|mennyiség)\s*[:=]?\s*\d",
    r"(jogszabály szerint|törvény szerint kötelező|hatósági határidő|támogatásra jogosult|jogosult a támogatásra)",
    r"(garantáljuk|garantált hozam|biztos bevétel|garantált bevétel|karbonjóváírás.*garant)",
    r"(diagnózis\s*:|állatorvosi kezelés\s*:|antibiotikum.{0,30}\d+\s*mg|\b\d+\s*mg\b.{0,30}antibiotikum)",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in PATTERNS]
def contains_prohibited(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _COMPILED)
def screen_parsed(parsed: dict) -> bool:
    """Return True if prohibited content detected in steps, common fields,
    or focused_questions (all user-visible model text is screened)."""
    if not isinstance(parsed, dict):
        return False
    chunks = []
    for s in parsed.get("steps", []) or []:
        if not isinstance(s, dict):
            continue
        for k in ("title","action_text","rationale","risk_note","expert_referral"):
            v = s.get(k)
            if v: chunks.append(str(v))
        for a in s.get("assumptions", []) or []:
            chunks.append(str(a))
    for a in parsed.get("assumptions", []) or []:
        chunks.append(str(a))
    for q in parsed.get("focused_questions", []) or []:
        chunks.append(str(q))
    blob = "\n".join(chunks)
    return contains_prohibited(blob)
