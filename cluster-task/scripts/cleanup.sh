#!/usr/bin/env bash
set -uo pipefail

log_info()  { echo "[cleanup] $*"; }
log_warn()  { echo "[cleanup] [warn] $*"; }
log_error() { echo "::error::[cleanup] $*"; }

NAMESPACE="${INPUT_NAMESPACE:-default}"
JOB_NAME="${JOB_NAME:-}"
# cleanup_policy removed: always perform full cleanup
NNODES="${INPUT_NNODES:-0}"
SOURCE_STAGE_DIR="${SOURCE_STAGE_DIR:-}"

# Resolve job name outputs job_name before any resources are created; an empty name means no resources exist yet
if [ -z "$JOB_NAME" ]; then
  log_warn "No job_name available, skipping cleanup"
  exit 0
fi

# Always remove the NAS source staging tarball, regardless of cleanup_policy, since it is temporary transfer storage
if [ -n "${SOURCE_STAGE_DIR:-}" ] && [ -f "$SOURCE_STAGE_DIR" ]; then
  log_info "Removing NAS source staging tarball: $SOURCE_STAGE_DIR"
  rm -f "$SOURCE_STAGE_DIR"
  _parent_dir="$(dirname "$SOURCE_STAGE_DIR")"
  if [ -d "$_parent_dir" ] && [ -z "$(ls -A "$_parent_dir" 2>/dev/null)" ]; then
    rmdir "$_parent_dir" 2>/dev/null || true
  fi
fi

log_info "Performing full cleanup: $JOB_NAME (namespace=$NAMESPACE)"

# --- Update task record ---
TASK_DIR="/wl_nas/devops/ppu-dashboard/tasks"
TASK_FILE="${TASK_DIR}/${JOB_NAME}.json"
(
  TASK_RECORD_LOCKED=false
  if [ -f "$TASK_FILE" ]; then
    exec 9> "${TASK_FILE}.lock"
    if flock -w 5 9; then
      TASK_RECORD_LOCKED=true
    else
      log_warn "Task record lock unavailable; leaving the record unchanged"
    fi
  fi
  TASK_STATUS="${INPUT_JOB_STATUS:-}"
  case "$TASK_STATUS" in
    ""|unknown|pending|running|scheduled)
      if [ -f "$TASK_FILE" ]; then
        TASK_STATUS="$(jq -r '.status // empty' "$TASK_FILE")"
      fi
      ;;
  esac
  case "$TASK_STATUS" in
    ""|unknown|pending|running|scheduled)
      TASK_STATUS="failed"
      [ "${INPUT_WORKFLOW_STATUS:-}" = "cancelled" ] && TASK_STATUS="cancelled"
      ;;
  esac
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "job_status=$TASK_STATUS" >> "$GITHUB_OUTPUT"
  fi
  if [ "$TASK_RECORD_LOCKED" = true ] && command -v jq >/dev/null 2>&1; then
    TASK_END_TIME="$(date +%Y-%m-%dT%H:%M:%S%z)"
    if jq --arg end_time "$TASK_END_TIME" --arg status "$TASK_STATUS" \
      '.end_time = (if (.end_time // "") == "" then $end_time else .end_time end) | .status = $status' \
      "$TASK_FILE" > "${TASK_FILE}.tmp" && mv "${TASK_FILE}.tmp" "$TASK_FILE"; then
      log_info "Task record updated: ${TASK_FILE} (status=${TASK_STATUS})"
    fi
  fi
)

POD_SELECTOR="ppu-job=$JOB_NAME"

# Share one budget across all cluster requests, reserving time for a final force deletion.
CLEANUP_TIMEOUT=60
FORCE_RESERVE=10
CLEANUP_START=$(date +%s)
CLEANUP_DEADLINE=$((CLEANUP_START + CLEANUP_TIMEOUT))
CLEANUP_CONFIRMED=true
PODS_CONFIRMED=false

kube_request() {
  local reserve=$1 remaining request_seconds
  shift
  remaining=$((CLEANUP_DEADLINE - $(date +%s) - reserve))
  [ "$remaining" -gt 0 ] || return 1
  request_seconds=$remaining
  [ "$request_seconds" -le 10 ] || request_seconds=10
  timeout --signal=KILL "${request_seconds}s" \
    kubectl "$@"
}

cleanup_sleep() {
  local seconds=$1 reserve=$2 remaining
  remaining=$((CLEANUP_DEADLINE - $(date +%s) - reserve))
  [ "$remaining" -gt 0 ] || return 1
  [ "$seconds" -le "$remaining" ] || seconds=$remaining
  sleep "$seconds"
}

