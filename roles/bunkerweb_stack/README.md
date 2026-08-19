# bunkerweb_stack

Brings citadel's BunkerWeb compose project under version control as a
byte-faithful template, with its three plaintext secrets moved into
`ansible-vault`.

The stack is the sole ingress for every proxied hostname in the lab (~48), and
the Cloudflare tunnel (`cloudflared`) is a service *inside the same compose
project*, so losing it takes external access down with it.

## What the role does

Two things:

1. Renders `templates/docker-compose.yml.j2` to
   `{{ bunkerweb_stack_dir }}/docker-compose.yml` with `backup: true`.
2. Runs the guard described below.

That is the whole role. It reproduces the live file byte for byte. The render
is verified with `cmp` against a capture of the running host's file, and the
seven per-service hashes from `docker compose config --hash="*"` are verified
against the `com.docker.compose.config-hash` labels on the live containers.
Applying this role to citadel changes nothing.

## What the role deliberately does NOT do

### It does not run `docker compose up`

There is no `community.docker.docker_compose_v2` task here, and adding one
would be a mistake. citadel's dockerd defaults to the `loki` log driver, where
tearing a container down deadlocks the daemon: the play would not fail, it
would hang, and so would anything else touching those containers afterwards.
Reconciling this stack is a deliberate, human-triggered, be-ready-to-unwedge
operation. Ansible's job here stops at the file.

### It does not manage the 48 vhosts

Under 10% of BunkerWeb's effective configuration is in the compose file. The
per-site configuration lives in MariaDB, in `bw_services`,
`bw_services_settings` and `bw_custom_configs`. The instance container proves
it:

```console
$ docker inspect -f '{{json .Mounts}}' bunkerweb-bunkerweb-1
[]
```

No mounts at all. `/data/configs` inside the
`bw-storage` volume looks like a config tree but is a *projection* the
scheduler writes out of the database. It is a cache, not a source. Editing it
is pointless; it gets overwritten. Site config is changed through the UI/API,
and it is captured by the `bunkerweb-mariadb` dump in the `borgmatic` role, not
by this template.

### It does not manage the anonymous volumes

`bw-api` and `bw-ui` each carry an anonymous volume at `/data`, created from
`VOLUME /data` in their images. They have hash-named volume IDs, they do not
appear anywhere in the compose file, and nothing here creates, names or backs
them up. Note that recreating those two services orphans the current pair.

### It does not manage the cloudflared credentials

`/home/nux/docker/cloudflared/` holds `cert.pem`, `config.yml` and the tunnel
credentials JSON, bind-mounted read-only into the `cloudflared` service. If
those are ever brought under Ansible it must be as path, owner and mode only,
never content. The credentials file is issued by Cloudflare and templating it
would mean committing the tunnel secret; a wrong byte in it silently drops
external access to everything.

## The guard

`community.docker.docker_compose_v2` in `check_mode`, which resolves to
`docker compose up --dry-run`: compose prints the plan and exits without
touching the daemon. If it reports any work to do, the role fails with the list
rather than letting a later run carry it out.

Two details that are easy to get wrong:

- The condition keys on `.actions`, **not** `.changed`. The dry-run task sets
  `changed_when: false` so it never pollutes the play recap, and that also
  forces `.changed` to `False` even when compose has work queued. Measured: a
  perturbed compose file produced `changed=False` with `actions=8`. Gating on
  `.changed` is a guard that can never fire, and it looks correct.
- `| bool` on every read of `bunkerweb_allow_recreate`. Extra vars arrive as
  strings and every non-empty string is truthy, so a bare truth test would treat
  `-e bunkerweb_allow_recreate=false` as "yes, disarm the guard". Same trap and
  same handling as `roles/cadvisor`.

Unlike `roles/cadvisor` this is not gated on the `loki` log driver. The deadlock
is only half the reason to refuse; the other half is that recreating anything in
this project drops the Cloudflare tunnel and every proxied hostname, which is
true on any log driver. Refusing unconditionally is simpler and safer.

This replaced a hand-rolled version that shelled out to `docker compose config
--hash="*"` and diffed the hashes against the containers' labels in Jinja. It
computed the right answer, but it reimplemented what the module already does in
check mode, in about 35 more lines.

### Why a recreate can be pending when nothing here changed

Watchtower deliberately keeps `cloudflare/cloudflared:latest` current, so that
service gets security fixes without anyone touching this repo. Every such
update rewrites the container's `com.docker.compose.image` label out-of-band,
and other services drift the same way whenever the stack is touched by hand.
The compose file and the running containers can therefore disagree at any
moment, through no action of Ansible's, which is why the guard
exists and why the role refuses rather than reconciles.

## Secrets

Three values are templated rather than committed:

| Template variable        | Where it appears in the rendered file            |
| ------------------------ | ------------------------------------------------ |
| `bunkerweb_db_password`  | `MYSQL_PASSWORD` on `bw-db`, **and** embedded in the `DATABASE_URI` connection string on the `x-bw-env` anchor |
| `bunkerweb_api_password` | `API_PASSWORD` on `bw-api`                       |

`DATABASE_URI` is a single URI string with the password inside it
(`mariadb+pymysql://bunkerweb:<password>@bw-db:3306/db`), so the template
reconstructs the URI around the variable rather than substituting the whole
value. The two occurrences are the same secret and must stay that way: a
mismatch between them means BunkerWeb cannot reach its database.

Both are defined in `inventory/host_vars/citadel/vars.yml` as non-vault-prefixed
passthroughs to `inventory/host_vars/citadel/vault.yml`, the same pattern as
`inventory/group_vars/synology/`. Read them with:

```bash
ansible-vault view inventory/host_vars/citadel/vault.yml
```

Neither has a default in `defaults/main.yml`, on purpose: applying this role to
a host without the vault should fail on an undefined variable, not render a
compose file with an empty password.

The rendered file is `0640 nux:nux`. It was `0644` to match the live host, which
meant the MariaDB password was world-readable on citadel; tightened once the
template existed to keep it that way. `docker compose` reads the file as the
invoking user (`nux`, who owns it), so nothing needed the world bit. File mode is
not part of the config hash, so the change recreated nothing. The template task
also sets `diff: false` so `--diff` cannot print the secrets to the terminal.

## Byte-fidelity

`templates/docker-compose.yml.j2` is the live file with three substitutions and
nothing else. That includes the things a formatter would "fix": the `x-bw-env`
YAML anchor, every inline comment, and the two-space indent. Do not "clean up"
the template. Every byte is load-bearing, because a difference compose can see
is a container recreate, and recreating this stack drops the Cloudflare tunnel
and all 48 vhosts.

The live file originally also carried trailing whitespace on the blank line
between `bunkerweb` and `bw-scheduler`, a trailing space after the commented-out
`FORWARDED_ALLOW_IPS`, and a trailing blank line at EOF. Those four bytes were
removed from the live file on citadel (backup:
`docker-compose.yml.bak-prews-*`) after verifying the change was config-inert:
all seven `docker compose config --hash` values were byte-identical before and
after, because none of the whitespace sat inside a value or inside the `redis`
`command: >` block scalar.

That was worth doing: the alternative was permanently excluding this path from
the `trailing-whitespace` and `end-of-file-fixer` pre-commit hooks, which meant
the byte-fidelity guarantee was one `.pre-commit-config.yaml` edit away from
being silently lost. There are now no exclusions. If someone needs to re-add
one, the live file has drifted back and that is the thing to fix.

## Usage

There is no playbook. This role is not wired into one yet. Wire it up only
alongside a plan for how the stack gets reconciled afterwards.
