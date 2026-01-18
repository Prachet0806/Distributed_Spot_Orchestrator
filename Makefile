run-worker:
	python worker/job_runner.py

watch:
	python orchestrator/watcher.py

test:
	python -m pytest tests/ -v

test-coverage:
	python -m pytest tests/ --cov=orchestrator --cov=storage --cov=worker --cov-report=html

test-unit:
	python -m pytest tests/ -v -k "test_"

quick-test:
	bash scripts/quick_test.sh