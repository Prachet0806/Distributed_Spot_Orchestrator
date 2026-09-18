#!/usr/bin/env bash
# criu_wrapper.sh — sole CRIU entry point on workers (Protocols #9 §11.8).
#
#   criu_wrapper.sh dump <pid> <dir> [options]
#     exit 0 success | 1 criu error | 2 timeout | 3 preflight failure
#   criu_wrapper.sh restore <dir> [options]   # prints restored root PID
#     exit 0 success | 1 criu error | 2 timeout | 3 PARTIAL_RESTORE | 4 env incompatible
#   criu_wrapper.sh verify <dir>
#     exit 0 valid | 1 invalid | 2 indeterminate
#   criu_wrapper.sh preflight                 # cached per pool with TTL (caller-side)
#     prints key=value lines; exit 0 pass | 1 fail
#
# <dir> defaults to $WORKSPACE_ROOT/checkpoint (/opt/job_workspace/checkpoint).

set -u

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/opt/job_workspace}"
DEFAULT_DIR="$WORKSPACE_ROOT/checkpoint"
TIMEOUT_BIN="$(command -v timeout || true)"

usage() {
  echo "Usage: $0 dump <pid> <dir> [criu opts] | restore <dir> [criu opts] | verify <dir> | preflight" >&2
  exit 1
}

need_criu() {
  if ! command -v criu >/dev/null 2>&1; then
    echo "criu binary not found" >&2
    return 3
  fi
}

run_with_timeout() {
  local secs="$1"; shift
  if [ -n "$TIMEOUT_BIN" ] && [ "$secs" -gt 0 ] 2>/dev/null; then
    "$TIMEOUT_BIN" "$secs" "$@"
    rc=$?
    [ $rc -eq 124 ] && return 2
    return $rc
  fi
  "$@"
}

CMD="${1:-}"; shift || true

case "$CMD" in
  dump)
    PID="${1:-}"; DIR="${2:-$DEFAULT_DIR}"; shift 2 2>/dev/null || shift $# 2>/dev/null || true
    [ -z "${PID:-}" ] && usage
    need_criu || exit 3
    mkdir -p "$DIR"
    if ! criu check --unprivileged >/dev/null 2>&1 && ! sudo -n criu check >/dev/null 2>&1; then
      echo "criu preflight failed for dump" >&2
      exit 3
    fi
    run_with_timeout "${CRIU_DUMP_TIMEOUT:-280}" \
      criu dump -t "$PID" --images-dir "$DIR" --shell-job --leave-running "$@"
    rc=$?
    [ $rc -eq 124 ] && exit 2
    exit $rc
    ;;
  restore)
    DIR="${1:-$DEFAULT_DIR}"; shift 2>/dev/null || shift $# 2>/dev/null || true
    [ -d "$DIR" ] || { echo "images dir missing: $DIR" >&2; exit 4; }
    need_criu || exit 4
    OUT="$(run_with_timeout "${CRIU_RESTORE_TIMEOUT:-170}" \
      criu restore --images-dir "$DIR" --shell-job "$@" 2>&1)"
    rc=$?
    echo "$OUT"
    if [ $rc -eq 124 ] || [ $rc -eq 2 ]; then exit 2; fi
    if echo "$OUT" | grep -qiE "partial|incomplete|failed to restore.*(task|process)"; then
      exit 3
    fi
    [ $rc -ne 0 ] && exit 1
    # Best-effort restored root PID for the control plane.
    RESTORED_PID="$(echo "$OUT" | grep -oiE 'pid[ :]+[0-9]+' | grep -oiE '[0-9]+' | head -n1)"
    [ -n "$RESTORED_PID" ] && echo "$RESTORED_PID"
    exit 0
    ;;
  verify)
    DIR="${1:-$DEFAULT_DIR}"
    [ -d "$DIR" ] || { echo "images dir missing: $DIR" >&2; exit 2; }
    [ -f "$DIR/inventory.img" ] || { echo "inventory.img missing" >&2; exit 1; }
    if ! command -v criu >/dev/null 2>&1; then
      echo "criu missing: cannot verify" >&2
      exit 2
    fi
    if criu show --images-dir "$DIR" >/dev/null 2>&1; then
      echo "valid"
      exit 0
    fi
    # Fall back to inventory presence: determinate-invalid vs indeterminate.
    if grep -q . "$DIR/inventory.img" 2>/dev/null; then
      echo "indeterminate: show failed but inventory non-empty" >&2
      exit 2
    fi
    echo "invalid: empty inventory" >&2
    exit 1
    ;;
  preflight)
    {
      echo -n "criu_version="
      criu --version 2>/dev/null | head -n1 | awk '{print $NF}'
      echo -n "kernel_version="
      uname -r
      echo -n "architecture="
      uname -m
      echo -n "capabilities="
      criu check --unprivileged >/dev/null 2>&1 && echo "unprivileged-ok" || echo "needs-privilege"
      echo -n "filesystem_features=basic"
      echo ""
      echo -n "unsupported_features="
      criu check 2>&1 | grep -i "does not" | head -n3 | tr '\n' ';'
      echo ""
    }
    if criu check >/dev/null 2>&1 || criu check --unprivileged >/dev/null 2>&1; then
      echo "passed=true"
      exit 0
    fi
    echo "passed=false"
    exit 1
    ;;
  *)
    usage
    ;;
esac
