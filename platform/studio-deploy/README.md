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

## The production trust path is literal (third round, finding A)

This is the rule that makes the rest of the design hold, so it is stated first:

> **No environment variable influences any trust decision.**

Every path, binary, image and bound the components use is a placeholder
(`__APP_SLUG__`, `__TRUST_DIR__`, `__STATE_DIR__`, `__GATE_REPO__`,
`__DOCKER_BIN__`, `__PSQL_IMAGE__`, `__COOLIFY_API__`, the three timeouts) that
the installing owner substitutes **exactly once**. The installed file reads no
override; `deploy-gate.sh`, `deploy-hook.sh` and `migrate-runner.sh` each refuse
to run while a placeholder is still present, so an unsubstituted copy fails loudly
instead of behaving like a production component.

Why it had to change: the previous version honoured `TRUST_DIR`,
`MIGRATE_RUNNER`, `APPROVALS_FILE`, `DOCKER_BIN`, `DOCKER_CONTEXT`, `PSQL_IMAGE`,
`DB_NETWORK`, `STATE_DIR`, `GATE_REPO`, `VERIFY_ONLY`, `REF_CHECK_ONLY` … and the
migration runner reads the owner credential, exports `PGPASSWORD` and then starts
a container. A caller able to set the environment could therefore point
`MIGRATE_RUNNER` at a script it wrote itself, or `PSQL_IMAGE` at an image it
controlled — and that code would hold the owner credential. A caller able to set
`VERIFY_ONLY` could silently downgrade a real deploy to a no-op. That is a
credential-authority problem, not a file-integrity one.

Three further consequences, all deliberate:

- **Modes are arguments, not variables.** `--verify-only` and `--ref-check`
  replaced `VERIFY_ONLY=1` / `REF_CHECK_ONLY=1`.
- **An override is reported, never silently ignored.** A run that sees a
  trust-relevant variable in its environment logs that it is ignoring it (names
  only — a value could itself be sensitive). "Nothing happened" must not be
  confusable with "the override worked".
- **The runner pins its call shape.** It acts only when called with its installed
  env-file and approvals path, and only for a migrations directory below the
  installed clone. A direct call with a caller-chosen env-file, approvals list or
  tree is refused *before any credential is read*.

The test suites no longer set environment variables on these scripts at all. They
build a **substituted copy** (`tests/lib/harness.sh`) and run that, so test
injection is a property of the copy and cannot be activated on the production path
by any caller, variable or flag.

### What is still caller-influenced

Honest inventory for the production path: the commit SHA (the sole argument, and
validated), the invoking user's `SSH_ORIGINAL_COMMAND` (validated), and the
contents of the host-side state directory (mode 600, owner-only). Nothing else —
see § "Verification" for the negative tests that assert this.

## Layout

| Path | Installed as | Purpose |
| --- | --- | --- |
| `deploy-hook.sh` | `/usr/local/bin/<slug>-deploy-hook` (root:wheel 0755) | SSH forced-command entry point; accepts exactly one 40-char lowercase-hex SHA |
| `deploy-gate.sh` | `/usr/local/libexec/studio-deploy/<slug>-deploy-gate` (root:wheel 0755) | The whole deploy: ref binding → approval binding → approved migration → trigger → poll → verify app+commit+health |
| `migrate-runner.sh` | `/usr/local/libexec/studio-deploy/<slug>-migrate-runner` (root:wheel 0755) | Applies owner migrations, but only content the operator approved, and only from the installed paths |
| `find-approvals.sh` | (run from the checkout, not installed) | Reviews a migration set: tag, file and hash side by side |
| `verify-approvals.sh` | (run from the checkout, not installed) | Independent check of a proposed approval list (recomputes hashes with `/usr/bin/shasum`) |
| `print-approvals.sh` | (run from the checkout, not installed) | Convenience wrapper over `find-approvals.sh --for-install` |
| `approved-migrations/<slug>.txt` | `/usr/local/libexec/studio-deploy/approved-migrations/<slug>.txt` (root:wheel 0644) | The approved migration set: `# commit <sha>` plus `<tag> <sha256>` per line |

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

## The approval chain does not terminate in agent-written code (third round, finding C)

The old step was `print-approvals.sh <checkout> <slug> | sudo tee …`.
`print-approvals.sh` lives in this **agent-writable** repository, so its output
could not stand for a human's judgement: whatever the agent wrote into that script
would silently *become* the approved hashes that owner rights obey. A hash
produced by agent-controlled code is not evidence of a human decision.

Two independent things now hold the chain:

1. **The human verifies the list without trusting project code.**
   `verify-approvals.sh` recomputes every hash from the files on disk with the
   system tool (`/usr/bin/shasum`, root-owned on macOS), compares them against the
   proposed list and exits non-zero on any mismatch. The equivalent by-hand check
   is one command — `(cd <migrations> && /usr/bin/shasum -a 256 *.sql)` — which is
   the whole point: the human never has to believe a hash that agent code printed.
