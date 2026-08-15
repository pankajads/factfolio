"""Provenance validator — the anti-hallucination code gate.

This is a deterministic check, not a prompt instruction. A recommendation's
`evidence` claims "tool X returned value Y". This module checks that claim
against what X *actually* returned this run, as captured by the PostToolUse
hook in `security.hooks`. A claim that cannot be matched is rejected before
the recommendation is ever accepted into the ledger.

Two match strengths, deliberately not collapsed into one boolean:

  EXACT  — the claimed value appears in the tool's output AND under a field
           whose name resembles the one claimed. Strongest evidence.
  VALUE  — the claimed value appears somewhere in that tool's output this
           run, but not obviously under the named field. The agent may have
           mislabelled the field; the number itself is still real. Treated
           as a pass, because the invariant that actually matters is "this
           number came from a real tool call", not "the label is tidy".
  NONE   — the value does not appear anywhere in that tool's output this
           run. Hard rejection. This is what catches fabrication.

Known limit, stated rather than hidden: matching uses a small tolerance to
absorb the agent's own rounding (e.g. stating "15.9%" for a value of
15.89234...). That tolerance could in principle let a subtly-wrong-but-close
number through. It is sized to catch flagrant fabrication — a number with no
basis in any tool call — not to catch a one-digit transcription slip. That is
the tradeoff a rounding-tolerant check has to make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from numbers import Real
from typing import Any

from mybroker.security.hooks import tool_results_for_run

REQUIRED_FIELDS = ("symbol", "action", "conviction", "rationale", "evidence")
VALID_ACTIONS = {"BUY", "SELL", "TRIM", "HOLD", "WATCH"}
VALID_CONVICTIONS = {"high", "medium", "low"}
ACTIONS_REQUIRING_TAX = {"SELL", "TRIM"}


def _tolerance(value: float) -> float:
    """Absolute tolerance for matching a claimed number against a real one.
    Wide enough for 1-2 decimal-place rounding; not so wide that an
    unrelated-but-similar figure passes by coincidence."""
    return max(0.02, abs(value) * 0.005)


def flatten_numeric(obj: Any, prefix: str = "") -> dict[str, float]:
    """Walk a JSON-like structure, returning every numeric leaf by path.

    Booleans are excluded even though `bool` is a `Real` subclass in Python —
    `True` matching a claimed value of `1` would be a false positive.
    """
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten_numeric(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list | tuple):
        for i, v in enumerate(obj):
            out.update(flatten_numeric(v, f"{prefix}[{i}]"))
    elif isinstance(obj, Real) and not isinstance(obj, bool):
        out[prefix] = float(obj)
    return out


EVIDENCE_SHAPE_EXAMPLE = (
    '{"tool": "get_quote", "field": "price", "value": 457.05}'
)


@dataclass
class EvidenceCheck:
    tool: str
    field: str
    claimed_value: float
    strength: str                     # "exact" | "value" | "none" | "malformed"
    matched_path: str | None = None
    matched_value: float | None = None
    available_sample: list[str] = field(default_factory=list)
    malformed_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.strength in ("exact", "value")

    def explain(self) -> str:
        if self.strength == "malformed":
            return (
                f"evidence item is malformed ({self.malformed_reason}). Each item "
                f"must be an object with exactly tool/field/value keys, one number "
                f"per item — e.g. {EVIDENCE_SHAPE_EXAMPLE}. A prose summary is not "
                f"a valid evidence item, even if it names a real tool and value."
            )
        if self.strength == "exact":
            return f"{self.tool}.{self.field}={self.claimed_value} — matched at {self.matched_path}"
        if self.strength == "value":
            return (
                f"{self.tool}: value {self.claimed_value} found at {self.matched_path} "
                f"(field label differs from claimed '{self.field}')"
            )
        sample = ", ".join(self.available_sample[:6]) or "(no numeric output at all)"
        return (
            f"{self.tool}.{self.field}={self.claimed_value} — NOT FOUND in this run's "
            f"output for {self.tool}. Values actually available: {sample}"
        )


def verify_evidence_item(item: Any, run_id: str) -> EvidenceCheck:
    """Check one {tool, field, value} claim against the run's captured tool
    results.

    Never raises. A malformed item (wrong type, missing keys, non-numeric
    value) is reported as a structured "malformed" result with concrete
    guidance — the same graceful-rejection contract as a genuinely
    unverifiable number gets, not a crash. `log_recommendation` depends on
    this: an exception here would abort the tool call instead of giving the
    agent something to correct and retry.
    """
    if not isinstance(item, dict):
        return EvidenceCheck(
            "", "", float("nan"), "malformed",
            malformed_reason=f"expected an object, got {type(item).__name__}: {item!r:.80}",
        )

    missing = [k for k in ("tool", "field", "value") if k not in item]
    if missing:
        return EvidenceCheck(
            str(item.get("tool", "")), str(item.get("field", "")), float("nan"),
            "malformed", malformed_reason=f"missing key(s): {', '.join(missing)}",
        )

    tool = str(item.get("tool", ""))
    field_name = str(item.get("field", ""))
    try:
        claimed = float(item.get("value"))
    except (TypeError, ValueError):
        return EvidenceCheck(
            tool, field_name, float("nan"), "malformed",
            malformed_reason=f"value {item.get('value')!r} is not numeric",
        )

    outputs = tool_results_for_run(run_id).get(tool, [])
    tol = _tolerance(claimed)

    best_exact: tuple[str, float] | None = None
    best_value: tuple[str, float] | None = None
    all_numbers: dict[str, float] = {}

    for output in outputs:
        flat = flatten_numeric(output)
        all_numbers.update(flat)
        for path, val in flat.items():
            if abs(val - claimed) > tol:
                continue
            leaf = path.rsplit(".", 1)[-1].split("[")[0]
            if field_name and (
                field_name.lower() == leaf.lower()
                or field_name.lower() in leaf.lower()
                or leaf.lower() in field_name.lower()
            ):
                best_exact = (path, val)
                break
            if best_value is None:
                best_value = (path, val)
        if best_exact:
            break

    if best_exact:
        return EvidenceCheck(tool, field_name, claimed, "exact",
                              matched_path=best_exact[0], matched_value=best_exact[1])
    if best_value:
        return EvidenceCheck(tool, field_name, claimed, "value",
                              matched_path=best_value[0], matched_value=best_value[1])

    sample = [f"{p}={v}" for p, v in list(all_numbers.items())[:10]]
    return EvidenceCheck(tool, field_name, claimed, "none", available_sample=sample)


@dataclass
class ValidationResult:
    ok: bool
    problems: list[str] = field(default_factory=list)
    evidence_checks: list[EvidenceCheck] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "problems": self.problems,
            "evidence": [
                {
                    "tool": c.tool, "field": c.field, "claimed_value": c.claimed_value,
                    "strength": c.strength, "matched_path": c.matched_path,
                    "explanation": c.explain(),
                }
                for c in self.evidence_checks
            ],
        }


def verify_recommendation(rec: dict, run_id: str) -> ValidationResult:
    """Validate a full recommendation. Structural checks first (cheap, no
    tool-log lookups needed), then evidence provenance."""
    problems: list[str] = []

    missing = [f for f in REQUIRED_FIELDS if not rec.get(f)]
    if missing:
        problems.append(f"missing required field(s): {', '.join(missing)}")

    action = str(rec.get("action", "")).upper()
    if action not in VALID_ACTIONS:
        problems.append(f"action {action!r} not one of {sorted(VALID_ACTIONS)}")

    conviction = str(rec.get("conviction", "")).lower()
    if conviction not in VALID_CONVICTIONS:
        problems.append(f"conviction {conviction!r} not one of {sorted(VALID_CONVICTIONS)}")

    evidence = rec.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        problems.append(
            f"evidence must be a non-empty list of objects, each "
            f"{EVIDENCE_SHAPE_EXAMPLE} — one number per item, not a prose summary"
        )
        return ValidationResult(ok=False, problems=problems)

    if action in ACTIONS_REQUIRING_TAX and not rec.get("tax_impact"):
        problems.append(
            f"action {action} involves a sale — tax_impact is required "
            f"(call compute_tax_impact and cite it)"
        )

    checks = [verify_evidence_item(item, run_id) for item in evidence]
    for c in checks:
        if not c.ok:
            problems.append(c.explain())

    return ValidationResult(ok=not problems, problems=problems, evidence_checks=checks)
