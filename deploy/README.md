# Deploying a long-lived worker

For a box we own and keep running — today the 3090. RunPod pods do not use any of this: the
API creates them from `:latest` with `runpod_client.worker_env()`, so they are current by
construction.

## Why this directory exists

There are two independent update channels:

| what | updates on |
|---|---|
| the daemon | container **restart** — `start.sh` clones `wanly-gpu-daemon` from `main` at boot |
| `start.sh`, `download_models.sh`, the engine | image **pull + recreate** |

A pod is recreated every time it launches, so it gets both. A long-lived container only ever
gets the first, because `docker restart` reuses its image by design. That is how the 3090 came
to run a 37-hour-old image and a 14-hour-old daemon while a pod ran current code, and produce
a different result from the same queue (#72).

## First install

```bash
git clone https://github.com/DavidJBarnes/wanly-gpu-docker.git ~/wanly-gpu-docker
cd ~/wanly-gpu-docker/deploy
cp worker.env.example worker.env
$EDITOR worker.env          # QUEUE_API_KEY and the host paths
./run-worker.sh
```

## Keeping it current

```bash
sudo cp wanly-worker-update.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wanly-worker-update.timer
```

`update-worker.sh` pulls `:latest`, compares its digest against the digest the running
container was created from, and recreates only if they differ **and** the engine reports
nothing in flight. It exits 0 when it decides not to act, so a non-zero exit in the journal is
a real failure.

Check on it with:

```bash
systemctl list-timers wanly-worker-update.timer
journalctl -u wanly-worker-update.service -n 50
```

## Rollback

`run-worker.sh` honours `IMAGE`, so pinning an older build is:

```bash
IMAGE=davidjbarnes/wanly-gpu-docker:<sha> ./run-worker.sh
```

Re-enable the timer afterwards or it will pull `:latest` back over the pin at the next tick.

## What it will never do

Recreate mid-render. A render is 10-13 minutes and interrupting one loses the work and leaves
the segment to be reclaimed by the stale-heartbeat path — costing more than the drift it fixes.
An unreachable engine counts as busy, because a box that is mid-boot must not be interrupted
either.