2. **The gate enforces the commit binding.** The approval file must carry a
   `# commit <full-sha>` line, and `deploy-gate.sh` refuses a list whose commit is
   not the commit being deployed. The file is root-owned, so a same-host agent
   cannot strip the line. This closes the hole a helper-script-only check leaves:
   skipping the helper does not skip the binding, because the binding lives inside
   the artifact the gate consumes.

The gate also re-verifies the approval list's **content hashes** through the
runner on every deploy, so an approval is never a one-time check.

Owner procedure (the whole of it):

```sh
# 1. review: which commit, and what changed
git -C <checkout> log -1 --format='%H %s'
git -C <checkout> diff <base>..HEAD -- apps/<slug>/drizzle/migrations

# 2. print tag, file and hash side by side and READ THE SQL
./platform/studio-deploy/find-approvals.sh <checkout> <slug>

# 3. write the proposed list (carries the "# commit" line)
./platform/studio-deploy/find-approvals.sh <checkout> <slug> --for-install > /tmp/proposed.txt

# 4. independent verification — recomputes hashes with /usr/bin/shasum
./platform/studio-deploy/verify-approvals.sh <checkout> <slug> /tmp/proposed.txt

# 5. install it (root-owned)
sudo install -m 0644 -o root -g wheel /tmp/proposed.txt \
  /usr/local/libexec/studio-deploy/approved-migrations/<slug>.txt
```

## Trusted-code promotion is an explicit human step (third round, finding D)

`platform/studio-deploy/` is the canonical **source** for the trusted components,
and it lives in a repository the agent can write — GitHub `main` is not currently
branch-protected. That is acceptable for a source tree, and only because
promotion is not automatic. The rules, stated so they can be enforced by reading
them:

- **The agent may propose and commit changes here.** Proposing trusted-code
  changes in a reviewable branch is normal work, not an escalation.
- **Installed trusted code never changes on its own.** Nothing in this repository,
  no workflow, no script and no agent action installs or upgrades a component.
  Only a human running `sudo install` moves code into
  `/usr/local/libexec/` or `/usr/local/bin/`.
- **Before every `sudo install`, the human reviews the concrete diff** of the
  trusted component being installed — not a summary of it. The files that decide
  what runs with owner rights are small on purpose.
- **Installation happens from an explicitly reviewed commit/SHA**, never from
  "current main". Check out the reviewed commit (or verify
  `git rev-parse HEAD` against the SHA that was reviewed) and install from that
  checkout, so the installed bytes correspond to the reviewed diff.
- **No automatic promotion from the agent repo.** If this repository is ever
  wired to deploy itself, the boundary this directory exists to draw is gone —
  an agent-writable commit would decide what runs next to the owner credential.

A useful check before installing, since the components are plain shell:

```sh
git -C <checkout> rev-parse HEAD          # the SHA you reviewed
bash -n platform/studio-deploy/*.sh       # syntax gate for the whole trusted set
bash platform/studio-deploy/tests/ref-binding.sh   # and the rest of the suites
```

## The SSH forced command (third round, finding B)

The documented `authorized_keys` entry uses **`restrict`**:

```
command="/usr/local/bin/<slug>-deploy-hook",restrict ssh-ed25519 AAAA... ci@github
```

Why `restrict` rather than the previous list (`no-pty,no-port-forwarding,
no-agent-forwarding,no-X11-forwarding`): that list leaves `~/.ssh/rc`
executable. In an interactive session sshd runs `~/.ssh/rc` *before* the forced
command, and on the host the login account is the same uid that owns the runtime
repo, the deploy clone and the state directory — so `~/.ssh/rc` is a path an agent
can create and get executed next to the deploy credential. `restrict` disables
port, agent and X11 forwarding, PTY allocation **and** `~/.ssh/rc`, and — per
`sshd(8)` — any future restriction capability added to `authorized_keys`.
`no-user-rc` is the narrow equivalent if the forwarding options are wanted
individually; either is acceptable, the previous list is not.

The installed hook additionally execs the gate under `env -i` with a fixed PATH,
as defence in depth on top of the gate's own literal trust path.

## Installation (owner step — needs root on the host)

Substitute the placeholders for your slug and host **before** installing; an
unsubstituted file refuses to run.

