# Research Agent, Phase 3: infrastructure

Status: PREPARED, NOT APPLIED. Every command below is reviewed and ready; none has been run.

Phase 3 exists so that infrastructure is ready **before** any backend revision can create research
work. Phase 2 built the durable engine but registered nothing: no route, no scheduler hook, no
dispatcher. Phase 4 is the first phase that can enqueue a real task, and it must not be the phase that
discovers the queue does not exist.

Scope is deliberately narrow. This phase creates no research behaviour, exposes no endpoint, and ships
no user-visible change.

---

## 1. Verified current state

Read live from `juno-2ea45` on 2026-08-11 with read-only `gcloud` calls. This is what is actually
deployed, not what the config files claim.

| Thing | State | Evidence |
|---|---|---|
| `juno-research` queue | **does not exist** | `gcloud tasks queues describe` returns NOT_FOUND |
| `juno-engagement` queue | RUNNING, 500/s, 1000 concurrent, **maxAttempts 100** | `queues describe` |
| Composite indexes deployed | 22, **none research** | `firestore indexes composite list` |
| TTL policies active | 9 | `firestore fields ttls list` |
| Research TTL policies | **0 of 12 deployed** | same |
| `juno-firecrawl-api-key` secret | **does not exist** | `gcloud secrets describe` NOT_FOUND |
| `juno-scheduler` service account | exists, enabled | `iam service-accounts describe` |
| Billing | enabled, `billingAccounts/01ACDF-D3FE2D-344D5D` | `gcloud billing projects describe` |
| `CLOUD_TASKS_RESEARCH_QUEUE` setting | **not present** in `settings.py` | grep |

### Two blockers found during reconnaissance

**A. `deploy.sh` is already broken and will fail on the next deploy.** Line 228 carries
`--set-secrets="FIRECRAWL_API_KEY=juno-firecrawl-api-key:latest"`, and that secret does not exist in
Secret Manager. `gcloud run deploy` fails when `--set-secrets` names a missing secret, so the next
`./deploy.sh` dies at the Cloud Run step regardless of anything research does. This landed with the
Phase 1 secret wiring and is unrelated to Phase 2. **Step 2 below fixes it and should be run first.**

**B. A blanket index deploy could have removed the `drafts` TTL policy.** `drafts` had an ACTIVE TTL
policy in production but no entry in `firestore.indexes.json`, because it was applied by hand via
`gcloud firestore fields ttls update` (see the one-time infra note in `services/drafts/fields.py:33`).
`firebase deploy --only firestore:indexes` reconciles the whole file, so deploying would have proposed
removing a retention policy that real user data depends on. The override has been added to
`firestore.indexes.json` so the file now mirrors production. **Re-verify the deploy plan anyway at
step 4; do not accept a plan that deletes anything.**

---

## 2. Create the Firecrawl secret (do this first, it unblocks all deploys)

```bash
# Create the secret, then add the real key as a version. Never paste the key into a shell that
# records history; read it from a file and shred the file.
gcloud secrets create juno-firecrawl-api-key \
  --project=juno-2ea45 \
  --replication-policy=automatic

printf '%s' "$FIRECRAWL_KEY" | gcloud secrets versions add juno-firecrawl-api-key \
  --project=juno-2ea45 --data-file=-

# The Cloud Run runtime SA must be able to read it.
gcloud secrets add-iam-policy-binding juno-firecrawl-api-key \
  --project=juno-2ea45 \
  --member="serviceAccount:620715294422-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

Verify: `gcloud secrets versions list juno-firecrawl-api-key` shows one ENABLED version.

---

## 3. Create the `juno-research` queue

A separate queue is not tidiness. Everything today shares `juno-engagement`, which runs at 500
dispatches/second with **maxAttempts 100**. A research fan-out on that queue would contend with
reminders, notifications, chat completion and meeting synthesis for dispatch rate, and, far worse,
would inherit 100 retries on stages that each spend Brave queries, Firecrawl credits and model tokens.

```bash
gcloud tasks queues create juno-research \
  --project=juno-2ea45 \
  --location=us-central1 \
  --max-dispatches-per-second=10 \
  --max-concurrent-dispatches=20 \
  --max-attempts=3 \
  --min-backoff=10s \
  --max-backoff=300s \
  --max-doublings=4
