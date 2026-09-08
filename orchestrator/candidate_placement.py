from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from enum import Enum
import uuid
import yaml
import logging

logger = logging.getLogger(__name__)

# ADR-009 placement v3: economics-free ranking over eligible candidates.
PLACEMENT_V3_WEIGHTS = {
    "compatibility": 0.30,
    "readiness": 0.25,
    "capacity": 0.20,
    "locality": 0.10,
    "reliability": 0.10,
    "operational_cost": 0.05,
}

PLACEMENT_TTL_SECONDS = 600.0


class PlacementEligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE_COMPATIBILITY = "INELIGIBLE_COMPATIBILITY"
    INELIGIBLE_READINESS = "INELIGIBLE_READINESS"
    INELIGIBLE_UNKNOWN = "INELIGIBLE_UNKNOWN"


@dataclass
class RankedCandidate:
    pool_id: str
    rank: int
    eligibility: PlacementEligibility
    compatibility_score: float
    readiness_score: float
    cost_score: float
    risk_score: float
    total_score: float
    rationale: List[str]


@dataclass
class PlacementRecommendation:
    recommendation_id: str
    ranked_candidates: List[RankedCandidate]
    selected_candidate_id: Optional[str]
    policy_version: str
    generated_at: datetime
    expires_at: datetime
    assessment_snapshots: Dict[str, str]


class PlacementPolicy:
    def __init__(
        self,
        version: str,
        hard_constraints: Dict = None,
        ranking_weights: Dict = None,
    ):
        self.version = version
        self.hard_constraints = hard_constraints or {
            "reject_unknown": True,
            "require_ready": True,
        }
        # v3 uses ADR-009 weights; legacy v2.1 file keys are translated.
        self.ranking_weights = ranking_weights or dict(PLACEMENT_V3_WEIGHTS)

    @classmethod
    def load_from_file(cls, path: str) -> "PlacementPolicy":
        with open(path) as f:
            data = yaml.safe_load(f)
        version = data.get("version", "v2.1")
        raw = data.get("ranking", {})
        weights = cls._translate_weights(version, raw)
        return cls(
            version=version,
            hard_constraints=data.get("hard_constraints", {}),
            ranking_weights=weights,
        )

    @staticmethod
    def _translate_weights(version: str, raw: Dict) -> Dict:
        """Map legacy v2.1 keys onto v3 semantics; v3 files pass through."""
        if not raw:
            return dict(PLACEMENT_V3_WEIGHTS)
        if any(k in raw for k in PLACEMENT_V3_WEIGHTS):
            merged = dict(PLACEMENT_V3_WEIGHTS)
            merged.update({k: v for k, v in raw.items() if k in merged})
            return merged
        legacy_map = {
            "interruption_risk": "reliability",
            "expected_cost": "operational_cost",
            "transfer_cost": "capacity",
            "topology": "locality",
            "historical_success": "readiness",
        }
        out = {k: 0.0 for k in PLACEMENT_V3_WEIGHTS}
        for lk, lv in raw.items():
            if lk in legacy_map:
                out[legacy_map[lk]] = float(lv)
        total = sum(out.values())
        if total > 0:
            out = {k: v / total for k, v in out.items()}
        else:
            out = dict(PLACEMENT_V3_WEIGHTS)
        # compatibility keeps default unless explicitly set
        return out


@dataclass
class PlacementInput:
    pool_id: str
    compatibility_status: "CompatibilityStatus"
    readiness_status: "ReadinessStatus"
    compatibility_dimensions: Dict[str, bool]
    readiness_checks: List[Dict]
    cost_analysis: Dict
    risk_analysis: Dict
    topology: Dict
    capacity_confidence: float


