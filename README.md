# Cedric's Studio — public excerpt

Personal project by Cedric Durau. This repository is a cleaned excerpt of a private studio where I design agent workflows, review gates, MCP servers, and application templates for a self-hosted platform.

It is a record of my own technical work. It is not employment history and it does not describe a commercial product. A separate public repository, [llm-bench](https://github.com/cdurau/llm-bench), holds reproducible benchmarks of local language models used as coding agents.

## What this history is

The commits below 2026-11-07 are real commits from the private project. `git-filter-repo` kept an allowlist of paths, preserved the original authors and timestamps, and dropped commits that became empty. Commit hashes differ from the private repository. A few identifiers inside the kept files were replaced with placeholders such as example hosts, example home directories, and example vault names. No historical commit was invented and none was backdated.

Some commit messages describe a larger original change than the diff that remains here. The operational platform, deployment configuration, and private agent setup are not part of this excerpt. This `README.md` was added on 2026-11-07 as a new commit.

No repository-wide license is selected yet. Third-party packages keep the licenses recorded in their own metadata. The TypeScript Obsidian MCP server declares MIT in `package.json`. Until a repository license is added, treat the other files as all rights reserved.

## Architecture

The excerpt has four layers. Each layer is code that can be read and, for the checks listed below, run without the private platform.

```text
app brief
    -> select_profile.py          choose one supported application profile
    -> scaffold_template.py       copy that profile into a bounded app tree
    -> git_gate.py                accept only the bounded diff, fail closed on secret shapes
    -> review_checklist.py        require inspected evidence before acceptance
    -> platform/studio-deploy     owner-installed deploy gate, separate from the app repo
templates/                        runnable application profiles
mcps/obsidian/                    two MCP servers over the same vault model
```

The workflow scripts live at `.hermes/archive/skills/app-builder-workflow/scripts/`. That is the path the filtered history preserves. The private skill definition that once wrapped these scripts is not included.

| Piece | Role |
| --- | --- |
| `select_profile.py` | Deterministic profile choice from nine brief dimensions |
| `scaffold_template.py` | Copies a profile and checks the result stays inside the app tree |
| `git_gate.py` | Validates the real git diff. An executor's own report is not evidence |
| `review_checklist.py` | Fail-closed review record, including senior-review triggers |
| `agy_job_wrapper.py` | Parses a job envelope and rejects malformed or incomplete input |
| `platform/studio-deploy/` | Shell deploy gate. Trust paths are fixed at install time. Tests use fake Docker and fake sudo |
| `templates/` | Application profiles: static Vite, React/Hono with SQLite or Postgres, Next.js, FastAPI, a small Python service, a Better Auth user-app, and a VPS app scaffold |
| `mcps/obsidian/obsidian-py/` | Python MCP server. Reads and writes Markdown directly. stdio or streamable HTTP |
| `mcps/obsidian/obsidian-js/` | TypeScript MCP server for the same vault model, bundled to one file |

`select_profile.py` knows six profiles: `static-vite`, `react-hono-sqlite`, `react-hono-postgres`, `next-postgres`, `python-fastapi`, and `python-service`. The React/Hono Postgres v2 profile, the user-app profile, and the VPS app scaffold are later templates in this repository. They are not members of that six-profile tuple.

## Tech stack

- Python 3.11+ for the workflow scripts, the standard library, and the Obsidian MCP server. The profile scripts use PyYAML and JSON Schema where a brief is validated.
- TypeScript on Node.js 20+ (the VPS app template asks for Node 22+), Vitest, React 19, Hono, Better Auth, Drizzle ORM, and PostgreSQL in the later templates.
- MCP over stdio and streamable HTTP. The Python HTTP path can verify a Cloudflare Access JWT. Tests use fake team and audience values.
- Shell for the deploy gate, with hermetic fixtures rather than a live host.
- Dockerfiles are present on the Python MCP server and the application templates. Image builds and integration databases were not run for this excerpt.

## Responsibility

I wrote the workflow, the review and deploy gates, both Obsidian MCP servers, and the application templates. Libraries such as Hono, React, Better Auth, Drizzle, FastAPI, and the MCP SDK are dependencies. I did not include third-party skill packs or the private platform that runs the studio.

## Agent workflow and MCP

Profile selection is a pure function of the brief. This command was run in the filtered tree:

```bash
cd .hermes/archive/skills/app-builder-workflow/scripts
python3 select_profile.py evaluate --python-needed --backend-needed
```

It prints a `python-service` decision and the nine dimension flags. `from-brief`, `derive-manifest`, and `validate` read an app brief from disk. Validation is fail-closed: a brief that does not match the schema is rejected.

The git gate checks the files git actually changed. The bounded shape is `apps/<slug>/**` plus one deploy workflow for that slug. Path traversal, unexpected workflows, and secret-shaped lines fail the gate. The tests include obvious fake credentials, including the public AWS example key and a `ghp_` string of x characters, so the detector has something to catch. Those fixtures are not credentials.

The two Obsidian servers do not ship a vault path. Configuration comes from the environment, a config file, or `--vault name=/path/to/vault` on the TypeScript server. Do not commit a vault path.

## Security and review model

The workflow treats an agent's self-report as untrusted. `git_gate.py` inspects the diff. `review_checklist.py` records whether scope, diff, and external effects were actually checked, and it forces a senior review for security boundaries, coupled control planes, and contradictory evidence.

`platform/studio-deploy/` is the owner-installed side of a deploy. Placeholders such as `__APP_SLUG__` and `__TRUST_DIR__` have to be substituted once at install time. The scripts refuse to run while a placeholder remains. Trust decisions do not follow environment overrides. Migration SQL runs only when an approval list matches the approved commit. The suites under `platform/studio-deploy/tests/` build a substituted copy and a fake Docker. They do not contact a production host.

The Python MCP server's HTTP mode checks a bearer token and can verify a Cloudflare Access JWT. The tests reject a query-string token and use non-production team and audience values.

## Checks that passed

Run on 2026-11-07 from this tree. Counts are the command output from that run.

App-builder workflow, from `.hermes/archive/skills/app-builder-workflow/scripts`:

```bash
python3 -m unittest discover -p 'test_*.py'
uv run --with pytest --with pyyaml --with jsonschema pytest -q \
  test_agy_job_wrapper.py test_portability_contract.py \
  test_scaffold_template.py test_select_profile.py
```

`unittest` reported 221 tests and 4 import errors, because four modules require `pytest`. It reported no assertion failure. The pytest command then reported `158 passed`.

Deploy gate, from `platform/studio-deploy`:

```bash
for s in approval-binding deploy-user hook-validation migration-approval \
         ref-binding trusted-path verify-binding
do
  bash "tests/${s}.sh"
done
```

All seven suites printed `PASS`. `tests/fake-docker.sh` is a helper. It is not a suite, and running it alone fails until a scenario file is set.

Obsidian MCP:

```bash
cd mcps/obsidian/obsidian-py && uv sync --extra dev && uv run pytest
cd mcps/obsidian/obsidian-js && npm ci && npm test
```

Python: `30 passed`, plus one Starlette deprecation warning about `httpx` in `tests/test_resources.py`. TypeScript: `48 passed` (31 unit, 12 e2e, 5 HTTP).

Application templates:

```bash
cd templates/react-hono-postgres-v2 && npm ci && npm run test:unit
cd templates/vps-app && npm ci && npm run test:unit
cd templates/python-fastapi
PYTHONPATH=. uv run --with-requirements requirements.txt \
  --with-requirements requirements-dev.txt pytest -q
cd templates/python-service
PYTHONPATH=. uv run --with-requirements requirements.txt \
  --with-requirements requirements-dev.txt pytest -q
```

Results: React/Hono Postgres v2 `27 passed`, VPS app unit tests `16 passed`, FastAPI `5 passed`, Python service `4 passed`. The template READMEs tell you to install into a virtualenv and run `pytest`. With `uv run`, the template root has to be on `PYTHONPATH` or `import source` fails. That is the command that passed.

`npm audit` on the three installed Node trees, without changing lockfiles, reported 13 advisories for the TypeScript MCP server (6 moderate, 5 high, 2 critical), 15 for the VPS app (1 low, 8 moderate, 3 high, 3 critical), and 12 for React/Hono Postgres v2 (8 moderate, 2 high, 2 critical).

## Checks not run

Integration scripts, `templates/vps-app` image smoke, and any live deployment were not run. They need Docker or a database. Unit tests for `static-vite`, `react-hono-sqlite`, `react-hono-postgres`, `next-postgres`, and `react-hono-postgres-v2-user-app` were not run for this pass. The user-app's live smoke tests are not in this excerpt.

## Chronology

The private repository has a longer history. This excerpt starts on 2026-08-21, the first commit that touches the kept paths, and its last historical commit is 2026-11-05. Sixty historical commits remain: 20 in August, 34 in September, 4 in October, and 2 in November.

- **August 2026.** Application profiles, the app-brief schema, profile selection, scaffolding, the git gate, the review checklist, and the job wrapper. Several commits only tighten fail-closed behavior after review.
- **September 2026.** The React/Hono Postgres v2 profile, the Better Auth user-app and its contract tests, the trusted deploy gate and its review rounds, both Obsidian MCP servers, and a harder migration runner.
- **October 2026.** Cloudflare Access JWT checks on the Python MCP server, then one commit whose message is about a single app skill. The surviving change is the archived copy of the workflow scripts.
- **November 2026.** The reviewed VPS app scaffold on 2026-11-04. The 2026-11-05 message describes GitHub App provisioning. The surviving diff is one container-registry line in `templates/vps-app/README.md`.

## Where to read next

1. `.hermes/archive/skills/app-builder-workflow/scripts/git_gate.py`
2. `.hermes/archive/skills/app-builder-workflow/scripts/select_profile.py`
3. `platform/studio-deploy/README.md` and `platform/studio-deploy/deploy-gate.sh`
4. `mcps/obsidian/obsidian-py/src/obsidian_mcp/server.py`
5. `mcps/obsidian/obsidian-js/src/`
6. `templates/vps-app/source/` and `templates/react-hono-postgres-v2/source/`