# Retry deletion to handle transient failures such as etcd NOSPACE
kube_delete_retry() {
  local max_retries=3 delay=5 attempt resource
  local -a pending=("$@") failed=() delete_args=()
  for ((attempt=1; attempt<=max_retries; attempt++)); do
    failed=()
    for resource in "${pending[@]}"; do
      delete_args=("$JOB_NAME")
      [ "$resource" != pods ] || delete_args=(-l "$POD_SELECTOR")
      if ! kube_request "$FORCE_RESERVE" delete "$resource" "${delete_args[@]}" \
        -n "$NAMESPACE" --wait=false --ignore-not-found=true 2>/dev/null; then
        failed+=("$resource")
      fi
    done
    [ "${#failed[@]}" -ne 0 ] || return 0
    pending=("${failed[@]}")
    if [ "$attempt" -lt "$max_retries" ]; then
      log_warn "Deletion unconfirmed for ${pending[*]} (round ${attempt}), retrying in up to ${delay}s..."
      cleanup_sleep "$delay" "$FORCE_RESERVE" || break
    fi
  done
  log_warn "Deletion could not be confirmed for ${pending[*]} within the retry and cleanup limits."
  return 1
}

log_info "Deleting pods: $JOB_NAME (namespace=$NAMESPACE)"
PENDING_RESOURCES=(pods)

# Standalone mode creates no Service or PodGroup.
if [ "$NNODES" != "0" ]; then
  log_info "Deleting service: $JOB_NAME"
  log_info "Deleting podgroup: $JOB_NAME"
  PENDING_RESOURCES+=(svc podgroups.scheduling.x-k8s.io)
else
  log_info "Single-/multi-card mode (nnodes=0), no service/podgroup to clean up, skipping."
fi
kube_delete_retry "${PENDING_RESOURCES[@]}" || CLEANUP_CONFIRMED=false

log_info "Waiting for associated pods to terminate..."
FORCE_ATTEMPTED=false
while true; do
  REMAINING=$((CLEANUP_DEADLINE - $(date +%s)))
  [ "$REMAINING" -gt 0 ] || break

  if [ "$REMAINING" -le "$FORCE_RESERVE" ] && [ "$FORCE_ATTEMPTED" = false ]; then
    FORCE_ATTEMPTED=true
    log_warn "Pod termination is unconfirmed, force-deleting within the remaining cleanup budget."
    if ! kube_request 0 delete pods -n "$NAMESPACE" -l "$POD_SELECTOR" \
      --force --grace-period=0 --wait=false --ignore-not-found=true 2>/dev/null; then
      CLEANUP_CONFIRMED=false
      log_warn "Force deletion failed or exhausted the cleanup budget."
    fi
    continue
  fi

  REQUEST_RESERVE=$FORCE_RESERVE
  [ "$FORCE_ATTEMPTED" = false ] || REQUEST_RESERVE=0
  if POD_LIST=$(kube_request "$REQUEST_RESERVE" get pods -n "$NAMESPACE" \
    -l "$POD_SELECTOR" --no-headers 2>/dev/null); then
    POD_COUNT=0
    while IFS= read -r pod_line; do
      if [ -n "$pod_line" ]; then POD_COUNT=$((POD_COUNT + 1)); fi
    done <<< "$POD_LIST"
    if [ "$POD_COUNT" -eq 0 ]; then
      PODS_CONFIRMED=true
      break
    fi
    ELAPSED=$(( $(date +%s) - CLEANUP_START ))
    echo "  Waiting for ${POD_COUNT} pods to terminate... (${ELAPSED}s/${CLEANUP_TIMEOUT}s)"
  else
    log_warn "Pod query failed, resource removal remains unconfirmed; retrying within the budget."
  fi
  cleanup_sleep 5 "$REQUEST_RESERVE" || continue
done

if [ -d "/tmp/training-manifests" ]; then
  log_info "Removing temporary manifest files"
  rm -rf /tmp/training-manifests
fi

if [ "$CLEANUP_CONFIRMED" = true ] && [ "$PODS_CONFIRMED" = true ]; then
  log_info "All associated pods have terminated."
  log_info "Cleanup complete: $JOB_NAME"
else
  log_warn "Cleanup finished, but cluster resource removal could not be confirmed: $JOB_NAME. Remaining pods may have finalizers; no finalizers were changed."
fi
