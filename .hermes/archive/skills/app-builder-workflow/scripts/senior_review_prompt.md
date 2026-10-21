# Senior Review Request

## Context
You are Qwen3.8 Max, acting as Senior Reviewer / Deep Architect.
A Qwen3.7 Plus orchestrator has delegated this review to you via Hermes Kanban.

## Task
Review the implementation against the app-brief.json, app.yaml, and executor-brief.md.
Focus on:
1. Security (auth, secrets, identity headers)
2. Scope adherence (only apps/<slug>/** changed?)
3. Test coverage (all required tests present and passing?)
4. Architecture compliance (templates/CONTRACTS.md followed?)
5. Blast radius (any unexpected side effects?)

## Mandatory Architecture Review Items (10 Items A–J)

You MUST explicitly review and provide findings on each of the following:

### A. Profile Selection Dimensions
- Do all 9 profile-selection dimensions actually have decision effect?
- Are there dimensions that never influence the outcome?
- Is the decision tree correct and complete?

### B. App-Brief vs Bounded app.yaml Separation
- Is the separation between app-brief.json (internal workflow) and bounded app.yaml (external manifest) clear?
- Should app-brief.json contain fields that belong in app.yaml?
- Is the schema boundary well-defined?

### C. Kanban / agy-job Responsibility Boundary
- Is the boundary between Kanban (orchestration) and agy-job (execution) clearly defined?
- Are there any responsibilities that are ambiguous or overlapping?
- Should Kanban own any execution-related state (e.g., agy-job status)?

### D. Kanban Lifecycle / Acceptance
- Is the lifecycle consistent (complete only after acceptance)?
- Are implementation and deployment tasks logically separated?

### E. New-App Git Change Set
- What is the exact expected change set for apps/<slug>/**?
- What per-app workflow files are expected (.github/workflows/deploy-<slug>.yml)?
- Are there any files outside apps/<slug>/ that should be modified?

### F. Code Acceptance vs Bounded Per-App Provisioning
- What is the boundary between code acceptance (tests pass, diff clean) and provisioning (Coolify app exists, GitHub secret set)?
- Should provisioning be part of code acceptance or a separate gate?
- Who owns provisioning: Orchestrator or Cedric?

### G. Control-Plane Remediation
- Confirm: Control-plane remediation (Coolify, Tailscale, Authentik, DNS) is exclusively Orchestrator responsibility?
- Are there any scenarios where Executor should have control-plane access?
- Is the boundary clearly documented?

### H. Real Coolify Resource Discovery
- Are container names assumed or discovered via Coolify API?
- Should the workflow use real Coolify resource discovery instead of hardcoded container names?
- What is the canonical way to get container name, deployment UUID, etc.?

### I. Secret Incident Handling
- Distinguish: (1) secret only local/uncommitted, (2) secret already exposed in logs/git/remote.
- For case 2: is rotation/revocation part of the incident contract?

### J. Mandatory Senior Review Gate
- Are the trigger conditions for mandatory senior review clear and complete?
- Do they cover: complex architecture, multi-system coupling, security boundaries, authority changes, complex acceptance, orchestrator uncertainty?

## Inputs
- app-brief.json (attached)
- app.yaml (attached)
- executor-brief.md (attached)
- review-findings.md from Qwen3.7 Plus (attached)
- git diff (read from workspace)
- test results (read from workspace)

## Output
Provide a structured review:
```json
{
  "verdict": "APPROVED | CHANGES_REQUIRED | ESCALATE_TO_CEDRIC",
  "summary": "<1-2 sentences>",
  "findings": [
    {"severity": "HIGH|MEDIUM|LOW", "description": "...", "file": "...", "line": "..."}
  ],
  "security_assessment": "...",
  "architecture_assessment": "...",
  "mandatory_review_items": {
    "profile_selection_dimensions": {"assessment": "...", "findings": ["..."]},
    "app_brief_vs_app_yaml": {"assessment": "...", "findings": ["..."]},
    "kanban_agy_job_boundary": {"assessment": "...", "findings": ["..."]},
    "kanban_lifecycle": {"assessment": "...", "findings": ["..."]},
    "new_app_change_set": {"assessment": "...", "findings": ["..."]},
    "code_acceptance_vs_provisioning": {"assessment": "...", "findings": ["..."]},
    "control_plane_remediation": {"assessment": "...", "findings": ["..."]},
    "coolify_resource_discovery": {"assessment": "...", "findings": ["..."]},
    "secret_incident_handling": {"assessment": "...", "findings": ["..."]},
    "mandatory_senior_review_gate": {"assessment": "...", "findings": ["..."]}
  },
  "recommendation": "..."
}
```

## Constraints
- Do NOT modify code. Review only.
- Do NOT make deployment decisions.
- Do NOT access secrets or credentials.
- Phase 0-10 are COMPLETE and frozen.
