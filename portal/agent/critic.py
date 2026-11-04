"""Independent critic: evidence, unknowns, cost and policy re-check.

The critic never sees the planner prompt, only the candidate, the
manifest source IDs and the policy binding. It runs as the last
substantive step even for read-only plans, and its findings are saved.
"""
HIGH_RISK_COMMANDS = frozenset({"delete"})


def review_candidate(candidate, manifest, policy):
    """Return {"verdict": ..., "findings": [...]}.

    Verdicts: "verified" (no findings), "needs_revision" (fixable),
    "blocked" (unsupported/critical: must stop).
    """
    findings = []
    known = {str(i.get("source_id")) for i in (manifest or {}).get("items", [])
             if isinstance(i, dict) and i.get("source_id")}
    steps = candidate.get("steps") if isinstance(candidate, dict) else []
    for pos, step in enumerate(steps or [], start=1):
        if not isinstance(step, dict):
            findings.append(_finding(
                "critical", pos, "step-malformed",
                "A lépés nem értelmezhető objektum."))
            continue
        for item in step.get("evidence") or []:
            sid = str((item or {}).get("source_id") or "")
            if sid and sid not in known:
                findings.append(_finding(
                    "critical", pos, "unsupported-claim",
                    f"A {sid} hivatkozás nem szerepel a forrásjegyzékben; "
                    "bizonyíték nélküli állítás nem mehet jóváhagyásra."))
        risk = str(step.get("risk") or candidate.get("risk") or "").lower()
        unknowns = step.get("uncertainties") or candidate.get("unknowns") or []
        if risk == "high" and not unknowns:
            findings.append(_finding(
                "critical", pos, "high-risk-no-unknowns",
                "Magas kockázatú lépés feltárt bizonytalanság nélkül; "
                "blokkolva jóváhagyásig."))
        for command in step.get("commands") or []:
            op = str((command or {}).get("op") or "")
            if op in HIGH_RISK_COMMANDS:
                findings.append(_finding(
                    "critical", pos, "high-cost-command",
                    f"A(z) {op} művelet nagy költségű/visszafordíthatatlan; "
                    "blokkolva jóváhagyásig."))
    if isinstance(candidate, dict) and not candidate.get("unknowns"):
        findings.append(_finding(
            "revision", 0, "no-unknowns-listed",
            "A terv nem sorol fel bizonytalanságot; "
            "kiegészítés szükséges."))
    policy_version = (policy or {}).get("version") or ""
    if not policy_version:
        findings.append(_finding(
            "critical", 0, "policy-unbound",
            "A tervhez nincs kötött policyverzió; blokkolva."))
    if any(f["severity"] == "critical" for f in findings):
        verdict = "blocked"
    elif findings:
        verdict = "needs_revision"
    else:
        verdict = "verified"
    return {"verdict": verdict, "findings": findings}


def _finding(severity, step, code, reason):
    return {"severity": severity, "step": step, "code": code,
            "reason": reason}
