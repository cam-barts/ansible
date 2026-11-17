# Observability Phase 5: Docker logs from the loki plugin to Alloy

Hosts: citadel first (BunkerWeb last), then warrig. Everything here runs in a
maintenance window. Design and dry run done 2026-11-14. EXECUTED 2026-11-16 on both
hosts, plugin removed the same day (BunkerWeb cold start on citadel cost ~9 min of
public 502s: pause sockpuppetbrowser and windmill_worker_reports first next time).

## What changes

| Piece | Before | After |
|---|---|---|
| dockerd default | `log-driver: loki` + `keep-file`, `loki-*` opts | `log-driver: json-file`, same `max-size`/`max-file` |
| Shipper | loki plugin, per container | `alloy-journal` stack (`config.alloy` docker section) |
| `host` label | `Warrig` / `citadel` | `warrig` / `citadel` |
| `filename` label | `/var/log/docker/<id>/json.log` | not shipped |
| other labels | `compose_project`, `compose_service`, `container_name`, `source`, auto `service_name` | identical (verified in the dry run) |
| sockpuppetbrowser drops | `loki-pipeline-stages` in its compose file | `stage.match` in `config.alloy` |

Repo diff (uncommitted):

- `roles/compose_stack/files/warrig/alloy-journal/config.alloy`: the docker
  pipeline (discovery.docker -> discovery.relabel -> local.file_match ->
  loki.source.file -> loki.process -> existing loki.write).
- `roles/compose_stack/files/warrig/alloy-journal/docker-compose.yml`: mounts
  `/var/run/docker.sock:ro` and `/var/lib/docker/containers:ro`. Positions
  live under `--storage.path`, i.e. the existing `alloy-data` volume.
- `roles/compose_stack/files/citadel/changedetection/docker-compose.yml`:
  sockpuppetbrowser moves to explicit `json-file` 20m x 3.
- `roles/compose_stack/{defaults,tasks}/main.yml`: new `compose_stack_recreate`
  (default `auto`, so no behaviour change) passed to docker_compose_v2 `recreate`.

Deliberately NOT in the diff: the `docker_daemon_config` edit in host_vars and
the `cadvisor_extra_args` removal. Both are in the steps below. Keeping
host_vars on loki until the window means an unrelated role run plus a reboot
cannot flip the driver early.

### Why Alloy reads files, not the Docker API

- `discovery.docker` has no log-driver meta label. Verified on v1.21.3 by
  dumping the component's targets: only `container_id/name/network_mode`,
  `container_label_*`, `network_*`, `port_*`.
- `loki.source.docker` reads loki-driver containers fine (dual logging /
  keep-file), backfill included. Dry run: 295 duplicate lines from
  `hoppscotch-hoppscotch-1` in under a minute. So "it will fail on loki
  containers" is false.
- `<id>-json.log` exists only for json-file containers. A loki-driver
  container's dir has no log file and `docker inspect` shows an empty
  `LogPath`. local.file_match passes the missing paths through but
  loki.source.file opens nothing for them, silently (dry run: 63 loki
  containers discovered, `loki_source_file_files_active_total` = 5 = exactly
  the 5 json-file containers, zero warnings).

So no opt-in label and no per-container list: a container gets shipped by
Alloy the moment it is (re)created on json-file, and never before.

### Things that start shipping that do not ship today

The containers already on json-file are not in Loki today. Alloy picks them
up on first start and backfills up to their `max-size x max-file`. Loki has
`reject_old_samples: false`, so the backfill lands with original timestamps.

- citadel: `loki` (the Loki container itself). It was put on json-file to
  stop it pushing into itself through the plugin. Going through Alloy has no
  dockerd-restart deadlock, and Loki does not log per push at its level, so
  no feedback loop. **Approve, or I add a drop for `container_name="loki"`.**
- warrig: `minuspod`, `parakeet`, `silverbullet-podcast-indexer-1`.
  podcast-indexer's json log has torn lines from 2026-10-19 (truncated while
  docker was writing). stage.docker cannot parse them, so they ship raw and
  without a `source` label. One-time, from the backfill only.

No other drops were added.

### Open question: journal host label

The journal pipeline still sets `host` from `__journal__hostname`, i.e.
`Warrig`. After cutover warrig has `host="warrig"` for containers and
`host="Warrig"` for host-syslog/auth/kernel. To make it consistent, add to
`loki.relabel "journal"` after the hostname rule:

```alloy
rule {
  source_labels = ["host"]
  target_label  = "host"
  action        = "lowercase"
}
```

Not done because the decision covered container logs. Say if you want it.

## Facts this runbook leans on

Verified:

- dockerd reload (`SIGHUP`, `systemctl reload docker`) does NOT apply
  `log-driver`/`log-opts`. The Docker reference's reload table lists only
  debug, labels, live-restore, max-concurrent-*, max-download-attempts,
  default-runtime, default-stop-timeout, runtimes, authorization-plugin,
  insecure-registries, registry-mirrors, shutdown-timeout, features. So a
  full `systemctl restart docker` is required.
