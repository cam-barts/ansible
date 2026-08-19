# cloudflared

Brings citadel's Cloudflare tunnel configuration under version control:
`config.yml` as a byte-faithful template, plus the two files it depends on
(tunnel credentials, origin cert) moved into `ansible-vault`.

`cloudflared` is a **service of the bunkerweb compose project**
(`docker-compose.yml`, see `roles/bunkerweb_stack/`), not a standalone
container. It is the sole inbound path for every one of the lab's ~48
hostnames. Nothing reaches citadel from outside except through this tunnel.

## What the role does

Two things, neither of which touches the container:

1. Renders `templates/config.yml.j2` to
   `{{ cloudflared_config_dir }}/config.yml`, `backup: true`.
2. Deploys the tunnel credentials JSON (0400) and the origin cert (0600)
   from vaulted vars via `ansible.builtin.copy` with `content:` and
   `no_log: true`.

The render is verified byte-identical against the live file with `cmp`; both
secrets are verified byte-identical (by checksum) against the live files
after a full vault round-trip. Applying this role to citadel changes nothing
on disk beyond timestamps.

## What it deliberately does NOT do

### No container action, no handlers

There is no `docker compose restart` / reload of any kind here, and no
handler that would trigger one. Recreating the `bunkerweb` compose project's
containers under citadel's `loki` log driver can deadlock the daemon (see
`roles/bunkerweb_stack/README.md`), and `cloudflared` is one of those
containers. A config change made by this role needs a deliberate,
hand-run `cloudflared` reload, done by hand, ready to unwedge
dockerd if it hangs. This role only ever writes files.

### DNS is documented, not managed

Two wildcard CNAME anchors point at this tunnel, not one:

| Anchor                       | Covers                                              |
| ----------------------------- | ---------------------------------------------------- |
| `warrig-cf.coder.cam`         | `*.coder.cam`, `coder.cam`, `www.coder.cam`           |
| `warrig.shadeking.cam`        | `*.shadeking.cam`, `www`, `*.tldr.cam`                |

Both CNAME to `83250c65-5a20-4a10-9764-b6f9df8fafd1.cfargotunnel.com`,
proxied. `community.general.cloudflare_dns` could manage them, but that would
put two records that change roughly never under automation that gives a bad
run the power to break every hostname in the lab at once. Not a trade worth
making for records this static. Record IDs for rollback live in SilverBullet
under `Projects/Cloudflare Tunnel Migration`.

### The auto-ban pipeline is documented, not managed

BunkerWeb's `[BADBEHAVIOR]` plugin (5 strikes, 24h, backed by the stack's
`redis`) feeds two Grafana alert rules in a "Security Alerts" folder
(`pipeline: autoban`), which trigger the Windmill flow
`f/Cams_Homelab/webhook/auto_ban_ip`, which calls the Cloudflare **IP Access
Rules** API. `f/Cams_Homelab/expire_auto_bans` runs at 03:00 daily and expires
entries on a 30-day TTL.

There is **no Cloudflare API token on disk anywhere in this pipeline. The
credential is a Windmill resource, `u/cam/lab_cloudflare`, resolvable only
inside Windmill. There is therefore nothing for `ansible-vault` to hold here;
the logic itself is live application state split across two apps (Grafana
alert rules, a Windmill flow), not config files this repo touches.

### Two operational risks worth recording

- The Grafana alert rules behind the auto-ban pipeline are deployed by
  `docker cp` + restart, not IaC. They are unversioned and drift-prone, and
  nothing here or in Grafana's own provisioning would catch a rule silently
  changing or disappearing.
- There is **one `cloudflared` container with no replica**. It is a single
  point of failure for external access to the entire lab; there is no
  failover tunnel.

### Watchtower keeps the image current, on purpose

`cloudflared` is one of the containers Watchtower deliberately keeps current
(`cloudflare/cloudflared:latest`) for security fixes, same as the rest of the
bunkerweb stack. That means a pending recreate can exist at any moment,
through no action of this role or Ansible generally. See
`roles/bunkerweb_stack/README.md` for why that's expected and why the guard
there exists.

## Secrets

Two values are templated rather than committed, both no-default in
`defaults/main.yml` on purpose: applying this role to a host without the
vault should fail on an undefined variable, not deploy an empty secret:

| Template variable              | Deployed as                                            | Mode |
| ------------------------------- | ------------------------------------------------------- | ---- |
| `cloudflared_credentials_json`  | `{{ cloudflared_config_dir }}/citadel-bunkerweb.json`    | 0400 |
| `cloudflared_cert_pem`          | `{{ cloudflared_config_dir }}/cert.pem`                  | 0600 |

Both are defined in `inventory/host_vars/citadel/vars.yml` as
non-vault-prefixed passthroughs to `inventory/host_vars/citadel/vault.yml`
(the same file that holds the bunkerweb_stack secrets), same pattern as
`inventory/group_vars/synology/`:

```bash
ansible-vault view inventory/host_vars/citadel/vault.yml
```

`cert.pem` (despite the name, an Argo Tunnel login token, not an X.509 cert)
is only used by `cloudflared tunnel login`; it is not read at runtime by the
running tunnel. It is deployed anyway because that's how the live host has
it, and losing it would block re-authenticating the tunnel by hand later.

## Byte-fidelity

`templates/config.yml.j2` is the live `config.yml` with two substitutions
(the tunnel ID and the credentials-file path) and nothing else. That includes
every inline comment explaining *why* (HTTPS:8443 not 8080 to avoid a
redirect loop; `noTLSVerify` because the leg is transport-only). Do not
"clean up" the template.
