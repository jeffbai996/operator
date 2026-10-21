# Personal-app workspace candidate

Companion changes for the agent application's `feature/agent-capabilities` branch.
No backend release version or deployed service was changed.

The signed internal `/operator/app-workspace` GET/POST route extends the existing
workspace owner. GET lists latest artifact/recipe revisions and public configured
resource metadata, or reads an exact object revision. POST edits a goal or saves a
versioned object. The app gateway enforces Tailscale-login ownership before calling
this route; the signed internal service retains its existing trust boundary.

Goals use the existing job store, optimistic revisions, and active-run checks.
An active/paused/closed disposition describes the goal and is independent of run
state; editing it does not start or stop execution. Artifact/recipe bodies are
validated and stored as immutable revisions in the additive `app_objects` table
inside the existing workspace database. No schedule is created by saving a recipe.

The personal-app prompt specifies a `.agent.json` table/checklist output format.
The cockpit accepts an explicit `conversation_id` query parameter using its existing
selection validation and lease controls. Browser feed mechanics are unchanged.

Validation on 2026-09-28: 99 focused tests passed across workspace, personal-app
tasks, prompts, resources, internal Operator API, and the paired Node contract.
The paired contract uses disposable storage, fake runtime metadata, and a loopback
test server; it never starts a model. Configure `AGENT_APP_CHECKOUT` and
`AGENT_TEST_NODE` to include it.

```sh
AGENT_APP_CHECKOUT=/absolute/agent-checkout \
AGENT_TEST_NODE=/absolute/node22/bin/node \
PYTHONPATH=modules/operator:modules/host-app python -m pytest \
  modules/operator/tests/test_app_workspace.py \
  modules/operator/tests/test_workspace.py \
  modules/operator/tests/test_personal_app_tasks.py \
  modules/operator/tests/test_operator_prompts.py \
  modules/operator/tests/test_operator_resources.py \
  modules/host-app/tests/test_mcp_internal_operator.py \
  modules/host-app/tests/test_agent_app_contract.py -q
```

A separate broad Operator run produced 1,039 passes, 113 skips, 3 failures, and
42 setup errors. Browser cases lacked the managed Chromium executable. The broader
release gate and live browser handoff remain unverified.

Deployment requires normal shared-service approval and release procedures. Activate
the backend before the companion app. Back up workspace SQLite using its backup
API, preserving WAL consistency. Rollback to the prior release can leave the
additive table and job field in place; older clients will not display them. Do not
delete user artifacts as a rollback mechanism.
