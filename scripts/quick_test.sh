#!/usr/bin/env bash
# V2 local regression gate. AWS resources are not changed.
set -euo pipefail
python -m pytest tests/test_v1_removal.py tests/test_job_registry.py \
  tests/test_models_v2.py tests/test_policy_v2.py tests/test_execution_kernel.py \
  tests/test_spotctl_admission.py -v
python -m orchestrator.main --help
python scripts/spotctl.py --help
