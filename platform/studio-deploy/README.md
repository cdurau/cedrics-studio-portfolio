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
`__DOCKER_BIN__`, `__DOCKER_HOST__`, `__PSQL_IMAGE__`, `__COOLIFY_API__`, the
three timeouts, and —
since S0-13 — `__DEPLOY_USER__` and `__SUDO_BIN__`) that the installing owner
substitutes **exactly once**. The installed file reads no override;
`deploy-gate.sh`, `deploy-hook.sh`, `migrate-runner.sh` and
`verify-container.sh` each refuse to run while a placeholder is still present, so
an unsubstituted copy fails loudly instead of behaving like a production
component.

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
- **The helpers take no arguments at all (S0-13).** The runner and the
  container-facts helper are invoked as root, through `sudo -n`, from a gate that
  runs as the dedicated deploy user. Their installed sudoers entries use the
  empty-argument form (`... <path> ""`), so sudo refuses any argv, and both
  scripts refuse a non-empty `$#` as well. The previous call-shape pinning (its
  installed env-file, approvals path and a tree below the clone) was therefore
  replaced by the stronger property: every input is a literal, and there is no
  argument left to launder.

The test suites no longer set environment variables on these scripts at all. They
build a **substituted copy** (`tests/lib/harness.sh`) and run that, so test
injection is a property of the copy and cannot be activated on the production path
by any caller, variable or flag.

### What is still caller-influenced

Honest inventory for the production path: the commit SHA (the sole argument, and
validated), the invoking user's `SSH_ORIGINAL_COMMAND` (validated), and the
contents of the deploy user's state directory (mode 600, owner-only). Nothing
else — see § "Verification" for the negative tests that assert this. The gate's
uid must be the account named by the installed `__DEPLOY_USER__` (the uid itself
is resolved by the OS, never written down); a copy invoked as any other user
refuses before it reads or acts on anything.

## Layout

| Path | Installed as | Purpose |
| --- | --- | --- |
| `deploy-hook.sh` | `/usr/local/bin/<slug>-deploy-hook` (root:wheel 0755) | SSH forced-command entry point; accepts exactly one 40-char lowercase-hex SHA |
| `deploy-gate.sh` | `/usr/local/libexec/studio-deploy/<slug>-deploy-gate` (root:wheel 0755) | The whole deploy: ref binding → approval binding → approved migration → trigger → poll → verify app+commit+health |
| `migrate-runner.sh` | `/usr/local/libexec/studio-deploy/<slug>-migrate-runner` (root:wheel 0755) | Applies owner migrations, but only content the operator approved. Takes **no arguments**; reached through `sudo -n` from the gate |
| `verify-container.sh` | `/usr/local/libexec/studio-deploy/<slug>-verify-container` (root:wheel 0755) | Read-only container facts (`ps`/`inspect`) for the pinned app. Takes **no arguments** and returns facts, never a verdict; the binding stays in the gate |
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
as defence in depth on top of the gate's own literal trust path. Under the
dedicated-deploy-user model (below) this `authorized_keys` entry lives in the
**deploy user's** home, so `~/.ssh/rc` would be that account's own file rather
than one the coding agent can write — which is the second reason the entry is
there and not in the login account.

## The dedicated deploy user (S0-13)

**Designed and asserted offline; NOT created on any host.** See
`the unpublished deploy-user review` for the full design and
the ordered owner steps.

The forced command runs as whichever uid owns the `authorized_keys` entry. While
that was the login user, mode `600` on `migrate.env` protected nothing: every
agent process ran as the same uid and could read it. The design moves the whole
deploy path onto a separate, hidden service account:

| Item | Value |
| --- | --- |
| Account | `studio-deploy` (hidden service account; identity is the NAME) |
| `UniqueID` / `gid` | **resolved at install time** — pick a free uid/gid immediately before creating the account and record it; do not assume a fixed number (a free uid on one machine, not a constant) |
| Home | `/var/studio-deploy`, mode 700 |
| Interactive rights | none (no admin rights; key-only auth; `restrict`) |

Why the uid is not written down: no account exists yet, and a uid that is free
today can be taken by another tool tomorrow. The components therefore check the
**account name**, and the uid is compared only against the uid the process
already is (owner checks), never against a stored number. Verify the choice
immediately before installing with `dscl . -list /Users UniqueID | sort -k2 -n | tail`
and `dscl . -list /Groups PrimaryGroupID | sort -k2 -n | tail`.

Two consequences are worth stating plainly:

- **The shell must not be `/usr/bin/false`.** sshd runs a forced command as
  `<shell> -c <command>`, so a login shell of `/usr/bin/false` would swallow the
  command and break the deploy. The boundary is the `restrict` option plus the
  forced command plus key-only authentication on a hidden, non-admin account —
  not a disabled shell.
