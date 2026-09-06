#!/usr/bin/env bash
# Converge a long-lived worker on :latest, but never mid-render (wanly-gpu-docker#72).
#
# Run from a timer. Does nothing at all unless the published image has actually changed, so
# it is cheap to run often.
#
# WHY IDLE MATTERS MORE THAN FRESHNESS
#     A render is 10-13 minutes (measured: 613s and 759s on the 3090). Recreating the
#     container mid-render throws that work away, and the segment is then reclaimed by the
#     stale-heartbeat path -- so an eager updater costs more than the drift it fixes. This
#     checks the worker is not processing and, if it is, exits and leaves the next run to it.
#
# WHY IT COMPARES DIGESTS, NOT TAGS
#     :latest is a moving pointer. "Am I on latest?" is only answerable by comparing the
#     digest the container was created from against the digest the tag resolves to NOW.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE:-davidjbarnes/wanly-gpu-docker:latest}"
NAME="${NAME:-wanly-ltx}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

if ! docker inspect "$NAME" >/dev/null 2>&1; then
    log "no container named $NAME — creating it"
    exec "$HERE/run-worker.sh"
fi

running_image=$(docker inspect -f '{{.Image}}' "$NAME")

log "pulling $IMAGE"
docker pull -q "$IMAGE" >/dev/null
latest_image=$(docker image inspect "$IMAGE" -f '{{.Id}}')

if [ "$running_image" = "$latest_image" ]; then
    log "already on the published image ($(echo "$latest_image" | cut -c8-19)) — nothing to do"
    exit 0
fi

log "image changed: $(echo "$running_image" | cut -c8-19) -> $(echo "$latest_image" | cut -c8-19)"

# Ask the ENGINE whether a render is in flight, not the GPU. A busy GPU could be
# Automatic1111 on the same box; an idle GPU can still be a claimed segment between diffusion
# stages. The engine knows what it is running, and it is what recreating the container kills.
#
# Asked with `docker exec`, NOT through the published port. The engine binds 127.0.0.1 INSIDE
# the container, so the -p 8190:8190 mapping resolves to nothing and a host-side curl returns
# empty -- which, parsed naively, reads as "idle" and would recreate the container mid-render.
# Verified on the 3090: host curl fails, `docker exec` returns
# {"queue_depth":0,"running":1,...}.
busy=$(docker exec "$NAME" curl -sf --max-time 10 http://127.0.0.1:8190/health 2>/dev/null \
       | python3 -c 'import json,sys;d=json.load(sys.stdin);print((d.get("running") or 0)+(d.get("queue_depth") or 0))' 2>/dev/null || echo unknown)

if [ "$busy" = "unknown" ]; then
    # Fail SAFE: an unreachable engine is not evidence of idleness. It may be mid-boot, and
    # recreating then would interrupt model staging.
    log "could not read the engine's health — assuming busy, will retry next run"
    exit 0
fi
if [ "$busy" != "0" ]; then
    log "engine reports $busy job(s) in flight — leaving it alone, will retry next run"
    exit 0
fi

log "worker is idle — recreating on the new image"
"$HERE/run-worker.sh"
log "done"
