"""Policy gate: shared loader plus precedence enforcement.

The canonical asset is read only through `portal.regenerative_policy`
(the single snapshot loader). Missing or corrupt policy blocks all
agronomic planning; the full policy text tail travels with every model
prompt independently of the context budget.
"""
import hashlib
import re


class PolicyBlocked(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _load_raw():
    """Single snapshot read; raises PolicyBlocked when unusable."""
    try:
        from portal.regenerative_policy import (
            policy_text, policy_sha256, POLICY_VERSION)
        text = policy_text()
        sha = policy_sha256()
        version = POLICY_VERSION
    except Exception as exc:
        raise PolicyBlocked("policy_unavailable") from exc
    if not text or not text.strip():
        raise PolicyBlocked("policy_empty")
    if not sha or len(sha) != 64:
        raise PolicyBlocked("policy_corrupt")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != sha:
        raise PolicyBlocked("policy_corrupt")
    return {"version": version or "1.0.0", "sha256": sha, "text": text}


def ensure_policy():
    """Return the binding policy or raise PolicyBlocked."""
    try:
        return _load_raw()
    except OSError as exc:
        raise PolicyBlocked("policy_unavailable") from exc


# A local rule that claims to override central guidance must be flagged
# for expert review instead of being silently applied or silently dropped.
_OVERRIDE_RE = re.compile(
    r"(fel[üu]l[íi]r|hat[áa]lyon k[íi]v[üu]l|nem kell betartani|"
    r"k[öo]telez[őo]en elt[ée]r|override|overrule)", re.IGNORECASE)


def check_conflicts(candidate_text, local_rules):
    """Flag local rules that claim to override central guidance."""
    findings = []
    blob = str(candidate_text or "")
    for rule in local_rules or []:
        body = str(getattr(rule, "body", "") or "")
        name = str(getattr(rule, "name", "") or "")
        if _OVERRIDE_RE.search(body) and blob:
            findings.append({
                "severity": "conflict",
                "rule": name,
                "reason": ("A helyi szabály a központi iránymutatás "
                           "felülírására utal; szakmai ellenőrzés kell."),
            })
    return findings
