from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class WorkloadObservation:
    job_id: str
    execution_epoch: int
    progress: Optional[float]
    checkpoint_size_bytes: Optional[int]
    checkpoint_duration_seconds: Optional[float]
    cpu_utilization: Optional[float]
    memory_utilization: Optional[float]
    observed_at: datetime
    observation_confidence: float
    source: str = "worker"


@dataclass
class WorkloadEstimate:
    job_id: str
    execution_epoch: int
    progress: Optional[float]
    remaining_runtime_seconds: Optional[float]
    expected_completion_at: Optional[datetime]
    prediction_confidence: float
    checkpoint_size_estimate_bytes: Optional[int]
    checkpoint_duration_estimate_seconds: Optional[float]
    estimated_at: datetime
    estimator_version: str
    model_version: str
    observation_snapshot_version: str


@dataclass
class EstimatorConfig:
    estimator_version: str = "v2-rule-based-1"
    model_version: str = "v2-heuristic-1"
    min_confidence_for_estimate: float = 0.3
    default_checkpoint_overhead_factor: float = 1.2


class WorkloadEstimator:
    def __init__(self, config: Optional[EstimatorConfig] = None):
        self.config = config or EstimatorConfig()
        self._observation_history: dict[str, list[WorkloadObservation]] = {}

    def estimate(
        self,
        job_id: str,
        execution_epoch: int,
        workload_type: str,
        observation: Optional[WorkloadObservation] = None,
        checkpoint_history: Optional[list[dict]] = None,
    ) -> WorkloadEstimate:
        if observation is None:
            return self._conservative_estimate(job_id, execution_epoch, workload_type)

        self._record_observation(job_id, observation)

        if observation.progress is None or observation.observation_confidence < self.config.min_confidence_for_estimate:
            return self._conservative_estimate(job_id, execution_epoch, workload_type)

        remaining_runtime = self._predict_remaining_runtime(
            workload_type, observation.progress, observation
        )

        checkpoint_size_est = self._predict_checkpoint_size(observation, checkpoint_history)
        checkpoint_duration_est = self._predict_checkpoint_duration(observation, checkpoint_history)

        return WorkloadEstimate(
            job_id=job_id,
            execution_epoch=execution_epoch,
            progress=observation.progress,
            remaining_runtime_seconds=remaining_runtime,
            expected_completion_at=(
                datetime.utcnow() + timedelta(seconds=remaining_runtime)
                if remaining_runtime is not None
                else None
            ),
            prediction_confidence=min(observation.observation_confidence, 0.9),
            checkpoint_size_estimate_bytes=checkpoint_size_est,
            checkpoint_duration_estimate_seconds=checkpoint_duration_est,
            estimated_at=datetime.utcnow(),
            estimator_version=self.config.estimator_version,
            model_version=self.config.model_version,
            observation_snapshot_version=observation.observed_at.isoformat(),
        )

    def _predict_remaining_runtime(
        self, workload_type: str, progress: float, observation: WorkloadObservation
    ) -> Optional[float]:
        if progress <= 0 or progress >= 1.0:
            return None

        history = self._observation_history.get(observation.job_id, [])
        if len(history) < 2:
            return self._heuristic_remaining_runtime(workload_type, progress, observation)

        recent = history[-2:]
        p1, t1 = recent[0].progress, recent[0].observed_at.timestamp()
        p2, t2 = recent[1].progress, recent[1].observed_at.timestamp()

        if p2 <= p1 or t2 <= t1:
            return self._heuristic_remaining_runtime(workload_type, progress, observation)

        rate = (p2 - p1) / (t2 - t1)
        if rate <= 0:
            return None

        remaining_progress = 1.0 - progress
        return remaining_progress / rate

    def _heuristic_remaining_runtime(
        self, workload_type: str, progress: float, observation: WorkloadObservation
    ) -> Optional[float]:
        type_defaults = {
            "short": 300,
            "medium": 3600,
            "long": 86400,
            "stateful": 7200,
        }
        base_duration = type_defaults.get(workload_type.lower(), 3600)
        if progress > 0:
            return base_duration * (1.0 - progress) / max(progress, 0.01)
        return base_duration

    def _predict_checkpoint_size(
        self, observation: WorkloadObservation, history: Optional[list[dict]]
    ) -> Optional[int]:
        if observation.checkpoint_size_bytes is not None:
            est = int(observation.checkpoint_size_bytes * self.config.default_checkpoint_overhead_factor)
            if history:
                avg_actual = sum(h.get("size_bytes", 0) for h in history) / len(history)
                est = int((est + avg_actual) / 2)
            return est
        if history:
            avg = sum(h.get("size_bytes", 0) for h in history) / len(history)
            return int(avg * self.config.default_checkpoint_overhead_factor)
        return None

    def _predict_checkpoint_duration(
        self, observation: WorkloadObservation, history: Optional[list[dict]]
    ) -> Optional[float]:
        if observation.checkpoint_duration_seconds is not None:
            est = observation.checkpoint_duration_seconds * self.config.default_checkpoint_overhead_factor
            if history:
                avg_actual = sum(h.get("duration_seconds", 0) for h in history) / len(history)
                est = (est + avg_actual) / 2
            return est
        if history:
            avg = sum(h.get("duration_seconds", 0) for h in history) / len(history)
            return avg * self.config.default_checkpoint_overhead_factor
        return None

    def _conservative_estimate(
        self, job_id: str, execution_epoch: int, workload_type: str
    ) -> WorkloadEstimate:
        return WorkloadEstimate(
            job_id=job_id,
            execution_epoch=execution_epoch,
            progress=None,
            remaining_runtime_seconds=None,
            expected_completion_at=None,
            prediction_confidence=0.1,
            checkpoint_size_estimate_bytes=None,
            checkpoint_duration_estimate_seconds=None,
            estimated_at=datetime.utcnow(),
            estimator_version=self.config.estimator_version,
            model_version=self.config.model_version,
            observation_snapshot_version="none",
        )

    def _record_observation(self, job_id: str, observation: WorkloadObservation):
        if job_id not in self._observation_history:
            self._observation_history[job_id] = []
        self._observation_history[job_id].append(observation)
        if len(self._observation_history[job_id]) > 50:
            self._observation_history[job_id].pop(0)


from datetime import timedelta