```sh
# 0. install from a REVIEWED commit (see the promotion policy above)
install -d -m 0755 /tmp/ua-smoke-deploy-build
git -C <checkout> archive <reviewed-sha> platform/studio-deploy | tar -x -C /tmp/ua-smoke-deploy-build

SUBST=/tmp/ua-smoke-deploy-build/platform/studio-deploy
for f in deploy-gate.sh deploy-hook.sh migrate-runner.sh; do
  /usr/bin/sed -i '' \
    -e 's|__APP_SLUG__|ua-smoke|g' \
    -e 's|__TRUST_DIR__|/usr/local/libexec/studio-deploy|g' \
    -e 's|__STATE_DIR__|/Users/<owner>/.local/state/ua-smoke|g' \
    -e 's|__GATE_REPO__|/Users/<owner>/.local/state/ua-smoke/deploy-clone|g' \
    -e 's|__DOCKER_BIN__|/usr/bin/docker|g' \
    -e 's|__PSQL_IMAGE__|postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2|g' \
    -e 's|__COOLIFY_API__|http://localhost:8000/api/v1|g' \
    -e 's|__DEPLOY_TIMEOUT__|900|g' \
    -e 's|__HEALTH_TIMEOUT__|180|g' \
    -e 's|__POLL_INTERVAL__|10|g' \
    "$SUBST/$f"
done

# 1. trusted components
sudo install -d -m 0755 /usr/local/libexec/studio-deploy
sudo install -m 0755 -o root -g wheel "$SUBST/deploy-gate.sh"    /usr/local/libexec/studio-deploy/ua-smoke-deploy-gate
sudo install -m 0755 -o root -g wheel "$SUBST/migrate-runner.sh" /usr/local/libexec/studio-deploy/ua-smoke-migrate-runner
sudo install -m 0755 -o root -g wheel "$SUBST/deploy-hook.sh"    /usr/local/bin/ua-smoke-deploy-hook

# 2. approvals (review the SQL diff first — see the approval chain above)
sudo install -d -m 0755 /usr/local/libexec/studio-deploy/approved-migrations
#   ... verify-approvals.sh ... then:
sudo install -m 0644 -o root -g wheel /tmp/proposed.txt \
  /usr/local/libexec/studio-deploy/approved-migrations/ua-smoke.txt

# 3. forced command (single line) in ~/.ssh/authorized_keys
#   command="/usr/local/bin/ua-smoke-deploy-hook",restrict ssh-ed25519 AAAA... ci@github
```

`__DOCKER_BIN__` is a deliberate choice and should be made explicitly, not left to
`PATH`: on the host `/usr/local/bin/docker` is a root-owned symlink into
`Docker.app`, whose payload is **owned by the login user** — so the Homebrew CLI
at `/usr/bin/docker` is the better default for a trusted component, and
the pin is recorded here so the decision is visible. The gate resolves its own
`PATH` and home directory from the OS, so a caller's `PATH`/`HOME` cannot move
either.

The gate refuses to run if a trusted component is missing or is group/other
writable, and — on the production path (`TRUST_DIR` = the literal default) — if it
is not root-owned.

## The OS user model this relies on (third round, finding A)

Measured on the host (2026-09-21), because the boundary is only as good as the
uids around it:

| Fact | Measured value |
| --- | --- |
| Forced command runs as | the login user (`the login account`), from `~/.ssh/authorized_keys` |
| `migrate.env`, `secrets.env` | `login-user:login-group`, mode `600` |
| `coolify_token` | not present (owner step), expected `600` |
| Docker context | `currentContext: devbox`; socket `the local Docker socket` (owner-only) |
| Deploy clone | `~/.local/state/ua-smoke/deploy-clone`, owned by the login user |
| Trusted components | `/usr/local/libexec/studio-deploy/` (root:wheel) — **not yet installed** |
| Same-uid processes | Node/Python agent processes and the runtime repo run as the same `login-user` |
| Dedicated `ci`/`deploy` user | none exists |

The consequence, stated plainly: **on this host the deploy credential is not
separated from the agent by uid.** The agent's own processes run as the same user
that owns `migrate.env` and can read it. The file-integrity properties above
(root-owned gate, literal trust path, approval hashes) reduce what can be
*redirected*, but not what can be *read* by anything already running as `login-user`.
That is a property of the single-user macOS host, not a defect the gate can fix
from inside — and it is why the trusted components are installed root-owned and
why nothing here claims uid separation.

A dedicated `ci`/`deploy` user would close it: owner-only state files readable
only by that uid, `authorized_keys` in that account, and the gate installed for
it. That is a deliberate architecture change with real cost (a second account,
its own Docker access or a group-scoped socket, its own clone and
credential placement) and it is **not** made here; it is recorded as the honest
next step for the owner. Nothing in the current design depends on it being
deferred — the gate fails closed either way — but no stronger claim than "the
credential is readable by the login user" should be made until it exists.

## Verification (no live mutation)

All of these run locally; none deploys, migrates or contacts the network, and the
suites point `COOLIFY_API` at a dead port so a run can never trigger a real deploy.

```sh
bash tests/ref-binding.sh        # the commit must be the tip of the allowed ref
bash tests/verify-binding.sh     # health check bound to app + full commit + state
bash tests/hook-validation.sh    # only one full sha reaches the gate; env cannot redirect it
bash tests/migration-approval.sh # unapproved/edited SQL never reaches the DB; call shape pinned
bash tests/approval-binding.sh   # approval content verified independently and bound to the commit
```

Each suite runs the component **substituted** (`tests/lib/harness.sh`), and each
FAIL expectation asserts the *reason* for the refusal, so a rejection from the
wrong branch of the logic cannot be mistaken for a pass.

## Open items

- Installation on the host needs root (`sudo`), which the agent does not hold —
  this remains an owner step.
- The CI key pair, the pinned host key/fingerprint secrets and the
  `authorized_keys` entry are owner steps too.
- `--verify-only <gate> <full-sha>` answers "is what is running actually this app
  at this commit?" at any time, reads no credential, and mutates nothing.
- A dedicated `ci`/`deploy` uid (see the OS user model above) is the remaining
  uid-separation step; not implemented here.
