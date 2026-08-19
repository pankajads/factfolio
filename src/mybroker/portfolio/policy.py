"""Investment policy: parse the rules, detect breaches.

The policy lives in `memory/investment_policy.md` — human-readable rationale
with a fenced ```yaml block holding the machine-enforceable limits. One file
serves both readers: you get the reasoning, the code gets the numbers, and
they cannot drift apart because they are the same document.

Breach detection is deterministic. The agent is never asked whether the
portfolio complies; it is *told*, and its job is to reason about what to do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from mybroker.config import POLICY_FILE, cd_hint_if_project_nearby
from mybroker.portfolio.metrics import PortfolioSnapshot

_YAML_BLOCK = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class Breach:
    """One violation of the policy."""

    rule: str
    severity: str          # critical | high | medium | low
    subject: str           # the position/sector/bucket at fault
    actual: float
    limit: float
    message: str

    @property
    def excess(self) -> float:
        return self.actual - self.limit

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "subject": self.subject,
            "actual_pct": round(self.actual, 2),
            "limit_pct": round(self.limit, 2),
            "excess_pct": round(self.excess, 2),
            "message": self.message,
        }


@dataclass
class Policy:
    """Parsed, enforceable investment policy."""

    target_cagr_pct: float = 15.5
    core_min_pct: float = 70.0
    core_max_pct: float = 75.0
    max_position_pct: float = 8.0
    max_sector_pct: float = 25.0
    max_satellite_position_pct: float = 5.0
    min_positions: int = 12
    max_positions: int = 25
    monthly_capital: float = 30_000.0
    max_annual_turnover_pct: float = 25.0
    speculative_cap_pct: float = 10.0
    speculative_symbols: list[str] = field(default_factory=list)
    glidepath_start: Any = None                       # date | None
    glidepath: list[dict] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # ── Glidepath ────────────────────────────────────────────────────────────
    def current_core_target(self, today: date | None = None) -> tuple[float, str]:
        """The core % expected *right now*, and a label for it.

        Falls back to the final floor when no glidepath is configured. Measuring
        against a dated step keeps the breach actionable — an alert that reads
        `critical` for four years is one that gets ignored.
        """
        if not self.glidepath or not self.glidepath_start:
            return self.core_min_pct, "final target"

        start = self.glidepath_start
        if isinstance(start, str):
            start = date.fromisoformat(start)
        today = today or date.today()
        elapsed = (today.year - start.year) * 12 + (today.month - start.month)

        steps = sorted(self.glidepath, key=lambda s: s["months"])
        for step in steps:
            if elapsed < step["months"]:
                return float(step["core_pct"]), (
                    f"month {elapsed} of {step['months']}-month step"
                )
        return float(steps[-1]["core_pct"]), "final target reached"

    @classmethod
    def load(cls, path: Path | None = None) -> Policy:
        path = path or POLICY_FILE
        if not path.exists():
            raise FileNotFoundError(
                f"No investment policy at {path}. This file defines the rules "
                f"every recommendation is measured against — run "
                f"`factfolio init`.{cd_hint_if_project_nearby()}"
            )
        match = _YAML_BLOCK.search(path.read_text())
        if not match:
            raise ValueError(
                f"{path} has no ```yaml block. The policy must carry a machine-"
                f"readable block so limits and prose cannot drift apart."
            )
        data = yaml.safe_load(match.group(1)) or {}
        known = {f for f in cls.__dataclass_fields__ if f != "raw"}
        return cls(**{k: v for k, v in data.items() if k in known}, raw=data)

    # ── Breach detection ─────────────────────────────────────────────────────
    def check(self, snap: PortfolioSnapshot) -> list[Breach]:
        """Every way the portfolio currently violates policy, worst first."""
        breaches: list[Breach] = []

        # Core/satellite balance — measured against the CURRENT glidepath step,
        # not the final floor, so the alert stays actionable.
        target, label = self.current_core_target()
        if snap.core_pct < target:
            gap = target - snap.core_pct
            breaches.append(
                Breach(
                    rule="core_allocation",
                    # Severity reflects distance from *this step's* target.
                    severity="critical" if gap > 15 else "high" if gap > 5 else "medium",
                    subject="portfolio",
                    actual=snap.core_pct,
                    limit=target,
                    message=(
                        f"Core is {snap.core_pct:.1f}% against a {target:.0f}% "
                        f"glidepath target ({label}) — {gap:.1f} points behind. "
                        f"Final floor is {self.core_min_pct:.0f}%. The portfolio is "
                        f"{snap.satellite_pct:.1f}% satellite, a structurally different "
                        f"risk profile from the one the {self.target_cagr_pct:.1f}% "
                        f"target assumes."
                    ),
                )
            )

        # Single-position limits.
        for w in snap.positions:
            cap = (
                self.max_satellite_position_pct
                if w.bucket == "satellite"
                else self.max_position_pct
            )
            if w.weight_pct > cap:
                breaches.append(
                    Breach(
                        rule="position_size",
                        severity="high" if w.weight_pct > cap * 1.5 else "medium",
                        subject=w.key,
                        actual=w.weight_pct,
                        limit=cap,
                        message=(
                            f"{w.key} is {w.weight_pct:.1f}% of the portfolio against a "
                            f"{cap:.0f}% cap for {w.bucket} positions."
                        ),
                    )
                )

        # Sector limits.
        for w in snap.sectors:
            if w.weight_pct > self.max_sector_pct:
                breaches.append(
                    Breach(
                        rule="sector_concentration",
                        severity="high",
                        subject=w.key,
                        actual=w.weight_pct,
                        limit=self.max_sector_pct,
                        message=(
                            f"{w.key} is {w.weight_pct:.1f}% of the portfolio against a "
                            f"{self.max_sector_pct:.0f}% cap. Sector shocks hit every "
                            f"holding in the group at once."
                        ),
                    )
                )

        # Speculative bucket.
        if self.speculative_symbols:
            spec = sum(
                w.weight_pct for w in snap.positions if w.key in self.speculative_symbols
            )
            if spec > self.speculative_cap_pct:
                present = [
                    w.key for w in snap.positions if w.key in self.speculative_symbols
                ]
                breaches.append(
                    Breach(
                        rule="speculative_exposure",
                        severity="high",
                        subject=", ".join(present),
                        actual=spec,
                        limit=self.speculative_cap_pct,
                        message=(
                            f"Speculative holdings ({', '.join(present)}) total "
                            f"{spec:.1f}% against a {self.speculative_cap_pct:.0f}% cap. "
                            f"These are turnaround/binary-outcome names where permanent "
                            f"capital loss is a live possibility."
                        ),
                    )
                )

        # Diversification breadth.
        n = len(snap.positions)
        if n < self.min_positions:
            breaches.append(
                Breach(
                    rule="min_positions", severity="medium", subject="portfolio",
                    actual=float(n), limit=float(self.min_positions),
                    message=f"{n} positions against a {self.min_positions} minimum.",
                )
            )
        elif n > self.max_positions:
            breaches.append(
                Breach(
                    rule="max_positions", severity="low", subject="portfolio",
                    actual=float(n), limit=float(self.max_positions),
                    message=(
                        f"{n} positions against a {self.max_positions} maximum — "
                        f"more than can be monitored properly."
                    ),
                )
            )

        breaches.sort(key=lambda b: (SEVERITY_ORDER.get(b.severity, 9), -abs(b.excess)))
        return breaches

    def compliance_summary(self, snap: PortfolioSnapshot) -> dict:
        breaches = self.check(snap)
        counts: dict[str, int] = {}
        for b in breaches:
            counts[b.severity] = counts.get(b.severity, 0) + 1
        return {
            "compliant": not breaches,
            "n_breaches": len(breaches),
            "by_severity": counts,
            "core_pct": round(snap.core_pct, 2),
            "core_target_min": self.core_min_pct,
            "core_gap_pct": round(max(0.0, self.core_min_pct - snap.core_pct), 2),
            "breaches": [b.to_dict() for b in breaches],
        }

    # ── Remediation planning ─────────────────────────────────────────────────
    def months_to_close_core_gap(self, snap: PortfolioSnapshot) -> dict:
        """How long new capital alone takes to reach the core floor.

        Answers the question that actually matters when fresh capital is
        available: can this be fixed without selling (and paying tax)?

        Each month adds `monthly_capital` to both core and the total, so the
        core share converges on 100% — solving for the month at which it first
        crosses the floor.
        """
        core_value = snap.total_value * snap.core_pct / 100
        total = snap.total_value
        target = self.core_min_pct / 100

        if snap.core_pct >= self.core_min_pct:
            return {"months": 0, "achievable_with_new_capital_only": True,
                    "capital_required": 0.0}

        if self.monthly_capital <= 0:
            return {"months": None, "achievable_with_new_capital_only": False,
                    "capital_required": None,
                    "note": "No monthly capital configured — requires selling."}

        # Solve core + x >= target * (total + x)  →  x >= (target*total - core)/(1-target)
        required = (target * total - core_value) / (1 - target)
        months = required / self.monthly_capital

        return {
            "months": round(months, 1),
            "capital_required": round(required, 2),
            "monthly_capital": self.monthly_capital,
            "achievable_with_new_capital_only": True,
            "note": (
                f"Directing ₹{self.monthly_capital:,.0f}/month entirely into core "
                f"holdings reaches the {self.core_min_pct:.0f}% floor in about "
                f"{months:.0f} months with zero selling and therefore zero capital-"
                f"gains tax."
            ),
        }
