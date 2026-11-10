"""Evidence gates for safe, reviewable self-improvement proposals."""
from dataclasses import dataclass
import re
from typing import Any, Literal

GateStatus = Literal["blocked", "ready_for_review", "approved_for_apply"]


@dataclass(frozen=True)
class TestEvidence:
    """Auditable test evidence for one version of the workspace."""

    __test__ = False

    passed: bool
    total: int
    failed: int = 0

    def __post_init__(self) -> None:
        if self.total < 0 or self.failed < 0 or self.failed > self.total:
            raise ValueError("Test counts must satisfy 0 <= failed <= total")
        if self.passed and self.failed:
            raise ValueError("Passing evidence cannot contain failed tests")
        if not self.passed and self.failed == 0:
            raise ValueError("Failing evidence must report at least one failed test")


@dataclass(frozen=True)
class ImprovementProposal:
    """A candidate improvement with evidence, not an executable patch."""

    title: str
    observed_failure: str
    hypothesis: str
    patch_summary: str

    def __post_init__(self) -> None:
        for name in ("title", "observed_failure", "hypothesis", "patch_summary"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True)
class ImprovementEvaluation:
    status: GateStatus
    reasons: tuple[str, ...]
    baseline_tests: TestEvidence
    candidate_tests: TestEvidence

    @property
    def eligible_for_review(self) -> bool:
        return self.status in ("ready_for_review", "approved_for_apply")


def parse_pytest_evidence(result: dict[str, Any]) -> TestEvidence | None:
    """Parse trustworthy pytest evidence; ambiguous or incomplete evidence fails closed."""
    if not isinstance(result, dict) or result.get("timed_out") is True:
        return None

    output = "\n".join(str(result.get(key, "")) for key in ("stdout", "stderr"))
    summaries = re.findall(r"(?m)^=+\s*(.*?)\s*=+\s*$", output)
    if not summaries:
        return None

    summary = summaries[-1]
    counts = {name: 0 for name in ("passed", "failed", "error", "skipped", "xfailed", "xpassed")}
    found = False
    for count, name in re.findall(
        r"(?<![\w])([0-9]+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)(?![\w])",
        summary,
    ):
        counts["error" if name.startswith("error") else name] += int(count)
        found = True
    if not found:
        return None

    total = sum(counts.values())
    failures = counts["failed"] + counts["error"]
    # A green process exit is not sufficient when pytest ran no passing tests.
    if total <= 0 or counts["passed"] == 0:
        return None

    # Do not accept contradictory evidence: a success flag with a non-zero
    # process exit, or a missing/invalid exit status, cannot authorize a proposal.
    returncode = result.get("returncode")
    process_succeeded = returncode == 0 and result.get("passed") is True
    passed = process_succeeded and failures == 0
    if not passed and failures == 0:
        return None
    return TestEvidence(passed=passed, total=total, failed=failures)


def evaluate_improvement(
    baseline: TestEvidence,
    candidate: TestEvidence,
    *,
    human_approved: bool = False,
) -> ImprovementEvaluation:
    """Compare evidence; never applies changes or treats tests as full proof."""
    reasons: list[str] = []
    if not baseline.passed:
        reasons.append("Baseline tests must pass before comparing an improvement.")
    if not candidate.passed:
        reasons.append("Candidate tests must pass.")
    if candidate.total < baseline.total:
        reasons.append("Candidate test count is lower than baseline; investigate missing tests.")
    if reasons:
        return ImprovementEvaluation("blocked", tuple(reasons), baseline, candidate)
    if not human_approved:
        return ImprovementEvaluation(
            "ready_for_review",
            ("Evidence passed; human review and approval are still required.",),
            baseline,
            candidate,
        )
    return ImprovementEvaluation(
        "approved_for_apply",
        ("Explicit approval recorded; use the existing guarded proposal workflow to apply.",),
        baseline,
        candidate,
    )
