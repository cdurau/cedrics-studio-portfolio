# Trusted deploy components (`studio-deploy`)

Root-owned deploy logic that runs **outside** every agent-writable repository.

## Why this directory exists

Gate review 2026-09-20, second round — finding 1 (KRITISCH). The first
remediation kept the deploy gate inside the runtime repository
(`apps/<slug>/scripts/deploy-gate.sh`). That repository is written by the deploy
agent, so a commit there decided what ran next to the owner-migration
credential, the Coolify token and the Docker socket — and a migration image
built from that same repository ran with `MIGRATION_DATABASE_URL`. Either one
re-flips the authority boundary S0 exists to draw.

The intended structure is:

```
GitHub → SSH forced command → root-owned trusted deploy gate → runtime repo only as build/source input
```

So the trusted logic lives here and the runtime repository is demoted to input.

## Layout

| Path | Installed as | Purpose |
| --- | --- | --- |
| `deploy-hook.sh` | `/usr/local/bin/<slug>-deploy-hook` (root:wheel 0755) | SSH forced-command entry point; accepts exactly one 40-char lowercase-hex SHA |
| `deploy-gate.sh` | `/usr/local/libexec/studio-deploy/<slug>-deploy-gate` (root:wheel 0755) | The whole deploy: ref binding → approved migration → trigger → poll → verify app+commit+health |
| `migrate-runner.sh` | `/usr/local/libexec/studio-deploy/<slug>-migrate-runner` (root:wheel 0755) | Applies owner migrations, but only content the operator approved |
| `print-approvals.sh` | (run from the checkout, not installed) | Prints the exact approval lines for a given checkout |
| `approved-migrations/<slug>.txt` | `/usr/local/libexec/studio-deploy/approved-migrations/<slug>.txt` (root:wheel 0644) | The approved migration set: `<tag> <sha256>` per line |

## The owner-migration boundary (fail-closed)

"Only approved hashes" is the chosen mechanism, in preference to a signed
migration artifact or a second review pipeline: it is verifiable offline, needs
no new key material, and the failure mode is a refused deploy rather than an
executed one.

- The runner validates the **whole** migration set — every journal entry present,
  every content hash listed — **before** the first byte reaches the database.
  A refused set has therefore touched nothing.
- A new migration does not run with owner rights until a human has added its
  tag and the sha256 of its exact content. Adding a migration, editing one, or
  swapping in an older body all change the hash and are refused.
- The runner builds no image from the checkout: it uses the pinned postgres
  image for `psql`, so **no agent-authored binary ever holds the owner
  credential**.
- `migrate.ts` semantics are preserved (same journal table, same
  hash-mismatch-is-fatal rule), so an existing deployment upgrades in place.

This deliberately does not remove the credential from the host — it removes the
agent's ability to decide what runs with it. The general runtime repo may ship
whatever code it likes; only reviewed SQL reaches owner rights.

## Installation (owner step — needs root on the host)

```sh
# 1. trusted components
sudo install -d -m 0755 /usr/local/libexec/studio-deploy
sudo install -m 0755 -o root -g wheel deploy-gate.sh   /usr/local/libexec/studio-deploy/<slug>-deploy-gate
sudo install -m 0755 -o root -g wheel migrate-runner.sh /usr/local/libexec/studio-deploy/<slug>-migrate-runner
sudo install -m 0755 -o root -g wheel deploy-hook.sh   /usr/local/bin/<slug>-deploy-hook

# 2. approvals (review the SQL diff first!)
sudo install -d -m 0755 /usr/local/libexec/studio-deploy/approved-migrations
#   from a checkout of the commit you intend to deploy:
#   ./platform/studio-deploy/print-approvals.sh <checkout> <slug> | sudo tee \
#     /usr/local/libexec/studio-deploy/approved-migrations/<slug>.txt
sudo chown root:wheel /usr/local/libexec/studio-deploy/approved-migrations/<slug>.txt
sudo chmod 0644     /usr/local/libexec/studio-deploy/approved-migrations/<slug>.txt

# 3. forced command (single line) in ~/.ssh/authorized_keys
#   command="/usr/local/bin/<slug>-deploy-hook",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... ci@github
```

The gate refuses to run if a trusted component is missing or is group/other
writable, and — on the production path — if it is not root-owned.

## Verification (no live mutation)

All of these run locally; none deploys, migrates or contacts the network.

```sh
bash tests/ref-binding.sh        # the commit must be the tip of the allowed ref
bash tests/verify-binding.sh     # health check bound to app + full commit + state
bash tests/hook-validation.sh    # only one full sha reaches the gate (injections)
bash tests/migration-approval.sh # unapproved/edited SQL never reaches the DB
```

## Open items

- Installation on the host needs root (`sudo`), which the agent does not hold —
  this remains an owner step.
- The CI key pair and `authorized_keys` entry are owner steps too.
- `VERIFY_ONLY=1 <gate> <full-sha>` answers "is what is running actually this
  app at this commit?" at any time, reads no credential, and mutates nothing.