class PlacementEngine:
    def __init__(self, policy: PlacementPolicy = None):
        self.policy = policy or PlacementPolicy(version="v2.1")

    def rank(
        self,
        inputs: List[PlacementInput],
        ttl_seconds: float = PLACEMENT_TTL_SECONDS,
        migration_time_estimates: Optional[Dict[str, float]] = None,
        freshness: Optional[Dict[str, float]] = None,
    ) -> PlacementRecommendation:
        now = datetime.utcnow()
        # Pre-normalize operational cost across ELIGIBLE candidates only.
        eligible_costs = []
        eligibility_cache: Dict[str, PlacementEligibility] = {}
        for inp in inputs:
            el = self._check_eligibility(inp)
            eligibility_cache[inp.pool_id] = el
            if el == PlacementEligibility.ELIGIBLE:
                eligible_costs.append(self._extract_cost(inp))
        max_cost = max(eligible_costs) if eligible_costs else 1.0

        ranked = []
        for i, inp in enumerate(inputs):
            eligibility = eligibility_cache[inp.pool_id]
            if eligibility != PlacementEligibility.ELIGIBLE:
                ranked.append(RankedCandidate(
                    pool_id=inp.pool_id,
                    rank=i + 1,
                    eligibility=eligibility,
                    compatibility_score=0.0,
                    readiness_score=0.0,
                    cost_score=0.0,
                    risk_score=0.0,
                    total_score=0.0,
                    rationale=[f"Ineligible: {eligibility.value}"],
                ))
                continue

            compat_score = self._compute_compatibility_score(inp)
            readiness_score = self._compute_readiness_score(inp)
            cost_score = self._compute_cost_score(inp, max_cost)
            risk_score = self._compute_risk_score(inp)
            capacity_score = max(0.0, min(1.0, inp.capacity_confidence or 0.0))
            locality_score = self._compute_locality_score(inp)

            weights = self.policy.ranking_weights
            total = (
                compat_score * weights.get("compatibility", 0.30) +
                readiness_score * weights.get("readiness", 0.25) +
                capacity_score * weights.get("capacity", 0.20) +
                locality_score * weights.get("locality", 0.10) +
                risk_score * weights.get("reliability", 0.10) +
                cost_score * weights.get("operational_cost", 0.05)
            )

            rationale = self._build_rationale(inp, total)

            ranked.append(RankedCandidate(
                pool_id=inp.pool_id,
                rank=i + 1,
                eligibility=PlacementEligibility.ELIGIBLE,
                compatibility_score=compat_score,
                readiness_score=readiness_score,
                cost_score=cost_score,
                risk_score=risk_score,
                total_score=total,
                rationale=rationale,
            ))

        mig_est = migration_time_estimates or {}
        fresh = freshness or {}

        def _sort_key(r: RankedCandidate):
            inp = next((x for x in inputs if x.pool_id == r.pool_id), None)
            cap = inp.capacity_confidence if inp else 0.0
            return (
                r.eligibility != PlacementEligibility.ELIGIBLE,
                -r.total_score,
                -(fresh.get(r.pool_id, 0.0)),
                -(cap or 0.0),
                mig_est.get(r.pool_id, float("inf")),
                r.pool_id,  # ADR-009 final deterministic tie-break
            )

        ranked.sort(key=_sort_key)

        for i, r in enumerate(ranked):
            r.rank = i + 1

        selected = ranked[0].pool_id if ranked and ranked[0].eligibility == PlacementEligibility.ELIGIBLE else None

        snapshots = {}
        for inp in inputs:
            snapshots[inp.pool_id] = f"compat:{inp.compatibility_status.value},readiness:{inp.readiness_status.value}"

        return PlacementRecommendation(
            recommendation_id=str(uuid.uuid4()),
            ranked_candidates=ranked,
            selected_candidate_id=selected,
            policy_version=self.policy.version,
            generated_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            assessment_snapshots=snapshots,
        )

    def _check_eligibility(self, inp: PlacementInput) -> PlacementEligibility:
        if inp.compatibility_status.value == "UNKNOWN":
            if self.policy.hard_constraints.get("reject_unknown", True):
                return PlacementEligibility.INELIGIBLE_UNKNOWN
        if inp.compatibility_status.value != "COMPATIBLE":
            return PlacementEligibility.INELIGIBLE_COMPATIBILITY
        if inp.readiness_status.value != "READY":
            if self.policy.hard_constraints.get("require_ready", True):
                return PlacementEligibility.INELIGIBLE_READINESS
        return PlacementEligibility.ELIGIBLE

    def _extract_cost(self, inp: PlacementInput) -> float:
        ca = inp.cost_analysis
        if ca is None:
            return 1.0
        if isinstance(ca, dict):
            return float(ca.get("expected_total_cost", 1.0) or 1.0)
        bd = getattr(ca, "cost_breakdown", None)
        if bd is not None:
            return float(getattr(bd, "expected_total_cost", 1.0) or 1.0)
        return float(getattr(ca, "expected_total_cost", 1.0) or 1.0)

    def _extract_risk(self, inp: PlacementInput) -> float:
        ra = inp.risk_analysis
        if ra is None:
            return 0.5
        if isinstance(ra, dict):
            return float(ra.get("interruption_probability", 0.5))
        return float(getattr(ra, "interruption_probability", 0.5))

    def _compute_locality_score(self, inp: PlacementInput) -> float:
        topo = inp.topology or {}
        if not isinstance(topo, dict):
            return 0.5
        if topo.get("same_region") is True:
            return 1.0
        return float(topo.get("locality_score", 0.5))

    def _compute_compatibility_score(self, inp: PlacementInput) -> float:
        if inp.compatibility_status.value != "COMPATIBLE":
            return 0.0
        passed = sum(1 for v in inp.compatibility_dimensions.values() if v)
        total = len(inp.compatibility_dimensions)
        return passed / max(total, 1)

    def _compute_readiness_score(self, inp: PlacementInput) -> float:
        if inp.readiness_status.value != "READY":
            return 0.0
        passed = sum(1 for c in inp.readiness_checks if c.get("status") == "READY")
        total = len(inp.readiness_checks)
        return passed / max(total, 1)

    def _compute_cost_score(self, inp: PlacementInput, max_cost: float = 1.0) -> float:
        cost = self._extract_cost(inp)
        denom = max(float(max_cost or 1.0), 1e-9)
        return max(0.0, min(1.0, 1.0 - (cost / denom)))

    def _compute_risk_score(self, inp: PlacementInput) -> float:
        risk = self._extract_risk(inp)
        return max(0.0, 1.0 - risk)

    def _build_rationale(self, inp: PlacementInput, total: float) -> List[str]:
        reasons = [f"Total score: {total:.3f}"]
        if inp.compatibility_status.value == "COMPATIBLE":
            reasons.append("Fully compatible")
        if inp.readiness_status.value == "READY":
            reasons.append("Fully ready")
        reasons.append(f"Capacity confidence: {inp.capacity_confidence:.2f}")
        return reasons