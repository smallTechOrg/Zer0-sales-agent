#!/bin/bash
# Move the Zer0 API on the shared HTTPS load balancer from the VMs to Cloud Run.
# DNS does not change: api.smalltech.in / api-staging.smalltech.in already
# point at the load balancer (staging-lb, url map https-staging). Only the
# backend behind their routes changes, and that is instant and reversible.
#
#   ./scripts/lb_cutover.sh prepare              # serverless NEGs + backend services (no traffic)
#   ./scripts/lb_cutover.sh switch staging       # api-staging.smalltech.in -> Cloud Run
#   ./scripts/lb_cutover.sh switch production    # api.smalltech.in (and unknown hosts) -> Cloud Run
#   ./scripts/lb_cutover.sh rollback staging|production   # back to the VM backend
#
# The url map is shared with other apps (webscraper, hashtaglocal, ir-agent):
# only the exact Zer0 backend services below are swapped, nothing else.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-ai-agent-boilerplate0}"
REGION="${REGION:-us-central1}"
URL_MAP="${URL_MAP:-https-staging}"

# environment -> "<VM backend service> <Cloud Run service> <public host>"
# (a function, not an associative array: macOS ships bash 3.2)
env_info() {
  case "$1" in
    staging)    echo "staging-backend zero-sales-agent-staging api-staging.smalltech.in" ;;
    production) echo "https-backend zero-sales-agent api.smalltech.in" ;;
    *)          return 1 ;;
  esac
}

g() { gcloud --project "$PROJECT_ID" "$@"; }

prepare() {
  for env in staging production; do
    read -r _ svc _ <<<"$(env_info "$env")"
    g compute network-endpoint-groups describe "${svc}-neg" --region "$REGION" >/dev/null 2>&1 \
      || g compute network-endpoint-groups create "${svc}-neg" --region "$REGION" \
           --network-endpoint-type serverless --cloud-run-service "$svc"
    g compute backend-services describe "${svc}-bes" --global >/dev/null 2>&1 \
      || g compute backend-services create "${svc}-bes" --global \
           --load-balancing-scheme EXTERNAL_MANAGED --protocol HTTPS \
           --enable-logging --logging-sample-rate 1.0
    g compute backend-services describe "${svc}-bes" --global --format='value(backends[].group)' \
      | grep -q "${svc}-neg" \
      || g compute backend-services add-backend "${svc}-bes" --global \
           --network-endpoint-group "${svc}-neg" --network-endpoint-group-region "$REGION"
  done
  echo "Prepared. No traffic has moved."
}

# Replace backend service $1 by $2 everywhere in the url map (exact name only).
swap() {
  local from="$1" to="$2" dir before after
  dir="$(mktemp -d)"
  before="$dir/before.yaml"; after="$dir/after.yaml"
  g compute url-maps export "$URL_MAP" --global --destination "$before" --quiet
  sed -E "s#(/backendServices/)${from}\$#\1${to}#" "$before" > "$after"
  if cmp -s "$before" "$after"; then
    echo "Nothing to change: no route uses ${from}."
    rm -rf "$dir"
    return 0
  fi
  echo "== Changes to ${URL_MAP}:"
  diff "$before" "$after" || true
  cp "$before" "./urlmap-${URL_MAP}-$(date +%Y%m%d-%H%M%S).yaml"   # backup
  # The export carries the fingerprint: a concurrent edit makes this fail
  # instead of being overwritten.
  g compute url-maps import "$URL_MAP" --global --source "$after" --quiet
  rm -rf "$dir"
}

check_health() {
  local svc="$1" host="$2" url code
  url=$(g run services describe "$svc" --region "$REGION" --format='value(status.url)')
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url/health" || true)
  if [ "$code" != "200" ]; then
    echo "::error:: $svc /health on $url answered $code. Not switching." >&2
    exit 1
  fi
  echo "$svc is healthy on $url"
}

wait_public() {
  local host="$1" code
  echo "Waiting for https://${host}/health to answer from Cloud Run (the load balancer takes ~1 min)..."
  for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://${host}/health" || true)
    echo "  HTTP $code"
    [ "$code" = "200" ] && return 0
    sleep 10
  done
  echo "https://${host}/health is not 200 yet. Check it, or roll back." >&2
  return 1
}

cmd="${1:-}"; env="${2:-}"
case "$cmd" in
  prepare) prepare ;;
  switch|rollback)
    info="$(env_info "$env")" || { echo "usage: $0 $cmd staging|production" >&2; exit 2; }
    read -r vm_bes svc host <<<"$info"
    if [ "$cmd" = "switch" ]; then
      check_health "$svc" "$host"
      swap "$vm_bes" "${svc}-bes"
    else
      swap "${svc}-bes" "$vm_bes"
    fi
    wait_public "$host"
    ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
