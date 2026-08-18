# docker_log_rotation

Manages **the whole of `/etc/docker/daemon.json`**, not just the log stanza.

The name is a leftover from when it only set `log-driver: json-file` with a
10m×3 cap. It is kept on purpose — renaming to `docker_daemon` would churn
`playbooks/configure_docker_log_rotation.yml`, the top-level README and any
inventory references, in exchange for nothing. Read "log rotation" as "the
reason the role was born", not "the limit of what it does".

## How it works

`docker_daemon_config` is merged (shallow `combine`) over whatever is already
on disk, then written with `to_nice_json`. Keys you don't mention survive.

The default value reproduces the old behaviour exactly, so the Pis are
unaffected:

```yaml
docker_daemon_config:
  log-driver: json-file
  log-opts:
    max-size: "10m"
    max-file: "3"
```

### First-apply diffs are expected

`to_nice_json` sorts keys and indents with 4 spaces. A host whose live
`daemon.json` was hand-written in another style shows a **formatting-only**
diff on first apply even though the JSON content is identical. Read the diff,
confirm nothing semantic moved, then apply. Observed 2026-08-17:

- **warrig** — live file is already 4-space, key-sorted (it looks to have been
  written by this role at some point) and has **no trailing newline**. Expect a
  one-byte diff.
- **citadel** — live file is 2-space and unsorted. Expect a whole-file
  reformat with no content change.

## Per-host config

The verbatim live configs below are transcribed from each host on 2026-08-17.
Drop them into `inventory/host_vars/<host>.yml` (that directory is not this
role's to edit) and the first apply is a no-op modulo formatting.

`inventory/host_vars/warrig.yml`:

```yaml
docker_daemon_config:
  data-root: /home/docker
  debug: true
  features:
    buildkit: true
  log-driver: loki
  log-opts:
    keep-file: "true"
    loki-batch-size: "400"
    loki-max-backoff: 800ms
    loki-retries: "2"
    loki-timeout: 1s
    loki-url: http://citadel.shadeking.cam.local:3100/loki/api/v1/push
    mode: non-blocking
  runtimes:
    nvidia:
      args: []
      path: nvidia-container-runtime
```

`inventory/host_vars/citadel.yml`:

```yaml
docker_daemon_config:
  debug: true
  log-driver: loki
  log-opts:
    keep-file: "true"
    loki-batch-size: "5000"
    loki-max-backoff: 800ms
    loki-retries: "2"
    loki-timeout: 3s
    loki-url: http://citadel.shadeking.cam.local:3100/loki/api/v1/push
    mode: non-blocking
```

Note the deliberate divergence: warrig batches 400 with a 1s timeout, citadel
batches 5000 with 3s. citadel is the Loki host, warrig ships over the network.
Keep the values as-is unless there's a reason.

## The restart handler is off by default

`docker_daemon_allow_restart: false`.

Restarting dockerd on a loki-logging host risks a deadlock: the log driver
ships to a Loki container supervised by the daemon being restarted, so
containers that cannot flush can wedge the restart. Both warrig and citadel are
in that category.

With the flag false the `Restart docker` handler no-ops and the role prints a
warning that a manual restart is owed. Pick your moment, then:

```sh
sudo systemctl restart docker
```

Set `-e docker_daemon_allow_restart=true` only on hosts where an automatic
bounce is genuinely safe (json-file Pis with nothing critical running).

Separately: log-driver and log-opts changes only affect **newly created**
containers regardless of restarts. Existing ones need
`docker compose up -d --force-recreate`.

## Apply

```sh
# Pis (default target)
ansible-playbook \
    playbooks/configure_docker_log_rotation.yml

# One other host, once its host_vars exist
ansible-playbook -e docker_daemon_target=warrig \
    -e docker_daemon_target=warrig \
    playbooks/configure_docker_log_rotation.yml
```

`docker_daemon_target` defaults to `raspberrypis` — it exists because adding a
group for the Arch docker hosts means editing inventory, which is a separate
change.
