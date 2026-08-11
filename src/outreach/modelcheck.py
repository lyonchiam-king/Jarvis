"""Prove a model can actually do this job, before it touches a real lead.

Anthropic enforces the schema; every other provider is asked nicely. The
difference only shows up on the hard schema -- most models return clean JSON
for six fields and quietly drop half of the nineteen the site spec needs.

So this runs all three, cheapest first, on one invented business. About three
calls and a few cents, and it answers the question the warning cannot: *will
this model work for me?*
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .providers import llm

# A business with enough texture to brief, and obviously fictional so no real
# listing is touched.
SAMPLE_EVIDENCE = """\
Business name: Kedai Gunting Rambut Seri Mutiara
Category: barber shop
Address: 12 Lorong Contoh, 10200 George Town, Penang
Google rating: 4.6 from 88 reviews
Existing web presence: social_only
Opening hours: Tue-Sun 10:00-20:00; Closed Monday

Reviews:
- [5/5, a month ago] Uncle Lim has cut my hair for nine years. Never rushes.
- [5/5, 2 weeks ago] Called three times to book, nobody picked up. Walked in
  and waited 40 minutes. Worth it but the phone thing is annoying.
- [4/5, a month ago] Old-school hot towel shave. Bring cash, no card machine.
- [5/5, 3 months ago] They fixed a fringe another shop butchered.
"""


@dataclass
class CheckResult:
    name: str
    ok: bool = False
    seconds: float = 0.0
    detail: str = ""
    missing: tuple[str, ...] = ()

    @property
    def line(self) -> str:
        mark = "[ OK ]" if self.ok else "[FAIL]"
        timing = f"{self.seconds:5.1f}s" if self.seconds else "     "
        return f"{mark} {timing}  {self.name}: {self.detail}"


@dataclass
class ModelReport:
    provider: str = ""
    model: str = ""
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def lines(self) -> list[str]:
        out = [f"Testing {self.model} via {self.provider}", ""]
        out += [check.line for check in self.checks]
        out.append("")

        if self.ok:
            out += [
                "This model can hold every schema the pipeline needs.",
                "Run Research on 5 leads next and read the angles -- passing "
                "here proves the shape, not the quality of the thinking.",
            ]
            return out

        failed = [c for c in self.checks if not c.ok]
        out.append("Not ready. What went wrong:")
        for check in failed:
            out.append(f"  {check.name}: {check.detail}")
            if check.missing:
                out.append(f"    fields it left out: {', '.join(check.missing)}")
        out += [
            "",
            # Counted from the schema rather than written out, because a
            # spelled-out number silently goes stale the moment a field is
            # added -- and this line is read by someone already confused
            # about why their model failed.
            f"The site spec is the demanding one -- "
            f"{len(llm.SITE_SPEC_SCHEMA['required'])} required fields.",
            "A model that passes the brief and fails the spec will research "
            "your leads happily and then fail every single site brief.",
            "",
            "Try a larger model, or set research.provider back to anthropic.",
        ]
        return out


def _missing_from(detail: str) -> tuple[str, ...]:
    marker = "missing required field(s): "
    if marker not in detail:
        return ()
    return tuple(part.strip() for part in detail.split(marker, 1)[1].split(","))


def _run(name: str, call) -> CheckResult:
    started = time.monotonic()
    try:
        call()
    except llm.LLMError as exc:
        elapsed = time.monotonic() - started
        return CheckResult(
            name=name,
            ok=False,
            seconds=elapsed,
            detail=str(exc).splitlines()[0],
            missing=_missing_from(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001 -- a check must never crash the CLI
        return CheckResult(
            name=name,
            ok=False,
            seconds=time.monotonic() - started,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return CheckResult(
        name=name,
        ok=True,
        seconds=time.monotonic() - started,
        detail="valid, every required field present",
    )


def run(config: Any, writer: Any | None = None) -> ModelReport:
    """Exercise all three schemas. Cheapest first, so a dead key fails fast."""
    report = ModelReport(
        provider=config.research.provider, model=config.research.model
    )
    writer = writer or llm.make_writer(config)

    spec_prompt = llm.build_site_spec_prompt(
        name="Kedai Gunting Rambut Seri Mutiara",
        category="barber shop",
        location="George Town, Penang",
        tone="old-school, unhurried",
        services="cuts, hot towel shave, fringe repair",
        price_tier="budget",
        praise="Uncle Lim never rushes; fixed a botched fringe",
        pain_points="Nobody answers the phone; long waits; cash only",
        angle="Reviews are full of people who could not get through by phone.",
    )
    outreach_prompt = llm.build_outreach_prompt(
        name="Kedai Gunting Rambut Seri Mutiara",
        category="barber shop",
        location="George Town, Penang",
        tone="old-school, unhurried",
        services="cuts, hot towel shave",
        praise="Uncle Lim never rushes",
        pain_points="Nobody answers the phone",
        angle="Bookings are being lost to an unanswered phone.",
        sender_name="Lyon Chiam",
        sender_title="Founder",
        company="COAI",
    )

    report.checks.append(
        _run("Research brief (6 fields)", lambda: writer.write(SAMPLE_EVIDENCE))
    )
    # No point spending on the rest if the model cannot be reached at all.
    if not report.checks[0].ok and not report.checks[0].missing:
        return report

    report.checks.append(
        _run("Outreach copy (3 fields)", lambda: writer.write_outreach_copy(outreach_prompt))
    )
    report.checks.append(
        _run(
            "Site spec (19 fields -- the hard one)",
            lambda: writer.write_site_spec(spec_prompt),
        )
    )
    return report
