#!/usr/bin/env bash
set -e

CMD=$1
PID=$2
DIR=/opt/job_workspace/checkpoint

if [ "$CMD" == "dump" ]; then
  mkdir -p "$DIR"
  criu dump -t "$PID" --images-dir "$DIR" --shell-job --leave-running
elif [ "$CMD" == "restore" ]; then
  criu restore --images-dir "$DIR" --shell-job
else
  echo "Usage: $0 dump <pid> | restore"
  exit 1
fi