- **The gate refuses any account but `__DEPLOY_USER__`.** Running a copy as the
  login user is a configuration error, and it fails loudly rather than acting with
  the agent's identity. The identity is the account NAME; the uid is whatever the
  OS assigned at install time, so no numeric uid is hard-coded anywhere.

The gate holds no Docker access. On this host the Docker socket is
`login-user:login-group` mode 600, so reaching it from the gate would hand the deploy path
the login user's authority and undo the separation. Instead exactly two
operations are delegated, through `sudo -n`, to root-owned, argument-free
helpers, and the `/etc/sudoers.d/studio-deploy` entries use the empty-argument
form so sudo refuses argv:

```
studio-deploy ALL=(root) NOPASSWD: /usr/local/libexec/studio-deploy/<slug>-migrate-runner ""
studio-deploy ALL=(root) NOPASSWD: /usr/local/libexec/studio-deploy/<slug>-verify-container ""
```
```

## Installation (owner step — needs root on the host)

Substitute the placeholders for your slug and host **before** installing; an
unsubstituted file refuses to run.

```sh
# 0. install from a REVIEWED commit (see the promotion policy above)
install -d -m 0755 /tmp/ua-smoke-deploy-build
git -C <checkout> archive <reviewed-sha> platform/studio-deploy | tar -x -C /tmp/ua-smoke-deploy-build

SUBST=/tmp/ua-smoke-deploy-build/platform/studio-deploy
for f in deploy-gate.sh deploy-hook.sh migrate-runner.sh verify-container.sh; do
  /usr/bin/sed -i '' \
    -e 's|__APP_SLUG__|ua-smoke|g' \
    -e 's|__TRUST_DIR__|/usr/local/libexec/studio-deploy|g' \
    -e 's|__STATE_DIR__|/var/studio-deploy/state/ua-smoke|g' \
    -e 's|__GATE_REPO__|/var/studio-deploy/state/ua-smoke/deploy-clone|g' \
    -e 's|__DEPLOY_USER__|studio-deploy|g' \
    -e 's|__SUDO_BIN__|/usr/bin/sudo|g' \
    -e 's|__DOCKER_BIN__|/usr/local/libexec/studio-deploy/docker|g' \
    -e 's|__DOCKER_HOST__|unix:///var/run/docker.sock|g' \
    -e 's|__PSQL_IMAGE__|postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2|g' \
    -e 's|__COOLIFY_API__|http://localhost:8000/api/v1|g' \
    -e 's|__DEPLOY_TIMEOUT__|900|g' \
    -e 's|__HEALTH_TIMEOUT__|180|g' \
    -e 's|__POLL_INTERVAL__|10|g' \
    "$SUBST/$f"
done

# 1. trusted components
sudo install -d -m 0755 /usr/local/libexec/studio-deploy
sudo install -m 0755 -o root -g wheel "$SUBST/deploy-gate.sh"       /usr/local/libexec/studio-deploy/ua-smoke-deploy-gate
sudo install -m 0755 -o root -g wheel "$SUBST/migrate-runner.sh"    /usr/local/libexec/studio-deploy/ua-smoke-migrate-runner
sudo install -m 0755 -o root -g wheel "$SUBST/verify-container.sh"  /usr/local/libexec/studio-deploy/ua-smoke-verify-container
sudo install -m 0755 -o root -g wheel "$SUBST/deploy-hook.sh"       /usr/local/bin/ua-smoke-deploy-hook

# 1a. the root-owned docker copy the helpers execute (see "Choosing __DOCKER_BIN__")
#     `install` dereferences the symlink and copies the real binary.
sudo install -m 0755 -o root -g wheel "$(readlink -f /usr/bin/docker)" \
  /usr/local/libexec/studio-deploy/docker
#     Confirm the whole chain is root-owned and link-free before relying on it:
ls -l /usr/local/libexec/studio-deploy/docker
stat -f '%Su:%Sg %Lp %N' / /usr /usr/local /usr/local/libexec /usr/local/libexec/studio-deploy

# 1b. the two argument-free sudo capabilities for the deploy user
sudo install -m 0440 -o root -g wheel /tmp/proposed-sudoers /etc/sudoers.d/studio-deploy
sudo /usr/sbin/visudo -cf /etc/sudoers.d/studio-deploy   # MUST validate before relying on it

