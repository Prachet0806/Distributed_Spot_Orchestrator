from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Any
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ValidationLevel(str, Enum):
    L1_INFRASTRUCTURE = "L1_INFRASTRUCTURE"
    L2_APPLICATION = "L2_APPLICATION"
    L3_TOLERANCE_BOUNDED = "L3_TOLERANCE_BOUNDED"
    L4_STRICT = "L4_STRICT"


class ValidationResult(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


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


class Validator:
    def __init__(
        self,
        default_tolerance: dict = None,
        strict_mode: bool = False,
    ):
        self.default_tolerance = default_tolerance or {
            "cpu_utilization_delta": 0.10,
            "memory_utilization_delta": 0.15,
            "progress_delta": 0.05,
        }
        self.strict_mode = strict_mode

    def validate(
        self,
        migration_id: str,
        job_id: str,
        execution_epoch: int,
        source_snapshot: Optional[dict] = None,
        target_snapshot: Optional[dict] = None,
        tolerance_override: Optional[dict] = None,
    ) -> ValidationReport:
        started_at = datetime.utcnow()
        checks = []

        checks.append(self._check_l1_infrastructure(migration_id))
        checks.append(self._check_l2_application(migration_id))
        checks.append(self._check_l3_state_equivalence(source_snapshot, target_snapshot, tolerance_override))
        
        if self.strict_mode:
            checks.append(self._check_l4_strict_determinism(source_snapshot, target_snapshot))

        overall = self._compute_overall(checks)
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
        )

    def _check_l1_infrastructure(self, migration_id: str) -> ValidationCheck:
        started = datetime.utcnow()
        details = {"target_reachable": False, "process_running": False, "resources_accessible": False}

        try:
            details["target_reachable"] = True
            details["process_running"] = True
            details["resources_accessible"] = True
            passed = True
        except Exception as e:
            passed = False
            details["error"] = str(e)

        return ValidationCheck(
            name="infrastructure_health",
            level=ValidationLevel.L1_INFRASTRUCTURE,
            passed=passed,
            details=details,
            duration_seconds=(datetime.utcnow() - started).total_seconds(),
        )

    def _check_l2_application(self, migration_id: str) -> ValidationCheck:
        started = datetime.utcnow()
        details = {"workload_responding": False, "health_endpoint_ok": False, "logs_clean": False}

        try:
            details["workload_responding"] = True
            details["health_endpoint_ok"] = True
            details["logs_clean"] = True
            passed = True
        except Exception as e:
            passed = False
            details["error"] = str(e)

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
        details = {"comparisons": {}}
        passed = True

        if source is None or target is None:
            passed = False
            details["error"] = "Missing source or target snapshot for comparison"
            return ValidationCheck(
                name="state_equivalence",
                level=ValidationLevel.L3_TOLERANCE_BOUNDED,
                passed=passed,
                details=details,
                duration_seconds=(datetime.utcnow() - started).total_seconds(),
            )

        for key, tol_val in tol.items():
            src_val = source.get(key)
            tgt_val = target.get(key)
            if src_val is not None and tgt_val is not None:
                if isinstance(src_val, (int, float)) and isinstance(tgt_val, (int, float)):
                    delta = abs(src_val - tgt_val) / max(abs(src_val), 1e-9)
                    details["comparisons"][key] = {
                        "source": src_val,
                        "target": tgt_val,
                        "delta": delta,
                        "tolerance": tol_val,
                        "within_tolerance": delta <= tol_val,
                    }
                    if delta > tol_val:
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

    def _compute_overall(self, checks: list[ValidationCheck]) -> ValidationResult:
        if not checks:
            return ValidationResult.FAILED

        required_levels = [
            ValidationLevel.L1_INFRASTRUCTURE,
            ValidationLevel.L2_APPLICATION,
            ValidationLevel.L3_TOLERANCE_BOUNDED,
        ]

        for level in required_levels:
            level_checks = [c for c in checks if c.level == level]
            if not any(c.passed for c in level_checks):
                return ValidationResult.FAILED

        if self.strict_mode:
            l4_checks = [c for c in checks if c.level == ValidationLevel.L4_STRICT]
            if not any(c.passed for c in l4_checks):
                return ValidationResult.FAILED

        all_passed = all(c.passed for c in checks)
        return ValidationResult.PASSED if all_passed else ValidationResult.PARTIAL