- Log driver binds at container creation. A restarted container keeps its
  driver. docker_compose_v2 `state: restarted` is literally
  `docker compose restart`, so `-e compose_stack_state=restarted` does
  nothing for this. `recreate: auto` does not fire either: a daemon default
  is not a compose config diff. Hence `-e compose_stack_recreate=always`.
- `docker_daemon_config` is shallow-combined: the host_vars `log-opts` dict
  replaces the live one wholesale, so omitting the `loki-*` keys removes them.
- Alloy's container sees the host's hostname (host network): `citadel`
  on citadel (checked), `Warrig` on warrig, lowercased by the config.
- Lines keep the container's own timestamp to the nanosecond, and a restart
  of Alloy resumes from the position with no gap and no duplicate (dry run).

Assumed, check during the window:

- That dockerd stops loki-driver containers cleanly on restart. The
  docker_log_rotation README flags a deadlock risk when the plugin cannot
  flush. `mode: non-blocking`, `loki-retries: 2`, `loki-timeout` 1s/3s should
  bound it. Watch `journalctl -fu docker` and have a second shell.
- That compose recreates (stop then rm) of loki-driver containers do not
  wedge. The compose_stack role relies on this already (tasks/main.yml).

## 0. Pre-flight (either host, any time before the window)

```sh
cd ~/git_local/ansible
ansible-playbook --check --diff playbooks/configure_compose_alloy_journal.yml
docker plugin ls                       # loki:latest enabled, keep it until step 6
docker ps -a --filter status=exited --format '{{.Names}}'   # stopped loki users block plugin removal later
```

On warrig `bechdel-exports` (not compose, exited) is still on the loki
driver: `docker rm` it or recreate it before step 6.

## 1. Deploy the Alloy docker pipeline (both hosts, safe before the window)

Only json-file containers get tailed, so this ships nothing twice.

```sh
ansible-playbook playbooks/configure_compose_alloy_journal.yml --limit citadel
ansible-playbook playbooks/configure_compose_alloy_journal.yml --limit warrig
```

The compose file changed, so this recreates `alloy-journal`. Verify:

```sh
docker logs alloy-journal 2>&1 | rg 'level=(error|warn)'      # expect nothing
curl -s 127.0.0.1:12345/metrics | rg '^loki_source_file_files_active_total'
# citadel expects 1 (loki), warrig 3 (minuspod, parakeet, podcast-indexer)
```

```logql
sum by (host, container_name, source) (count_over_time({host=~"citadel|warrig", compose_project=~".+", filename=""}[5m]))
```

## 2. Switch the citadel daemon default

`inventory/host_vars/citadel/vars.yml`, replace the `log-driver`/`log-opts` keys:

```yaml
  log-driver: json-file
  log-opts:
    max-size: "20m"
    max-file: "3"
```

(`keep-file` and every `loki-*` key must go: they are plugin options, and
json-file validates its opts, so leaving one in makes dockerd refuse to
start. `mode` is a generic option valid for every driver; dropping it means
the default `blocking`, keeping `mode: non-blocking` is also fine.)

```sh
ansible-playbook --check --diff -e docker_daemon_target=citadel playbooks/configure_docker_log_rotation.yml
ansible-playbook -e docker_daemon_target=citadel playbooks/configure_docker_log_rotation.yml
ssh citadel sudo systemctl restart docker      # NOT reload, see above
ssh citadel "docker info -f '{{.LoggingDriver}}'"   # json-file
```

Every container comes back via its restart policy, still on the loki driver
(created before the switch). The plugin must stay enabled for that.

## 3. Recreate citadel stacks, BunkerWeb last

One at a time, verify (step 5) before the next:

```sh
ansible-playbook playbooks/configure_compose_alloy_journal.yml --limit citadel -e compose_stack_recreate=always
ansible-playbook playbooks/configure_compose_monitoring.yml       -e compose_stack_recreate=always
ansible-playbook playbooks/configure_compose_changedetection.yml  -e compose_stack_recreate=always
ansible-playbook playbooks/configure_compose_searxng_docker.yml   -e compose_stack_recreate=always
ansible-playbook playbooks/configure_compose_taskwarrior.yml      -e compose_stack_recreate=always
ansible-playbook playbooks/configure_compose_windmill.yml         -e compose_stack_recreate=always
ansible-playbook playbooks/configure_compose_portainer.yml --limit citadel -e compose_stack_recreate=always
# cAdvisor on citadel: step 7
# BunkerWeb has no play (ingress outage of a few seconds):
ssh citadel 'cd ~/docker/bunkerweb && docker compose up -d --force-recreate'
```

Confirm nothing on citadel is left on the plugin:

```sh
ssh citadel "docker ps -aq | xargs docker inspect -f '{{.HostConfig.LogConfig.Type}} {{.Name}}' | rg -v '^json-file'"
```

## 4. warrig

Same as step 2 with `inventory/host_vars/warrig/vars.yml` (`max-size: "50m"`,
`max-file: "3"`; keep `features`, `runtimes`, `debug`) and
`-e docker_daemon_target=warrig`, then `sudo systemctl restart docker`
(about 14 s on the SSD data-root).

Recreate with a play where one exists:

