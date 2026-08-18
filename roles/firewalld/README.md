# firewalld

**This is a best-effort model authored blind.** The live firewalld rules on
warrig and citadel are root-only, and this role was written without `sudo`
access to read them (`ansible.posix.firewalld` needs `become`, but nothing
here ever ran `firewall-cmd --list-all` against the real hosts). Before, or
at the latest during, the first apply, Cam must run:

```bash
sudo firewall-cmd --list-all
```

on both warrig and citadel and reconcile what's here against what's actually
live — this role does **not** know what's already open, only what it thinks
*should* be open based on the repo's own stated intent (SSH access, and the
citadel-only scrape ports from `roles/node_exporter` and `roles/cadvisor`).

`--ask-become-pass` is required — `ansible.posix.firewalld` always needs
`become` on these password-gated hosts.

## Additive only

Every task uses `state: enabled` — nothing in this role ever disables,
removes, or purges a rule, a zone, or a service. It can only ever open ports
that were closed; it cannot close anything the live host already has open,
so it's safe to layer on top of whatever the real (unread) ruleset already
allows.

## The rule model

| rule | port/service | source | hosts |
|------|--------------|--------|-------|
| SSH | `ssh` service, default zone | any | warrig, citadel |
| node_exporter scrape | `9100/tcp`, rich rule | citadel (`192.168.1.88`) | warrig, citadel |
| cadvisor scrape | `8080/tcp`, rich rule | citadel (`192.168.1.88`) | warrig only |

- SSH is left unrestricted by source (matches the intent of `roles/ufw`'s
  rate-limited-but-open SSH rule, adapted to firewalld — no `ufw limit`
  equivalent is used here, since a bare `service: ssh` is the simple option
  and warrig/citadel are already password-gated for `become`).
- `9100` (node_exporter) and `8080` (cadvisor) are scoped to citadel via
  `rich_rule` rather than the plain `port` + `source` params, because
  firewalld's `source` option assigns an entire zone to that source (a
  source-restricted zone) rather than scoping one port to one source — a
  rich rule is the simple way to say "this port, from this source only."
- cadvisor's rule only applies on warrig: citadel's cadvisor container is
  reached over the internal `monitoring_default` docker network, not a
  published host port (see `roles/cadvisor/README.md`), so there's nothing
  to open on citadel for it.
- citadel's IP (`192.168.1.88`) matches `roles/ufw/defaults/main.yml`'s
  `ufw_citadel_ip` — same trusted-scraper source, different enforcement
  mechanism (ufw on the Pis, firewalld on the Arch hosts).

## Usage

```bash
ansible-playbook playbooks/configure_firewalld.yml --ask-become-pass
```

Targets the `firewalld` inventory group (warrig + citadel, in
`inventory/hosts`).

Tunables are documented inline in `defaults/main.yml`. Note ssh is an
unconditional task, separate from the `firewalld_rich_rules` list.