```

Every number, and why it is not the `juno-engagement` value:

| setting | value | reasoning |
|---|---|---|
| `max-dispatches-per-second` | 10 | A quick run's widest wave is `fanout_max=8`. 10/s clears a full wave in under a second while capping a runaway loop at a rate a human notices. |
| `max-concurrent-dispatches` | 20 | Above one full wave so a second user is never queued behind the first, but far under Firecrawl's plan concurrency (`FIRECRAWL_CONCURRENCY=3` per stage). |
| `max-attempts` | **3** | Matches `store.STAGE_ATTEMPT_CAP`. The queue must never retry more times than the engine's own attempt cap, or Cloud Tasks keeps redelivering stages the engine has already declared terminal. This is the single most important divergence from `juno-engagement`'s 100. |
| `min-backoff` | 10s | `juno-engagement` uses 0.1s. A research stage that failed on a provider timeout should not be retried in 100ms; the failure is usually still true. |
| `max-backoff` | 300s | The quick preset's whole `wall_clock_s` is 240s. Backing off longer than the run can live is pointless. |
| `max-doublings` | 4 | 10s, 20s, 40s, 80s, 160s, then flat. Reaches the ceiling inside one run's lifetime. |

`dispatch_deadline` is **per task, not per queue**, and no caller in this repo sets it today, so every
task rides the 10 minute HTTP default. Phase 4's dispatcher must set it explicitly to
`RESEARCH_TASK_DISPATCH_DEADLINE_S = 600`, comfortably above the 150s quick stage bound.

Verify:
```bash
gcloud tasks queues describe juno-research --location=us-central1 \
  --format="yaml(state,rateLimits,retryConfig)"
```

---

## 4. Deploy indexes and TTL policies

`firestore.indexes.json` already carries everything, written during Phase 2 plus the `drafts` repair:
**24 composite indexes** (5 research) and **38 field overrides** (12 research TTL + the restored
`drafts` entry).

The five research indexes exist to serve the five sweeper queries; without them the sweeper throws
`FAILED_PRECONDITION` and crash recovery silently stops working:

| index | serves |
|---|---|
| `research_job_outbox (dispatch_due_at)` | pass A, outbox redelivery |
| `stages (state, stage_deadline_at)` | pass B, stale lease recovery |
| `research_runs (state, pending_question_expires_at)` | pass C, clarification expiry |
| `coord (join_claimed, deadline_at)` | pass D, stuck fan-out collapse |
| `research_deletions (state)` | pass E, deletion drain |

```bash
cd /c/Users/varun/MobileApps/Aura
firebase deploy --only firestore:indexes --project juno-2ea45
```

**Read the plan before confirming.** It must show only additions. If it proposes deleting any index or
field override, stop: that means production still holds something the file does not describe, exactly
like the `drafts` case above.

Verify, and wait for READY rather than assuming (index builds are asynchronous and a CREATING index
serves nothing):

```bash
gcloud firestore indexes composite list \
  --format="table(collectionGroup,state)" | grep -E "research|stages|coord"

gcloud firestore fields ttls list \
  --format="table(name.scope(collectionGroups),ttlConfig.state)" | grep research