```sh
for s in alloy_journal ansible_semaphore atuin babybuddy bin blaze cyberchef dashy excalidraw \
         flatnotes hoarder hoppscotch immich jellyfin kutt metube morss ollama_gpt pinchflat \
         pwndrop silverbullet speedtest string_is; do
  ansible-playbook playbooks/configure_compose_$s.yml --limit warrig -e compose_stack_recreate=always || break
done
ansible-playbook playbooks/configure_compose_portainer_agent.yml --limit warrig -e compose_stack_recreate=always
```

Stacks with no play, in their project dirs:

```sh
for d in actual ghostfolio media-automation minuspod moodle opnform parakeet void-datasette webhook/webhook-tester; do
  (cd ~/docker_services/$d && docker compose up -d --force-recreate) || break
done
```

(minuspod, parakeet and silverbullet already ship via Alloy; recreating them
is harmless.) Then the same "nothing left on the plugin" check, plus cAdvisor
(step 7).

## 5. Verification queries (per stack, then per host)

Plugin streams carry `filename` (and `host="Warrig"` on warrig); Alloy
streams carry neither. That is the duplicate discriminator.

Labels look right (expect lowercase host, `source` on every stream):

```logql
sum by (host, compose_project, compose_service, container_name, source) (count_over_time({compose_project="<stack>", filename=""}[5m]))
```

The plugin has stopped for the stack (expect empty a minute after recreate):

```logql
sum by (container_name) (count_over_time({compose_project="<stack>", filename=~"/var/log/docker/.+"}[2m]))
```

Duplicate detection: both of the two queries above non-zero for the same
`container_name` over the same recent 2m window, past the recreate moment,
is double shipping. Line-level spot check (works once json-file):

```sh
docker logs --since 5m <container> 2>&1 | wc -l
```
```logql
sum(count_over_time({container_name="<container>"}[5m]))
```

Dashboards/rules the contract protects:

```logql
sum by (source) (count_over_time({compose_service="bunkerweb", source="stdout"}[5m]))
{container_name="bunkerweb-bunkerweb-1"}
sum by (vhost) (count_over_time({compose_service="bunkerweb", source="stdout"} | pattern `<vhost> <_>` [5m]))
```

Plus the Loki ruler's BunkerWeb recording rules still producing in Prometheus.

sockpuppetbrowser drops (expect 0):

```logql
sum(count_over_time({compose_service="sockpuppetbrowser"} |~ "Failed to connect to the bus|_request_retry" [15m]))
```

End state, per host (expect empty):

```logql
sum by (host) (count_over_time({host=~"citadel|Warrig", filename=~"/var/log/docker/.+"}[10m]))
```

Dashboards with a literal `host="Warrig"` or a `$host` variable built from
Loki need checking after warrig (journal streams still say `Warrig`, see the
open question).

## Rollback

- Alloy pipeline only (step 1): `git checkout` the two alloy-journal files,
  rerun the play. Nothing else depended on it.
- Daemon: restore the host_vars block (git), rerun docker_log_rotation,
  `systemctl restart docker`, recreate the stacks already moved
  (`-e compose_stack_recreate=always`). Remove the Alloy docker section first
  or at the same time, otherwise recreated stacks are on loki again and Alloy
  sees nothing for them (no dupes, just back to the plugin).
- Never remove the plugin (step 6) until both hosts have been stable for a day.

## 6. Remove the plugin (after both hosts verified, ideally a day later)

Per host. Every container, including stopped ones, must be off it:

```sh
docker ps -aq | xargs docker inspect -f '{{.HostConfig.LogConfig.Type}} {{.Name}}' | rg '^loki'   # must be empty
```

Reclaim the keep-file copies first, by truncation, never rm (the plugin can
hold fds; rm frees nothing until it exits):

```sh
P=$(docker plugin inspect -f '{{.Id}}' loki:latest)
sudo du -xhd1 /var/lib/docker/plugins/$P/rootfs/var/log/docker | sort -rh | head
sudo fd -t f -g 'json.log*' /var/lib/docker/plugins/$P/rootfs/var/log/docker -x truncate -s 0
```

Then:

```sh
docker plugin disable loki:latest
docker plugin rm loki:latest
```

## 7. cAdvisor

Both become ordinary recreates once `docker info` says json-file, because the
role's guard only refuses on `LoggingDriver == loki`.

warrig: delete the `cadvisor_extra_args: []` line (and its comment) from
`inventory/host_vars/warrig/vars.yml`, together with the ghcr.io image bump
noted there, then:

```sh
ansible-playbook --check --diff playbooks/deploy_cadvisor.yml --limit warrig --ask-become-pass
ansible-playbook playbooks/deploy_cadvisor.yml --limit warrig --ask-become-pass
```

citadel: add it under `[cadvisor]` in `inventory/hosts` (the vars in
`host_vars/citadel/vars.yml` already match the live container: no port,
`monitoring_default`, 512m), update the comment above the group, then the
same check/apply with `--limit citadel`. Prometheus scrapes it by container
name, so confirm `up{job=~".*cadvisor.*", instance=~".*citadel.*"}` stays 1.
