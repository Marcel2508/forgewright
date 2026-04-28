#!/bin/sh
# Container entrypoint — picks between poller (supercronic) and webhook (--serve)
# modes based on the first argument. Unknown args are passed through to
# `python -m forgewright` so `docker compose run --rm <svc> --dry-run` still works.
set -eu

case "${1:-poll}" in
  poll)
    schedule="${POLL_SCHEDULE:-*/5 * * * *}"
    crontab="/home/forgewright/crontab"
    echo "${schedule} python -m forgewright" > "${crontab}"
    echo "forgewright: starting supercronic with schedule: ${schedule}" >&2
    # Absolute path is required: supercronic detects PID 1 and re-execs
    # itself via syscall.ForkExec(os.Args[0], …) for the reaper child.
    # ForkExec is a literal path (no PATH lookup), so an unqualified
    # `exec supercronic …` leaves os.Args[0]="supercronic" and the re-exec
    # fails with ENOENT.
    exec /usr/local/bin/supercronic -passthrough-logs "${crontab}"
    ;;
  webhook)
    echo "forgewright: starting webhook server" >&2
    exec python -m forgewright --serve
    ;;
  run|oneshot)
    shift
    exec python -m forgewright "$@"
    ;;
  *)
    exec python -m forgewright "$@"
    ;;
esac
