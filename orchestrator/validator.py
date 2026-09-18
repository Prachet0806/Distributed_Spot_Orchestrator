from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any, Callable
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ValidationLevel(str, Enum):
    L1_INFRASTRUCTURE = "L1_INFRASTRUCTURE"
    L2_APPLICATION = "L2_APPLICATION"
    L3_TOLERANCE_BOUNDED = "L3_TOLERANCE_BOUNDED"
    L4_STRICT = "L4_STRICT"
    L4_OWNERSHIP = "L4_OWNERSHIP"


class ValidationResult(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    # V2 verdicts (Protocols #10 §12.2). PARTIAL maps to INCONCLUSIVE.
    VALID = "VALID"
    INVALID = "INVALID"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass
class ValidationCheck:
    name: str
    level: ValidationLevel
    passed: bool
    details: dict
    duration_seconds: float


@dataclass
class ValidationReport:
    migration_id: str
    job_id: str
    execution_epoch: int
    overall_result: ValidationResult
    checks: list[ValidationCheck]
    started_at: datetime
    completed_at: datetime
    tolerance_config: dict = None
    epoch_match: bool = True
    lineage_valid: bool = True
    contract_version: str = ""

    def to_v2_verdict(self) -> str:
        """Map onto VALID / INVALID / INCONCLUSIVE (§12.2)."""
        if self.overall_result in (ValidationResult.PASSED, ValidationResult.VALID):
            return ValidationResult.VALID.value
        if self.overall_result == ValidationResult.FAILED or not self.epoch_match \
                or not self.lineage_valid:
            return ValidationResult.INVALID.value
        return ValidationResult.INCONCLUSIVE.value


DEFAULT_TOLERANCE_CONTRACT = {
    "version": "tolerance-v1",
    "minimum_level": "L3",
    "strict_determinism": False,
    "metrics": {
        "cpu_utilization_delta": {"tolerance_type": "RELATIVE", "value": 0.10},
        "memory_utilization_delta": {"tolerance_type": "RELATIVE", "value": 0.15},
        "progress_delta": {"tolerance_type": "RELATIVE", "value": 0.05},
    },
}


class Validator:
    """Post-restore correctness gate (Protocols #10).

    L1 alive is never enough alone; epoch + lineage are verified
    independently of process health; L4 bitwise applies only when the
    workload contract opts into `strict_determinism`. Evidence comes from
    injected probes — without probes L1/L2 report pass-through details
    stamped `probe: stub-v1` so audits show the weakness.
    """

    def __init__(
        self,
        default_tolerance: dict = None,
        strict_mode: bool = False,
        infra_probe: Optional[Callable[..., dict]] = None,
        app_probe: Optional[Callable[..., dict]] = None,
    ):
        self.default_tolerance = default_tolerance or {
            "cpu_utilization_delta": 0.10,
            "memory_utilization_delta": 0.15,
            "progress_delta": 0.05,
        }
        self.contract_version = (default_tolerance or {}).get("version", "tolerance-v1")
        self.strict_mode = strict_mode
        self.infra_probe = infra_probe
        self.app_probe = app_probe

    def validate(
        self,
        migration_id: str,
        job_id: str,
        execution_epoch: int,
        source_snapshot: Optional[dict] = None,
        target_snapshot: Optional[dict] = None,
        tolerance_override: Optional[dict] = None,
        expected_epoch: Optional[int] = None,
        checkpoint_lineage: Optional[dict] = None,
        strict_determinism: Optional[bool] = None,
    ) -> ValidationReport:
        started_at = datetime.utcnow()
        checks = []

        checks.append(self._check_l1_infrastructure(migration_id))
        checks.append(self._check_l2_application(migration_id))
        checks.append(self._check_l3_state_equivalence(
            source_snapshot, target_snapshot,
            tolerance_override or self.default_tolerance))

        want_strict = self.strict_mode if strict_determinism is None else strict_determinism
        if want_strict:
            checks.append(self._check_l4_strict_determinism(source_snapshot, target_snapshot))

        epoch_match, lineage_valid = True, True
        checks.append(self._check_ownership(
            execution_epoch, expected_epoch, checkpoint_lineage))
        epoch_check = checks[-1]
        epoch_match = bool(epoch_check.details.get("epoch_match", True))
        lineage_valid = bool(epoch_check.details.get("lineage_valid", True))

        overall = self._compute_overall(checks, want_strict)
        # Ownership failure can never pass regardless of process health.
        if not (epoch_match and lineage_valid) and overall == ValidationResult.PASSED:
            overall = ValidationResult.FAILED
        completed_at = datetime.utcnow()

        return ValidationReport(
            migration_id=migration_id,
            job_id=job_id,
            execution_epoch=execution_epoch,
            overall_result=overall,
            checks=checks,
            started_at=started_at,
            completed_at=completed_at,
            tolerance_config=tolerance_override or self.default_tolerance,
            epoch_match=epoch_match,
            lineage_valid=lineage_valid,
            contract_version=(tolerance_override or {}).get("version", self.contract_version),
        )

    def to_v2_report(self, report: ValidationReport, checkpoint_id: str = "",
                     lineage_id: str = ""):
        """Convert to the models_v2 ValidationReport (VALID/INVALID/INCONCLUSIVE)."""
        from orchestrator.models_v2 import ValidationReport as V2Report
        from orchestrator.models_v2 import ValidationResult as V2Result
        from orchestrator.models_v2 import ValidationLevel as V2Level
        verdict = report.to_v2_verdict()
        highest = V2Level.L1
        for check in report.checks:
            if check.passed and check.level == ValidationLevel.L2_APPLICATION:
                highest = V2Level.L2
            if check.passed and check.level == ValidationLevel.L3_TOLERANCE_BOUNDED:
                highest = V2Level.L3
            if check.passed and check.level == ValidationLevel.L4_STRICT:
                highest = V2Level.L4
        return V2Report(
            migration_id=report.migration_id, job_id=report.job_id,
            execution_epoch=report.execution_epoch,
            result=V2Result(verdict), level_reached=highest,
            epoch_match=report.epoch_match, lineage_valid=report.lineage_valid,
            measurements={c.name: c.details for c in report.checks},
            contract_version=report.contract_version)

    def _check_l1_infrastructure(self, migration_id: str) -> ValidationCheck:
        started = datetime.utcnow()
        if self.infra_probe is not None:
            try:
                details = dict(self.infra_probe(migration_id=migration_id))
                passed = bool(details.pop("passed", True))
            except Exception as e:
                details, passed = {"error": str(e)}, False
        else:
            details = {"target_reachable": True, "process_running": True,
                       "resources_accessible": True,
                       "probe": "stub-v1",
                       "note": "no infra probe configured; configure for production"}
            passed = True

        return ValidationCheck(
            name="infrastructure_health",
            level=ValidationLevel.L1_INFRASTRUCTURE,
            passed=passed,
            details=details,
            duration_seconds=(datetime.utcnow() - started).total_seconds(),
        )

    def _check_l2_application(self, migration_id: str) -> ValidationCheck:
        started = datetime.utcnow()
        if self.app_probe is not None:
            try:
                details = dict(self.app_probe(migration_id=migration_id))
                passed = bool(details.pop("passed", True))
            except Exception as e:
                details, passed = {"error": str(e)}, False
        else:
            details = {"workload_responding": True, "health_endpoint_ok": True,
                       "logs_clean": True, "probe": "stub-v1",
                       "note": "no app probe configured; configure for production"}
            passed = True

        return ValidationCheck(
            name="application_health",
            level=ValidationLevel.L2_APPLICATION,
            passed=passed,
            details=details,
            duration_seconds=(datetime.utcnow() - started).total_seconds(),
        )

    def _check_l3_state_equivalence(
        self,
        source: Optional[dict],
        target: Optional[dict],
        tolerance: Optional[dict],
    ) -> ValidationCheck:
        started = datetime.utcnow()
        tol = tolerance or self.default_tolerance
        # Support versioned contract shape {metrics: {name: {...}}} and legacy flat shape.
        flat: dict = {}
        if isinstance(tol, dict) and isinstance(tol.get("metrics"), dict):
            for name, spec in tol["metrics"].items():
                if isinstance(spec, dict) and "value" in spec:
                    flat[name] = spec["value"]
                else:
                    flat[name] = spec
        elif isinstance(tol, dict):
            flat = {k: v for k, v in tol.items() if not k.startswith("_") and k != "version"}
        details = {"comparisons": {}, "contract_version": tol.get("version", "") if isinstance(tol, dict) else ""}
        passed = True

        if source is None or target is None:
            # Missing snapshots are insufficient evidence, not proof of failure.
            return ValidationCheck(
                name="state_equivalence",
                level=ValidationLevel.L3_TOLERANCE_BOUNDED,
                passed=False,
                details={**details, "error": "Missing source or target snapshot",
                         "inconclusive": True},
                duration_seconds=(datetime.utcnow() - started).total_seconds(),
            )

        for key, tol_val in flat.items():
            if key in ("minimum_level", "strict_determinism"):
                continue
            src_val = source.get(key)
            tgt_val = target.get(key)
            if src_val is not None and tgt_val is not None:
                if isinstance(src_val, (int, float)) and isinstance(tgt_val, (int, float)):
                    delta = abs(src_val - tgt_val) / max(abs(src_val), 1e-9)
                    within = delta <= tol_val
                    details["comparisons"][key] = {
                        "source": src_val,
                        "target": tgt_val,
                        "delta": delta,
                        "tolerance": tol_val,
                        "within_tolerance": within,
                    }
                    if not within:
                        passed = False

        return ValidationCheck(
            name="state_equivalence",
            level=ValidationLevel.L3_TOLERANCE_BOUNDED,
            passed=passed,
            details=details,
            duration_seconds=(datetime.utcnow() - started).total_seconds(),
        )

    def _check_l4_strict_determinism(
        self,
        source: Optional[dict],
        target: Optional[dict],
    ) -> ValidationCheck:
        started = datetime.utcnow()
        details = {"bitwise_equal": False}
        passed = False

        if source and target:
            passed = source == target
            details["bitwise_equal"] = passed

        return ValidationCheck(
            name="strict_determinism",
            level=ValidationLevel.L4_STRICT,
            passed=passed,
            details=details,
            duration_seconds=(datetime.utcnow() - started).total_seconds(),
        )

    def _check_ownership(
        self,
        execution_epoch: int,
        expected_epoch: Optional[int],
        lineage: Optional[dict],
    ) -> ValidationCheck:
        started = datetime.utcnow()
        epoch_match = True if expected_epoch is None else (execution_epoch == expected_epoch)
        lineage_valid = True
        details: dict = {"epoch_match": epoch_match}
        if lineage is not None:
            lineage_valid = bool(lineage.get("lineage_valid", lineage.get("valid", True)))
            details["lineage_valid"] = lineage_valid
            details["lineage_id"] = lineage.get("lineage_id")
        else:
            details["lineage_valid"] = True
        if not epoch_match:
            details["error"] = (f"epoch mismatch: expected {expected_epoch}, "
                                f"got {execution_epoch}")
        return ValidationCheck(
            name="ownership",
            level=ValidationLevel.L4_OWNERSHIP,
            passed=bool(epoch_match and lineage_valid),
            details=details,
            duration_seconds=(datetime.utcnow() - started).total_seconds(),
        )

    def _compute_overall(self, checks: list[ValidationCheck], want_strict: bool) -> ValidationResult:
        if not checks:
            return ValidationResult.FAILED

        required_levels = [
            ValidationLevel.L1_INFRASTRUCTURE,
            ValidationLevel.L2_APPLICATION,
            ValidationLevel.L3_TOLERANCE_BOUNDED,
            ValidationLevel.L4_OWNERSHIP,
        ]

        for level in required_levels:
            level_checks = [c for c in checks if c.level == level]
            if not any(c.passed for c in level_checks):
                # L3 without snapshots is inconclusive, not invalid.
                if level == ValidationLevel.L3_TOLERANCE_BOUNDED and any(
                        c.details.get("inconclusive") for c in level_checks):
                    return ValidationResult.PARTIAL
                return ValidationResult.FAILED

        if want_strict:
            l4_checks = [c for c in checks if c.level == ValidationLevel.L4_STRICT]
            if not any(c.passed for c in l4_checks):
                return ValidationResult.FAILED

        all_passed = all(c.passed for c in checks)
        return ValidationResult.PASSED if all_passed else ValidationResult.PARTIAL
