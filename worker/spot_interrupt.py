import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from worker.constants import (
    IMDS_BASE_URL,
    IMDS_TOKEN_URL,
    IMDS_ACTION_URL,
    IMDS_TOKEN_TTL,
    SPOT_INTERRUPT_POLL_INTERVAL,
)


def _fetch_imds_v2_token(timeout=1.0, ttl_seconds=IMDS_TOKEN_TTL):
    request = urllib.request.Request(IMDS_TOKEN_URL, method="PUT")
    request.add_header("X-aws-ec2-metadata-token-ttl-seconds", str(ttl_seconds))
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _get_spot_instance_action(token=None, timeout=1.0):
    request = urllib.request.Request(IMDS_ACTION_URL, method="GET")
    if token:
        request.add_header("X-aws-ec2-metadata-token", token)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _write_interrupt_flag(flag_path):
    os.makedirs(os.path.dirname(flag_path), exist_ok=True)
    payload = {
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "source": "imds",
    }
    with open(flag_path, "w") as f:
        json.dump(payload, f)


def monitor_spot_interruption(flag_path, poll_interval=SPOT_INTERRUPT_POLL_INTERVAL, stop_event=None, logger=print):
    """
    Poll the IMDS spot interruption notice endpoint and create a flag file
    when a termination is scheduled.
    """
    token = None
    while True:
        if stop_event is not None and stop_event.is_set():
            return False

        try:
            if token is None:
                token = _fetch_imds_v2_token()

            action = _get_spot_instance_action(token=token)
            if action:
                _write_interrupt_flag(flag_path)
                logger("Spot interruption notice detected; flag written.")
                if stop_event is not None:
                    stop_event.set()
                return True
        except urllib.error.HTTPError as exc:
            # 404 means no interruption scheduled
            if exc.code != 404:
                logger(f"IMDS HTTP error: {exc}")
        except urllib.error.URLError as exc:
            logger(f"IMDS connection error: {exc}")
            token = None
        except Exception as exc:
            logger(f"IMDS unexpected error: {exc}")
            token = None

        time.sleep(poll_interval)