```

Expected: 5 indexes `READY`, 12 TTL policies `ACTIVE`. A TTL policy backfills existing documents, so
allow time on collections that already hold data. All research collections are empty today, so this
should settle quickly.

---

## 5. Settings that Phase 4 needs and Phase 2 did not add

Phase 2 added only `PROJECT_RESEARCH_DAILY_COST_CAP_MICROUSD` (`settings.py:180`), because the ledger
reads it. Two more are required before a dispatcher can exist, and both belong in the same release as
the dispatcher, not here:

```python
CLOUD_TASKS_RESEARCH_QUEUE: str = "juno-research"
RESEARCH_TASK_DISPATCH_DEADLINE_S: int = 600
```

They are listed here so step 3's queue name and step 6's deadline are not invented twice.

---

## 6. Budget alerts and the kill switch

`PROJECT_RESEARCH_DAILY_COST_CAP_MICROUSD` defaults to 25_000_000 (25 USD/day) and is enforced
in-process by `ledger.reserve_project_spend`, which fails closed. Setting it to `0` stops admission
entirely. It is a budget value, deliberately not spelled `RESEARCH_ENABLED`, because the backend
forbids feature flags.

Cloud billing budgets are the independent outer boundary, because the in-process cap only knows about
spend the engine itself reserved. It cannot see Firecrawl's own invoice or a bug that bypasses the
ledger.

```bash
gcloud billing budgets create \
  --billing-account=01ACDF-D3FE2D-344D5D \
  --display-name="juno research providers" \
  --budget-amount=200USD \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.75 \
  --threshold-rule=percent=0.9
```

Firecrawl and Brave bill outside GCP, so their credit alerts must be set in those provider dashboards
separately. The architecture's 50/75/90 alerting requirement is only half satisfied by the command
above.

---

## 7. The three proof probes Phase 3 owes

Infrastructure is not "ready" because a command returned zero. Each of these produces evidence.

**Probe 1, OIDC reaches a non-public target.** Confirms the queue's service account can actually
invoke the internal endpoint before Phase 4 depends on it.

```bash
gcloud tasks create-http-task probe-oidc-1 \
  --queue=juno-research --location=us-central1 \
  --url="https://juno-backend-620715294422.us-central1.run.app/internal/research/step" \
  --oidc-service-account-email=juno-scheduler@juno-2ea45.iam.gserviceaccount.com \
  --oidc-token-audience="https://juno-backend-620715294422.us-central1.run.app" \
  --body-content='{"probe":true}'
```

Expected today: the endpoint does not exist yet, so a 404 from the Cloud Run service is the SUCCESS
signal. It proves the task was dispatched and authenticated. A 401 or 403 means the OIDC identity is
wrong and must be fixed before Phase 4.

**Probe 2, duplicate delivery is harmless.** Create the same task name twice and confirm the second
returns `ALREADY_EXISTS` rather than a second task. This is the property `tasks.task_name_for`'s
determinism relies on, and the reason a crash between enqueue and commit is safe.

**Probe 3, the kill switch blocks admission.** With `PROJECT_RESEARCH_DAILY_COST_CAP_MICROUSD=0`,
`ledger.reserve_project_spend` must return False and admission must refuse. This is provable in
process without touching cloud infrastructure and should be run as a throwaway inspection, not a test.

---

## 8. Rollback

| step | undo |
|---|---|
| secret | `gcloud secrets delete juno-firecrawl-api-key` |
| queue | `gcloud tasks queues delete juno-research --location=us-central1` |
| indexes | `gcloud firestore indexes composite delete <index-id>` per index |
| TTL | `gcloud firestore fields ttls update expires_at --collection-group=<cg> --disable-ttl` |
| budget | `gcloud billing budgets delete <budget-id>` |

Deleting a TTL policy does **not** restore documents it already expired. Research collections are
empty today, so applying now is the cheapest moment; that stops being true the instant Phase 4 runs.

## 9. Exit criteria

Phase 3 is done when: the queue reports RUNNING with the config in step 3, all 5 research indexes
report READY, all 12 research TTL policies report ACTIVE, the Firecrawl secret has an ENABLED version
and `deploy.sh` no longer fails on it, budget alerts exist, and all three probes have recorded
evidence. Until then Phase 4 must not be started, because it is the first phase that can create a task.