# 2. approvals (review the SQL diff first — see the approval chain above)
sudo install -d -m 0755 /usr/local/libexec/studio-deploy/approved-migrations
#   ... verify-approvals.sh ... then:
sudo install -m 0644 -o root -g wheel /tmp/proposed.txt \
  /usr/local/libexec/studio-deploy/approved-migrations/ua-smoke.txt

# 3. forced command (single line) in the DEPLOY USER's authorized_keys
#   owner <deploy-user>:<deploy-group>, mode 600
#   command="/usr/local/bin/ua-smoke-deploy-hook",restrict ssh-ed25519 AAAA... ci@github
```

### Choosing `__DOCKER_BIN__` — install a root-owned copy

`__DOCKER_BIN__` is what a **root** helper executes, so it is part of the trusted
computing base and the choice must be explicit, never left to `PATH`. Measured on
the host (2026-09-22), **neither** installed docker CLI qualifies on its own:

| candidate | measured | verdict |
| --- | --- | --- |
| `/usr/bin/docker` | symlink → Cellar, owned by the login user | agent-replaceable |
| `/usr/local/bin/docker` | `root:wheel` **symlink** → `/Applications/Docker.app/…/docker`, payload owned by the login user, writable | agent-replaceable *through the link* |

The second row is the dangerous one, and it is why the earlier guidance here was
not enough: `stat` has lstat semantics, so an ownership check on that path
measures the **link** (uid 0) and passes, while root executes a binary the login
user can overwrite. An earlier revision of this file recommended the Homebrew CLI
as "the better default" — that is also wrong, just more visibly: it is
login-user-owned, so the production-path check refuses it and the helper cannot
run at all.

**So pin a root-owned copy under the trust directory** (installed in step 1
below). It is the only option on this host whose whole path chain is root-owned:

```
__DOCKER_BIN__ = /usr/local/libexec/studio-deploy/docker
```

The cost is explicit and accepted: a copy does not follow `brew upgrade`, so the
docker CLI used by the trusted helpers is pinned until the owner re-installs it.
That is the intended trade — a trusted binary that changes under you when an
unrelated package updates is not a trusted binary.

Defence in depth, independent of that choice (all four components):

- a trusted path that **is** a symlink is refused outright — there is no
  legitimate reason for one on an installed trust path;
- every ownership/mode check uses `stat -L`, so a link that did appear is
  measured by what actually runs;
- on the production path the **whole chain** from `/` down is asserted —
  every component root-owned and never group/other-writable — because a
  root-owned file inside an agent-writable directory can be replaced wholesale.

The migration runner validates its docker binary the same way. Before
2026-09-22 it validated it not at all, which was the wrong way round: it is the
helper that holds the owner DB credential and performs the only Docker WRITE.

The gate resolves its own `PATH` and home directory from the OS, so a caller's
`PATH`/`HOME` cannot move either.

The gate refuses to run if a trusted component is missing, is a symlink or is
group/other writable, and — on the production path (`TRUST_DIR` = the literal
default) — if its path chain is not root-owned.

## The OS user model this relies on

Measured on the host 2026-09-22 (third round measured 2026-09-21), because the
boundary is only as good as the uids around it.

**Before (the state the third round documented, and the reason S0-13 exists):**

| Fact | Measured value |
| --- | --- |
| Forced command runs as | the login user (`the login account`), from `~/.ssh/authorized_keys` |
| `migrate.env`, `secrets.env` | `login-user:login-group`, mode `600` |
| `coolify_token` | not present (owner step), expected `600` |
| Docker context | `currentContext: devbox`; socket `the local Docker socket`, `login-user:login-group` 600 |
| Deploy clone | `~/.local/state/ua-smoke/deploy-clone`, owned by the login user |
| Trusted components | `/usr/local/libexec/studio-deploy/` (root:wheel) — **not yet installed** |
| Same-uid processes | Node/Python agent processes and the runtime repo run as the same `login-user` |
| Dedicated `ci`/`deploy` user | none exists yet |
| Login user's sudo | `(ALL) ALL` with **password required** — but see the blocker below: one NOPASSWD devbox rule targets a **login-user-writable** binary, so the login uid reaches root without a password |

Target (designed here, not yet created): the deploy path runs as
`studio-deploy`. Then `migrate.env` and `coolify_token` — mode 600, owned by
`studio-deploy` — are unreadable to `login-user` (the login uid), because Unix permissions
act against a different uid. That is the whole separation. The uid of
`studio-deploy` is whatever the OS assigns at install time; the separation
property depends on it differing from the login uid, not on any particular value.

What the file-integrity properties above (root-owned gate, literal trust path,
approval hashes) do **not** do is change who can *read* a file owned by the uid
the agent itself runs as. That is exactly the gap the deploy user closes, and why
the account is the architectural change rather than another gate check.

### What the uid separation does NOT do (read this before installing)

The separation is real for **file confidentiality**, and that is all it is. Two
measured facts bound it, and neither is closed by anything in this directory:

1. **The agent's uid controls the Docker daemon that runs the production
   database.** The socket is `login-user:login-group` 600 and every agent process runs as
   `login-user`, so the owner DB credential is not the only way to the data — whoever
   holds the socket reaches the Postgres container directly. Moving `migrate.env`
   to `studio-deploy` therefore raises the cost of reading a *file*; it does not
   make the database unreachable from a compromised agent. By the same argument,
   `<slug>-verify-container` is an honest proof against *Coolify* misreporting a
   deploy, but **not** against an adversary who controls the daemon: container
   labels and health can be forged by anyone who can start containers.
2. ~~**The login uid can become root without a password.**~~ **Remediated
   2026-09-22** (`a login-user-writable helper` was login-user-writable while a
   `%login-group` NOPASSWD rule pointed at it; it is now `root:wheel` and the
   unprivileged re-run of `the host sudo-target check` returns PASS).
   Until that fix, root read `/var/studio-deploy/…` regardless of mode and the
   uid boundary was void. Full before/after evidence:
   `the unpublished host-trust review` §H-1.5.
   Re-run that script after any `brew upgrade` — a package update can restore a
   bad ownership.

Blocker 1 remains, and must be understood and accepted — or removed by moving
the coding agent off the login account
(`the unpublished agent-migration review`) — **before** the
installation steps below are worth running. Until then, installing this raises
the bar against accident and against a weak attacker; it does not establish the
boundary the design language implies.

Smaller residuals: the deploy user can still invoke its two argument-free helpers
— that is what they are for — and the sudoers empty-argument form is a sudo(8)
behaviour that the owner must verify with `visudo -cf` at install time. Nothing
here claims the uid separation is installed until the owner steps have run.

## Verification (no live mutation)

All of these run locally; none deploys, migrates or contacts the network, and the
suites point `COOLIFY_API` at a dead port so a run can never trigger a real deploy.

```sh
bash tests/ref-binding.sh        # the commit must be the tip of the allowed ref
bash tests/verify-binding.sh     # health check bound to app + full commit + state
bash tests/hook-validation.sh    # only one full sha reaches the gate; env cannot redirect it
bash tests/migration-approval.sh # unapproved/edited SQL never reaches the DB; no argument is accepted
bash tests/approval-binding.sh   # approval content verified independently and bound to the commit
bash tests/deploy-user.sh        # the deploy credential is bound to the deploy uid; no argument injection
bash tests/trusted-path.sh       # symlinked/replaceable trusted paths are refused; the chain is checked
```

Each suite runs the component **substituted** (`tests/lib/harness.sh`), and each
FAIL expectation asserts the *reason* for the refusal, so a rejection from the
wrong branch of the logic cannot be mistaken for a pass. The gate's two sudo
helper calls are exercised through a sudo stand-in (`tests/lib/fake-sudo.sh`), so
the call shape is tested without root — and case I asserts a refused call fails
closed.

Measured 2026-09-22 after the trusted-path fix (counting `^PASS`): `ref-binding`
**11/11**, `verify-binding` **10/10**, `hook-validation` **24/24**,
`migration-approval` **16/16**, `approval-binding` **12/12**, `deploy-user`
**13/13**, `trusted-path` **15/15** — **101/101, 0 FAIL**.

`trusted-path` case A asserts that the shared trusted-path block is
byte-identical in all four components (it is inlined on purpose — each component
is installed standalone, with no shared library on the trust path — so drift is
the risk that has to be tested rather than assumed), and case B pins the platform
semantics the fix rests on: bare `stat` measures a symlink, `stat -L` measures
its target.

## Open items

- Installation on the host needs root (`sudo`), which the agent does not hold —
  this remains an owner step.
- Creating the dedicated `studio-deploy` account, moving the state files under
  `/var/studio-deploy/state/ua-smoke/`, writing its `authorized_keys` and
  installing `/etc/sudoers.d/studio-deploy` are owner steps too, and the design
  is asserted offline (`tests/deploy-user.sh`) but not installed. The ordered
  list is in `the unpublished deploy-user review` § 6.
- The CI key pair, the pinned host key/fingerprint secrets and the
  `authorized_keys` entry are owner steps too.
- `--verify-only <gate> <full-sha>` answers "is what is running actually this app
  at this commit?" at any time, reads no credential, and mutates nothing.
- The sudoers empty-argument form (`""`) is a sudo(8) behaviour: validate the
  installed file with `visudo -cf` before relying on it.
