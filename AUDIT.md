# Nexus — Full-Repo Engineering Audit

**Scope:** 11 subsystems, 120 findings that survived an adversarial refutation pass (18 candidates were rejected and are excluded). Deduplicated below into 10 high, 22 medium, and 8 grouped low entries. Every line reference in this report was re-read and verified.

---

## Overall assessment

This is a genuinely well-built codebase, and the documentation is better than any comparable repo I have reviewed: the "AI Note" convention repeatedly and accurately calls out real sharp edges before a reader trips over them, and ~20 sites carry explicit `POSSIBLE BUG` markers that the audit confirmed rather than contradicted. The test suite is substantial and, in several places, genuinely load-bearing — the runner's Event-before-send and store-result-before-set-Event orderings, the parser's 119 adversarial cases, the JWT `type`-claim enforcement in both directions, the NAS path-containment guard, and the completed UUID/`_sid()` coercion layer are all correct and all pinned. The auth/JWT service and `parser.py` are effectively clean.

The weaknesses are not in the algorithms; they are all at the seams. Three patterns account for most of the 120 findings: (1) the hand-mirrored TypeScript client has drifted from FastAPI's actual wire shapes in five places, and because the frontend tests mock at the `api`/store boundary while the backend tests use the correct parameter names, **five user-facing features have never worked for anyone** — maintenance/drain, adding a node to a pool, creating a credential, the Jobs status filter, and register-only node onboarding; (2) failures are consistently detected and then dropped — `console.error`-only handlers, `isLoading` latching true forever, unmapped `IntegrityError`/`EOFError`/`AttributeError` becoming bare 500s; and (3) a surprising amount of documented, typed, and tested surface is not wired to anything — `${var}` interpolation, `credential_config` delivery, the group/pool ACL, the artifact storage pipeline, `cancel_step`, the step `timeout` field, and the entire `/api/admin/*` router.

Two findings deserve to be called out as worse than "bugs." `cancel_job` never cancels anything — the `_active_jobs` dict is keyed by `str` and the route passes a `uuid.UUID`, so the runner keeps executing every remaining step and then overwrites `cancelled` with `completed`. And any authenticated non-admin can read, re-key, or irreversibly delete any other user's credential. Neither is a trust-model trade-off; both are unintended and both are reachable today.

The most expensive systemic risk is that this repo's docstrings are treated as the contract by both humans and agents, and roughly a dozen of them now assert behavior the code does not have (`${...}` resolution, "via cascade", "succeeds silently on SQLite", "the runner emits the job's terminal status", "credential management is admin-gated", "retrying the same request is safe"). Those are worse than an undocumented gap, because the next reader reasons from them.

---

## THEMES

### T1. The client/server contract is hand-mirrored, and the test strategy structurally cannot see drift

Five independent breaks, same root cause and same invisibility mechanism:

| Site | Sends | Server wants | Result |
|---|---|---|---|
| `frontend/src/api/client.ts:273` | `{maintenance: bool}` body | `?enable=` query (`nodes.py:552`) | 422 — drain never worked |
| `frontend/src/api/client.ts:294` | `{node_id: "..."}` | bare JSON array (`pools.py:232`) | 422 — pools can never gain members |
| `frontend/src/pages/Admin.tsx:1029` | `data: {...}` | `fields` (`schemas.py:452-476`) | 422 — no credential can be created from the UI |
| `frontend/src/pages/Jobs.tsx:165` | `?status=` | `job_status` (`jobs.py:139`) | filter silently returns everything |
| `frontend/src/pages/Nodes.tsx:954` | reads `result.id` | `{node, api_key}` (`nodes.py:260`) | emits `--node-id undefined` |

Why nothing caught it: the vitest suites mock `api.*` wholesale and *assert the broken shape* (`client.test.ts:224` pins the JSON body; `Admin.test.tsx:1434` pins `data:`; `Pools.test.tsx:81` and `Nodes.test.tsx:76/79` mock the calls entirely), while the pytest integration tests use the correct wire shape (`params={"enable": ...}`, `json=[str(node.id)]`, `job_status=`). Both halves are green; nothing compares them. Add drift-proofing at the pattern level: generate the client from `/openapi.json`, or add one contract test per mutating endpoint that asserts the URL+body `client.ts` actually builds. The same drift exists in the type layer (`types/index.ts:124` omits `target_node_id`/`target_pool_id`/`target_os`; `submitJob` omits `storage_target`), and because `request<T>` casts without validating, drift is a silent `undefined` rather than a compile error.

### T2. Errors are detected and then discarded — on both sides of the wire

The frontend has no error surface for mutations: roughly a dozen handlers catch into `console.error` or an empty block (one annotated `// handled`, `Admin.tsx:997`, which handles nothing), and every store `fetch()` sets `isLoading: true` and clears it only on success (`stores/index.ts:140, 173, 210, 246, 273, 301`), so a single failed GET produces a permanent spinner plus an unhandled rejection. `client.ts:115` throws `new Error(body.detail)` where FastAPI's 422 `detail` is an array, so validation failures render literally as `[object Object]`. The server mirrors it: unique-name and composite-PK violations, `EOFError` on a truncated tarball, and `AttributeError` from a non-string credential field all escape as bare 500s because `create_app()` registers no exception handler. Four fixes close ~15 findings: `try/catch/finally` + an `error` slice in the six stores; a `detail`-array normalizer in `request()`; one app-level `IntegrityError` → 409 handler; and surfacing caught errors in the mutation handlers instead of `console.error`.

### T3. "Async" is declared but not honored, and sibling implementations disagree

`StepExecutor` invokes `step.startup()` and `step.check()` synchronously on the agent's only event loop (`executor.py:174, 453, 566`), and **six shipped steps** run blocking `subprocess.run` with 120–600 s timeouts inside `startup()`. `detect_capabilities()` adds ~25 s inline on every reconnect (`connection.py:317`). On the server, `S3Strategy.test_connection` is an `async def` doing synchronous boto3 I/O with botocore's 60 s × 5 defaults — while its sibling `GitPATStrategy.test_connection` does the same job correctly with `httpx.AsyncClient`, which is the tell that this is drift, not policy. `broadcast_to_dashboards` awaits each `send_json` inline on the agent receive loop with no timeout. The missing rule: anything that crosses the process boundary goes through `asyncio.to_thread` or a bounded `asyncio.wait_for`. Two `to_thread` calls in `executor.py` fix six step findings at once.

### T4. Identity representation is normalized at the ORM layer only — and execution has no identity at all

The already-completed `_sid()` work fixed raw UUIDs bound to `String(36)` columns, but the same class recurs everywhere an id crosses into a non-ORM structure: `runner._active_jobs` is `str`-keyed and looked up with a `UUID` (`runner.py:228` — the cancel bug), `StorageManager._backends` is `str`-keyed and looked up with a `UUID` (`manager.py:113`), and `model_dump()` leaves `uuid.UUID` objects inside a JSON column (`jobs.py:263`). Separately, a step *attempt* has no identity: the completion key is `"{job_id}:{step_index}"` (position, not execution, `runner.py:727`), `get_latest_step_run` breaks ties by `StepRun.id.desc()` over random uuid4 strings (`ops.py:1124`), and the UI keys live logs on the array index instead of `step_index` (`JobDetail.tsx:566`). Any `jump` loop therefore mis-attributes state, status, and logs across iterations. Fix: extend the `_sid()` convention to every keyed structure, and put the `StepRun` id (or a per-dispatch nonce) into `ExecuteStepCommand` and echo it in `StepStarted/Completed/Failed`.

### T5. Documented, typed, tested — and not wired to anything

`${var}` interpolation is promised by six docstrings, reaches the published OpenAPI schema, and is deliberately deferred to the runner by the parser — and does not exist anywhere in `packages/`. `credential_config` is resolved, decrypted, and transmitted, then discarded on arrival (`executor.py:159`). The Group→GroupPoolAccess ACL is documented as "the authorization gate for job submission" (`ops.py:451`) and has zero call sites — and no route exists to create a grant. The artifact pipeline has no writer, no download route, and a UI link to a 404. `DashboardJobCompleted` has zero producers. `cancel_step` has zero senders. `/api/admin/*` has zero routes behind eight frontend fetches. The `timeout` param on four steps is stored and never compared to anything. Each of these needs a decision — implement or delete — and in the meantime the asserting docstring must be corrected, because agents and maintainers alike are reasoning from it.

### T6. Creation is centralized; teardown is ad hoc

Not one ORM relationship declares a cascade, so four DELETE endpoints 500. Four steps create two temp log files each and nothing ever unlinks them. The WS `finally` unconditionally evicts whatever socket is registered, including a newer one. `useWebSocket`'s cleanup closes the socket but doesn't null `onclose`, so the socket resurrects itself as an unowned orphan. Nothing reaps stale `node.status`, nothing wakes the runner when a node dies, and nothing sends `cancel_step`. The consistent shape: the code that acquires a resource is careful; the code that releases it was written once per site and never reviewed as a family.

---

## HIGH

### H1. No relationship cascades — four DELETE endpoints return 500 and delete nothing
`packages/server/src/nexus_server/db/models.py:253, 282, 283, 497, 498`
Every child FK is either half of a composite PK or `NOT NULL`, so SQLAlchemy's default de-association aborts the flush.
- `DELETE /api/pools/{id}` (`pools.py:227`) → `AssertionError: Dependency rule on column 'pools.id' tried to blank-out primary key column 'pool_node_memberships.pool_id'` for any pool with a member or a group grant.
- `DELETE /api/nodes/{id}` (`ops.py:657` via `nodes.py:546`) → same `AssertionError` on `node_id`. **Deregistering any node that is actually in a pool is impossible**, and since `pools.py` lets any authenticated user add a node to a pool, a non-admin can make a node permanently undeletable by admins.
- `DELETE /api/jobs/{id}` (`jobs.py:596`) → `IntegrityError: NOT NULL constraint failed: step_runs.job_id`. **Job history can never be pruned** — only jobs that dispatched zero steps are deletable, which is exactly what the fixtures build.

**Fix:** `cascade="all, delete-orphan"` on the five relationships (~5 lines, one file). Then correct three docstrings that assert the opposite: `models.py:293-296` ("leaves orphaned membership rows"), `ops.py:648-652` ("become genuine orphans"), `nodes.py:529` ("via cascade"), and `jobs.py:592-595` ("on SQLite this succeeds silently" — wrong; `NOT NULL` is enforced regardless of `PRAGMA foreign_keys`). *Partly author-flagged.*

### H2. Five frontend→server calls use the wrong wire shape; all five features have never worked
`frontend/src/api/client.ts:273`, `:294`; `frontend/src/pages/Admin.tsx:1029`; `frontend/src/pages/Jobs.tsx:165`; `frontend/src/pages/Nodes.tsx:954`
See the table in **T1** for exact shapes. Concretely: an admin clicks "Enable Maintenance" to drain a box → 422, spinner clears, label unchanged, scheduler keeps placing steps on it. An operator adds a node to a pool → 422, so pools created in the dashboard can never acquire members, and `target_pool_id` jobs have nothing to schedule onto. Credential creation → 422 rendered as `[object Object]`, which also blocks any storage backend needing a `credential_id`. Register-only onboarding emits `--node-id undefined` with a valid one-time key the operator can never re-derive (no id column in the node table), so the recovery is delete-and-re-register.

**Fix:** five one-liners (`?enable=`, `JSON.stringify([nodeId])`, `fields:`, `params.job_status`, `result.node.id`), then flip `client.test.ts:224` and `Admin.test.tsx:1434` which currently pin the broken shapes, and add the contract test from T1.

### H3. Cancel is a no-op twice over — and silently upgrades `cancelled` to `completed`
`packages/server/src/nexus_server/runner/runner.py:228`
`Job.id` is `String(36)`, so `submit_job` registers `_active_jobs[job.id]` under a `str`; `POST /api/jobs/{job_id}/cancel` declares `job_id: UUID` and passes a `uuid.UUID`. The lookup always misses in production: `task.cancel()` is never called, the runner executes every remaining step, and on completion writes `status="completed"` over the `cancelled` row. Reproduced end to end: immediately after cancel the row reads `cancelled` (so the UI and the 200 agree), then both steps execute and the final status is `completed`. This also falsifies the AI Note at `runner.py:221-226`. Worse, during the interim the row is in a terminal state, so `DELETE /api/jobs/{id}` is permitted while the runner is still writing `step_runs` against that id. Compounding it, **no server code ever sends `CancelStepCommand`** — the agent's entire cancel ladder (`executor.cancel`, SIGTERM, SIGKILL after 5 s, every `FlowStep.cancel()` override) is dead outside tests, so a cancelled 6-hour gem5 run keeps burning the node.

**Fix:** key `_active_jobs` by `str(job_id)` on both sides (and in the `finally` at `runner.py:497`); make `_run_job`'s terminal write conditional on the job not already being `cancelled`; send `CancelStepCommand` from `cancel_job` and from the `asyncio.TimeoutError` branch at `runner.py:753`. `tests/integration/test_runner_resume.py:859-890` currently pins the broken behavior and must flip. *Author-flagged (the missing `cancel_step`; the key mismatch was not).*

### H4. Any authenticated user can read, re-key, or delete any other user's credential
`packages/server/src/nexus_server/api/routes/credentials.py:34, 58, 83, 91`; `services/credentials/manager.py:20`
Every credential route uses `CurrentUser`, not `AdminUser`, and **no code anywhere reads `owner_id`, `is_shared`, or `allowed_groups`** — they are written by `ops.create_credential`/`manager.store` and consulted by nothing. Reproduced against the real app: admin creates a private `git_pat`; a `role=user` token then (1) lists it, (2) `PUT`s new fields → 200, after which `CredentialManager.get_by_name` returns the attacker's token, destroying the only copy of the plaintext and silently re-authenticating every step that names it, (3) `DELETE`s it → 204, after which every dependent job fails with `Credential 'x' not found` and any `storage_backends` row referencing it is silently skipped at the next restart.

This is not the documented posture: `deps.py:238-239` lists "credential management" among the routes `require_admin` guards, and `ops.py:1226-1228` explicitly delegates ownership filtering to this route, which never does it. `manager.py:19-20` advertises "access control." Separately, `runner.py:708` resolves `credential_name` with no submitter scoping, so a low-privileged user can have another user's plaintext transmitted over cleartext `ws://` to a node they control.

**Fix:** an authorization helper (`owner_id == user.id or admin or (is_shared and group match)`) applied to list (as a filter), update, delete, and test — or make the mutating routes `AdminUser` to match `deps.py`'s documented contract. Return 404 rather than 403 so ids are not enumerable. Fix the two false docstrings.

### H5. Six shipped steps block the agent's event loop for minutes; the node is marked offline mid-step
`packages/agent/src/nexus_agent/executor.py:174` (+ `:453`, `:566`)
Blocking sites: `git/clone.py:162` (600 s), `git/pull.py:143` (300 s), `package/install.py:193` (600 s), `docker/ensure_container.py:279` (up to 600 s), `gem5/collect_results.py:228` + `:272` (600 s tar + 600 s PUT), `system/update_software.py:241-272`. While one runs, the agent sends no heartbeats, reads no frames, and cannot answer uvicorn's pings — whose defaults (20 s/20 s, confirmed resolved from the real config) close the socket at ~40 s. `ws.py:356` then sets `status="offline"`. The blocked step's own result survives (critical-send retries across the reconnect), but **every other job needing that node fails outright** during the blackout: `scheduler._node_matches_step` rejects non-`online/busy` nodes and `runner.py:677-680` fails immediately with `No available node for step ...` — there is no queueing. Concurrently, a Cancel does nothing because the frame is never read.

**Fix:** two lines in the executor — `state = await asyncio.to_thread(step.startup, params, ctx)` and the same for `check` — which covers all six steps plus `check()`. Update the concurrency note at `executor.py:78-82` in the same change. Add `await asyncio.to_thread(detect_capabilities)` at `connection.py:317` (~25 s inline on every reconnect). *Author-flagged.*

### H6. CPython's `subprocess._cleanup()` steals the reap — a successful step is reported FAILED with exit_code -1
`packages/steps/src/nexus_steps/shell/run_command.py:159` (also `run_script.py`, `python/run.py`, `gem5/run_simulation.py`)
All four polling steps abandon the `Popen` object and keep only `pid`, then reap with `os.waitpid(pid, WNOHANG)`. `Popen.__del__` appends the unwaited object to module-global `subprocess._active`, and `subprocess._cleanup()` — which runs at the top of **every subsequent `Popen` construction anywhere in the agent process** — polls it and consumes the zombie. `check()` then hits `ChildProcessError`, sets `exit_code = -1` and returns FAILED. Reproduced on Python 3.14.2 for `run_command`, `run_python`, and `run_simulation`. Real shape: two jobs share a node (the scheduler deliberately piles them on), job A's command finishes at T, job B's `startup()` spawns at T+0.2 s and steals the reap, job A's next poll reports FAILED for a command that exited 0. Worst case is a multi-hour gem5 simulation discarded this way. The AI Note at `run_command.py:192-200` assumes the only route to that branch is a double `check()` and misses the in-process reaper entirely.

**Fix:** hold the `Popen` (`self._proc = proc`; the step instance lives for the whole run via `_RunningStep`) and reap with `self._proc.poll()`, falling back to `waitpid` only when absent. `tests/unit/test_steps/test_shell_steps.py:166-192` pins the pre-reaped case as FAILED and must be updated.

### H7. The node API key leaks through three channels that the 0600 config file exists to prevent
`packages/agent/src/nexus_agent/connection.py:239` (high) · `packages/server/src/nexus_server/services/provisioner.py:528` + `INSTALL_SH:159` (medium) · `packages/server/src/nexus_server/api/routes/nodes.py:474` (low)
`_connect_and_run` logs the full URL — `ws://host:8000/ws/agent/<id>?api_key=<KEY>&node_id=<id>` — at INFO on every connect and reconnect, into an `agent.log` created by `nohup ... >agent.log` with the remote shell's umask (typically 0644), while `config.save()` chmods `config.json` to 0600. This contradicts `config.py:16-19` and `main.py:206-208`, which both assert the key is never logged. The key is also passed as argv to `/tmp/nexus-install.sh` and again to `nexus-agent init`, readable via `/proc/<pid>/cmdline` for the whole multi-minute install on Linux. And `reconnect_node`'s response body streams `tail -n 6 agent.log` back to the dashboard on the **success** path, so the key the docstring promises is "never re-disclosed" renders on screen.

Escalation is concrete: a node key attaches to `/ws/agent/{id}`, `connect_agent` silently displaces the real agent's registry entry, and `execute_step` frames carry `credential_config` in plaintext.

**Fix:** log `self.config.server_url` only (or scrub the query string); pass the key over the already-open SFTP channel as a 0600 file or on stdin instead of argv; run `line.replace(api_key, "***")` over the provisioner log before returning it. **Rotate existing node keys after fixing — current logs already contain them.**

### H8. Crash recovery converts running jobs into instantly-failed jobs
`packages/server/src/nexus_server/runner/resume.py:64` + `packages/server/src/nexus_server/api/routes/ws.py:308`
`resume_active_jobs` is awaited inside the lifespan **before** `yield`, so ASGI has not begun accepting connections and `agent_connections` is necessarily empty; `_execute_remote_step` has exactly one placement attempt and one send attempt with no retry. Both branches reproduced: graceful restart → `No available node for step 'run_command'`; hard crash → `Agent for node <host> not connected` (node rows still read `online` because the teardown `finally` never ran, and nothing resets or expires `node.status` — no startup reset, no heartbeat-expiry sweep, `heartbeat_timeout_seconds` is never read). Every resume test uses `{"step": "sleep"}`, so the remote case — the one that matters — is untested. Fixing this immediately exposes the position-scoped completion key (see **T4**): the pre-restart execution's `step.completed`, which the agent retries for ~90 s across reconnects, would be consumed as the re-dispatched step's result.

**Fix:** mark all nodes offline during lifespan startup; move `resume_active_jobs` after `yield` plus a grace period; give the remote path a bounded retry (~`heartbeat_timeout_seconds`) for "no eligible node" and "agent not connected" — which also fixes submitting a job seconds before a node reconnects. Note that writing the job back to `queued` is *not* sufficient: nothing polls for queued jobs.

### H9. One non-reading dashboard client stalls step processing cluster-wide
`packages/server/src/nexus_server/api/routes/ws.py:199`
Every agent step handler awaits `broadcast_to_dashboards`, which sequentially awaits `ws.send_json` per client with no timeout and no per-client queue. With the installed uvicorn 0.44 + websockets 16 stack, `asgi_send` → `write_frame` → `drain()` parks on a flow-control future once ~64 KB is buffered for a peer that stops reading; it does not raise, so the `except` never fires. The strongest refutation — "keepalive ping will time out and kill it in ~40 s" — **fails**: `keepalive_ping` itself does `await self.ping()` → `write_frame` → `drain()`, so it blocks on the same paused transport and never reaches its timeout branch. There is no rescue path. The wedged agent's receive loop never iterates again, its `step.completed` is never read, and the runner burns the full `asyncio.wait_for(..., timeout=7200)` before failing the step with no retry. Every other agent emitting a `step.*` frame wedges the same way. `/ws/dashboard` needs no auth, so this is an unauthenticated availability lever.

**Fix:** `asyncio.wait_for(ws.send_json(...), timeout≈1s)` treating timeout as a stale client, or a bounded per-client `asyncio.Queue` drained by its own task. Authenticating the endpoint reduces exposure but does not fix head-of-line blocking — a suspended laptop with an authenticated tab does it too. *Author-flagged as "latency," which understates it.*

### H10. Two-thirds of the admin console calls endpoints that do not exist
`frontend/src/pages/Admin.tsx:153` · `packages/server/src/nexus_server/api/routes/__init__.py:18`
Eight raw `fetch()` calls target `/api/admin/users*` and `/api/admin/groups*`; the live route table (enumerated from the constructed app) contains no path matching `admin`, and git history shows no deleted module — it was never implemented. Because `fetchUsers`/`fetchGroups` only assign `if (res.ok)` and swallow everything else, the Users table reads "No users found." forever and Groups reads "No groups created yet." An admin can never see, promote, demote, or deactivate an account from the dashboard. The one working control is Create User (it uses the real `POST /api/auth/register`), so operators can mint accounts they can never afterwards see or disable. These fetches also bypass `request()`'s 401 → clear-token → `/login` policy, so an expired session is indistinguishable from an empty database. `Admin.test.tsx` passes because it invents the server it tests against.

**Fix:** implement the router (`ops.py` already has `create_group`/`list_groups`/`add_user_to_group`/`remove_user_from_group`/`set_group_pool_access`/`list_users`, all currently caller-less) behind `AdminUser` and mount it, or delete the two tabs. Route the calls through `api.request` with shared `@/types`, and add a test asserting every path `Admin.tsx` fetches resolves to a real route.

---

## MEDIUM

### M1. `${var}` interpolation is published in OpenAPI and implemented nowhere; `-> $captures` is dropped
`runner.py:691` · `schemas.py:239` · `models.py:457, 520` · `ops.py:1013` · `parser.py:383`
`StepContext.resolve` is a two-line dict merge; no regex, no string walk, on the server or the agent. So implicit chaining works only when a downstream param name *exactly equals* an upstream `OUTPUT_KEY`, and a `${...}` string ships verbatim. Because `executor.py:382` uses `create_subprocess_shell` with no `env=`, the shell expands the unset name to the empty string: `cp build/out ${clone_path}/artifacts/` writes to `/artifacts/` and exits 0, so the step reports success and the job completes wrong. The parser's `_captures` list (`parser.py:383`) is emitted and discarded by `StepConfig`'s `extra="ignore"`, and `tests/unit/test_parser.py:110-121` pins the pass-through on the explicit premise that the runner resolves it. Reachability is currently limited (the CLI is a stub, the JobBuilder offers no `${}` affordance), which is the only reason this is medium.
**Fix:** implement a template pass over strings/lists/dicts (reuse `parser._VAR_REF`) in `StepContext.resolve` or before `resolve_for_os`, with an explicit decision for unknown names, and feed `_captures` into the context key names — **or** strike the claim from all six docstrings and the grammar.

### M2. Submit-time validation is strictly more permissive than execution
`packages/server/src/nexus_server/api/routes/jobs.py:259`
`val_ctx.outputs.update(step_cfg.params)` injects every step's *param names* into the validation context, and `RequiredRule` treats a name present in `context.outputs` as satisfied. The runner never puts params into the runtime context (`runner.py:421-422` merges only declared `OUTPUT_KEYS`), so a step genuinely missing a required param is satisfied by an unrelated earlier step's same-named param. Verified: two `run_command` steps with the second missing `command` → 201; the same step alone → 400. At execution the agent raises `ValidationError: command Field required` — after step 0 has burned its full runtime (hours, for a gem5 step). The params merge is not even load-bearing for the gem5 chain it was presumably added for (`gem5_run_simulation` already declares `m5out_path` in `OUTPUT_KEYS`).
**Fix:** seed `val_ctx.outputs` from `OUTPUT_KEYS` only; if concrete values are needed for pass-3 typing, keep them in a separate dict the rules do not consult. Amend `jobs.py:223-226` and `base.py:66-67`.

### M3. Per-step `target_node_id`/`target_pool_id` 500s at submit
`packages/server/src/nexus_server/api/routes/jobs.py:263`
`[s.model_dump() for s in body.steps]` leaves `uuid.UUID` objects inside the list bound to the `jobs.steps_config` JSON column, whose serializer is plain `json.dumps` (the engine sets no `json_serializer`) → `StatementError: Object of type UUID is not JSON serializable` → bare 500, no row written. Per-step targeting is a first-class documented feature (`StepConfig`, the DSL's `_STEP_KEYWORDS`, `runner.py:398-402`) that cannot be submitted through the API at all. Job-level targets survive only because `ops.create_job` runs them through `_sid()`.
**Fix:** `s.model_dump(mode="json")`. Add a route test that submits a step-level pin.

### M4. `on_fail` is an unvalidated string and the runner fails **open**
`packages/common/src/nexus_common/models/schemas.py:256` · `packages/server/src/nexus_server/runner/runner.py:451`
`if on_fail == "stop"` means every other value — `"Stop"`, `"STOP"`, `"abort"`, `5` — takes the continue path. All four reproduced end to end: `"Stop"` on a failing step yields `steps [(0,'failed'),(1,'success')]` and `Job.status="completed"`. The operator sees a green job for a failed deploy. `tests/unit/test_parser_edge_cases.py:995-1001` explicitly defers enum validation to "the schema layer" / "POST /api/jobs is the validation boundary" — a boundary that does not validate.
**Fix:** `on_fail: Literal["stop","continue"] = "stop"` (422 at submit) **and** invert the runner to `if on_fail != "continue":` so unknown values fail closed.

### M5. No terminal job frame is ever broadcast — finished jobs stay "Running"
`packages/common/src/nexus_common/agent_protocol.py:384` · `runner.py:451-472` · `frontend/src/stores/index.ts:416`
`DashboardJobCompleted` has zero construction sites, and the runner's three terminal writes (failed / completed / cancel) call `ops.update_job` with no `ws` call. The last live frame a finishing job emits is `job.status {status:"running"}` from `ws.py:467`. `Jobs.tsx` fetches only on mount and filter change and `Dashboard.tsx` has no interval, so the row keeps its Running badge with a duration counting from `Date.now()` indefinitely. `ws.py:464-466`'s comment ("The runner emits the job's terminal status once it advances past the last step") is simply false.
**Fix:** broadcast from the three terminal sites via the ws manager the runner already holds. Note `completed_at` on that model is a bare `datetime`, not `UTCDateTime` — pass it tz-aware or this path re-hits the already-fixed naive-timestamp bug.

### M6. `StorageManager` registry is `str`-keyed and looked up by `UUID`
`packages/server/src/nexus_server/services/storage/manager.py:113` · `api/routes/storage.py:284`
`_new_uuid()` returns `str(uuid.uuid4())` into a `String(36)` column, so `init_backends` keys by `str`; the routes declare `backend_id: UUID` and `get_backend` does a bare `in` test. Every healthy backend reports `{"healthy": false, "error": "Backend not initialized"}` — a red dot with a message whose own AI Note misdiagnoses it as "registered after startup" — and `POST /api/storage/transfer` always 404s on its destination (the *source* lookup uses the ORM's `str` and hits, which is why only the destination fails).
**Fix:** `str()` coercion in `init_backends` and at the top of `get_backend` (the `_sid()` pattern). Flip `test_storage_routes.py:529`, collapse `_register_fake_both`, update the `Storage.test.tsx` note. *Author-flagged.*

### M7. The artifact pipeline is dead end to end, and the UI links into it
`db/ops.py:1395` · `services/storage/manager.py:262` · `api/routes/artifacts.py:10` · `frontend/src/pages/JobDetail.tsx:966`
`ops.create_artifact`'s only caller is `StorageManager.upload_artifact`, which has zero non-test callers; gem5 results instead go to `PUT /api/jobs/{id}/results` → a local tarball. So `GET /api/artifacts?job_id=` always returns `[]`, nothing is ever written to MinIO, and no error is logged — an operator who configures a backend concludes it works. There is also **no route serving artifact bytes**: `artifacts.py` registers only `GET ""`, and `artifacts.py:10-11` and `storage.py:26-27` each name the other as the download owner while `ops.get_artifact_by_id` claims to be "the entry point for the download endpoint." `JobDetail.tsx:966` renders a link to `/api/artifacts/{id}/download` → verified 404.
**Fix:** decide. Either call `upload_artifact` from the results route and implement the download endpoint (a plain `<a href>` cannot carry the Bearer token — needs cookie or signed URL), or mark the subsystem unwired in `models.py:389-412`, `ops.py:1395` and the storage docstrings and remove the UI link and its test. ~400 lines of manager/backend code plus ~1,500 lines of tests are validating a path nothing invokes.

### M8. The pool ACL is documented as the job-submission gate and is enforced nowhere
`db/ops.py:451` · `api/deps.py:276`
`check_user_pool_access` has exactly one caller — the never-mounted `require_pool_access` factory. `submit_job` performs no pool or node authorization; verified: a non-admin with no group membership submits with `target_pool_id` for an admin-created pool → 201, and her command runs there. Three docstrings assert the opposite (`ops.py:450-452`, `ops.py:806-807`, `deps.py:275-277`). Severity is held at medium only because no route exists to *create* a `GroupPoolAccess` grant (`set_group_pool_access`, `create_group`, `list_groups`, `remove_user_from_group`, `list_users`, and the three template helpers all have zero production callers), so no operator can currently be relying on a restriction they configured. The danger is entirely for whoever adds restricted pools next.
**Fix:** enforce it in `submit_job` and on the per-step `target_pool_id` override, and add the admin surface (H10) so grants can exist — or delete the ACL model and rewrite the four docstrings.

### M9. Agent death mid-step never wakes the runner — the job parks for 2 hours
`packages/server/src/nexus_server/runner/runner.py:745`
`agent_websocket`'s `finally` calls `disconnect_agent` + `update_node(status="offline")` + broadcast, but never notifies the runner, and `ws.app.state.runner` *is* in scope there. The only setters of a step Event are inbound agent frames. `heartbeat_timeout_seconds` exists in config and is never read. A power-cycled node 5 s into a 30 s command leaves the coroutine parked for the full 7200 s, then fails with the misleading "Step execution timed out (2h)"; the StepRun stays `running`, the job stays `running` on a node the same API reports offline, and the job's dedicated `AsyncSession` is pinned for two hours.
**Fix:** a grace-period reaper, **not** an on-teardown kill — `connection.py:274-281` deliberately does not cancel step tasks on reconnect and delivers results over the next socket, so failing in-flight steps on every teardown would regress that design. Track `key -> node_id` (already in hand at `_execute_remote_step`) and fail after `heartbeat_timeout_seconds` of disconnection.

### M10. A late WS teardown evicts the reconnected socket, and no heartbeat ever restores it
`packages/server/src/nexus_server/api/routes/ws.py:353`
The `finally` calls `disconnect_agent(node_id)` unconditionally, so a slowly-unwinding session A evicts session B's socket and writes `offline`. The in-code note and the pinning test both claim recovery "on the next heartbeat" — verified false: the heartbeat branch only writes DB status and never calls `connect_agent`. The DB flaps back to `online` with an empty registry entry, `_node_matches_step` accepts the node (it reads only `node.status`), `send_to_agent` returns False, and every step targeting it fails instantly with "Agent for node X not connected" — permanently, because the server keeps acking the agent's heartbeats so neither side reconnects, and there is no step retry.
**Fix:** evict only when `agent_connections.get(node_id) is ws`; have `connect_agent` close the socket it displaces; re-assert registration in the heartbeat branch. Flip the test and correct its docstring. *Author-flagged.*

### M11. The agent silently discards `credential_config` — every credentialed step runs unauthenticated
`packages/agent/src/nexus_agent/executor.py:159`
`execute()` never reads `cmd.credential_config` and `StepContext` has no field to carry it. Worse, the server pops `credential_name` out of params as a security boundary (`runner.py:696-701`), so the step cannot recover the reference either. A `git_clone` on a private repo with `credential_name: gitlab-token` runs unauthenticated and fails with an auth error after up to 600 s, with nothing in the job log indicating the configured credential was never applied.
**Fix:** add `credential: dict | None` to `StepContext`, populate from `cmd.credential_config`, and have `git_clone`/`git_pull` consume it. Until steps do, fail loudly at submit or dispatch when `credential_name` is set but unread. *Author-flagged in three places.*

### M12. The `timeout` param is published to the UI and enforced nowhere
`shell/run_command.py:179` · `shell/run_script.py` · `python/run.py` · `gem5/run_simulation.py`
All four publish `timeout: int = Field(3600, description="Maximum execution time in seconds.", ge=1, le=86400)`, store it in state, and never record a start timestamp or compare elapsed time. Verified: `startup({'command':'sleep 6','timeout':1})` then `check()` at 1/2/3/4 s returns RUNNING every time. `_poll_step` has no timeout guard either, so the job is pinned to the 7200 s ceiling, then reported "Step execution timed out (2h)" with no `cancel_step` sent — the agent's poll loop and the child keep running and `active_count` never drops until the agent restarts.
**Fix:** record `started_at` in state and enforce it in `check()` (killpg → `exit_code=-1` → FAILED), or delete the field from the three unflagged schemas.

### M13. No error surface: swallowed rejections, latched spinners, `[object Object]`
`stores/index.ts:140, 173, 210, 246, 273, 301` · `Storage.tsx:224` · `Jobs.tsx:166` · `Nodes.tsx:1248` · `Admin.tsx:952, 997` · `client.ts:115`
One rejected list GET pins `isLoading: true` forever and raises an unhandled rejection: `/storage` shows a spinning `Loader2` where the backend grid belongs (while the transfers table below renders fine, so it reads as a partial hang), `/jobs` shows a spinner row recoverable only by changing the filter, and `Dashboard` — which deliberately has no loading gate — renders an outage as an empty cluster with zero tiles. `Admin.tsx:997`'s `// handled` comment sits on an empty catch with no error state, so a concurrent credential delete (404) is indistinguishable from success. And `client.ts:115` throws `new Error(body.detail)` where a 422 `detail` is an array, so `Storage`'s "Add Backend" — whose Credential select defaults to `""` with no `required`, i.e. the **default** path — renders "[object Object]". The store's own AI Note prescribes page-level `.catch()`, which cannot clear the store's flag.
**Fix:** `try/catch/finally` + an `error` slice in the six stores (7 edits vs ~11 call sites); render error + Retry in Storage/Jobs/Dashboard/Nodes; normalize `detail` arrays in `request()` (`provisionNode` already does half of this — hoist it); replace the `// handled` lie. Update `stores/index.ts:22-24` and flip `stores/index.test.ts:236-243`. *Partly author-flagged.*

### M14. The global 401 handler hijacks the login request itself
`frontend/src/api/client.ts:104`
`request`'s 401 branch has no path exemption, so a wrong password (`auth.py:98`, a deliberate generic 401) clears the token and assigns `window.location.href = "/login"` — a same-URL document reload that destroys the banner `Login.tsx:80` just set and blanks the form. If the tab already held a live session, the failed attempt also wipes the good token. `frontend/e2e/smoke.spec.ts:59-67` asserts the error text stays visible and the URL stays `/login`, i.e. the project's own spec contradicts the implementation; `Login.test.tsx` passes only because it mocks the store action.
**Fix:** exempt `/auth/*` (or add a `skipAuthRedirect` option) and propagate `body.detail` so the banner reads "Invalid credentials".

### M15. `useWebSocket` cleanup leaves a self-reconnecting orphan socket
`frontend/src/hooks/useWebSocket.ts:127`
Cleanup clears the pending timer then calls `close()`, but `onclose` fires *after* cleanup returns and unconditionally schedules `connect()`, which reassigns `wsRef.current` with nothing left to close it. Each mount/unmount cycle permanently adds a live dashboard socket, so after logout→login every `step.log` line is appended twice by the module-level handler and JobDetail shows duplicated output; StrictMode's double-invoked effect produces the same orphan on first mount in dev. Invisible to tests because `FakeWebSocket.close()` never fires `onclose`.
**Fix:** null `onclose` before `close()` (or add an `unmounted` generation ref checked in `onclose`/`connect`), and make the fake fire `onclose`. *Author-flagged as "one orphan timer" — it is an orphan socket.*

### M16. Step attempts have no identity — loop iterations corrupt each other's state, status, and logs
`db/ops.py:1124` · `runner/runner.py:727` · `frontend/src/pages/JobDetail.tsx:566`
`get_latest_step_run` orders by `StepRun.id.desc()`, a lexicographic sort over random uuid4s, and `ws.py:432-442` is the *only* mechanism for locating the row for an incoming `step.started` (nothing carries a `step_run_id` on the wire). With a backward `jump`, index 3 has attempt A (`success`, `finished_at` set) and attempt B; the frame for B flips A back to `running` with a fresh `started_at` and possibly the wrong `node_id` ~50% of the time, while B keeps `state=NULL` so crash recovery for it has no resume handle. The completion key `"{job_id}:{step_index}"` is position-scoped, so an abandoned earlier execution's `step.completed` is consumed as the current dispatch's result (reproduced) — currently masked by H8, and live the moment H8 is fixed. The UI keys live logs on the array index, so for `[run_command, run_script, jump(target=1,max_jumps=3)]` the 9 returned rows (ordered by `step_index`: 0,1,1,1,1,2,2,2,2) mean clicking indices 3-8 reads keys nothing writes → "No log output yet" while the step streams, and index 1 merges four iterations with no separator.
**Fix:** put the `StepRun` id (or a per-dispatch nonce) into `ExecuteStepCommand` and echo it in `StepStarted`/`StepCompleted`/`StepFailed`; key `_step_events`/`_step_results` on it and drop non-matching frames; add a monotonic `seq`/`created_at` ordering to `step_runs`; key the UI on `steps[selectedStep].step_index`. Flip the strict xfail at `test_db_ops.py:678`, and correct the two AI Notes at `runner.py:263-267` and `755-762` which assert the guarantee the code does not provide. *Partly author-flagged.*

### M17. `POST /api/auth/register` commits an invalid role, then 500s serializing it
`packages/server/src/nexus_server/api/routes/auth.py:76`
`RegisterRequest.role` is a bare `str` with no validator and `users.role` is `String(16)` with no CHECK, while `UserInfo.role` is the `UserRole` enum. `{"role":"superuser"}` → row inserted **and committed**, then `UserInfo(...)` raises → unhandled 500. The admin believes creation failed, but the account exists and can log in (`TokenResponse` carries no `UserInfo`); `GET /api/auth/me` then 500s for that user forever so their dashboard cannot boot, and `require_admin`'s string compare denies them everything.
**Fix:** `role: UserRole = UserRole.USER` on the request model so FastAPI 422s before any write.

### M18. `/ws/dashboard` is unauthenticated and streams every job's live stdout
`packages/server/src/nexus_server/api/routes/ws.py:536`
The handler takes only `ws` and never reads the `?token=` the frontend already appends. `websocat ws://nexus:8000/ws/dashboard` with no credentials receives, in real time, every node hostname and status transition, every job id + step index, and — because `ws.py:497` forwards the raw inbound dict — the verbatim stdout/stderr of every running step, which routinely includes tokens echoed by build scripts, `git clone https://user:token@...` URLs, and env dumps. Two tests pin the gap as characterization (`test_dashboard_ws_connects_without_authentication`, `..._with_a_bogus_token_is_still_accepted`).
**Fix:** validate the query token with `AuthService` before `connect_dashboard`; close 4001/4003 otherwise. No client change needed; flip the two tests. *Author-flagged.*

### M19. `_unhide_editable_pth()` globs the base interpreter's lib dir, so the self-heal it exists for never runs
`packages/steps/src/nexus_steps/system/update_software.py:83`
`Path(venv_python).resolve()` follows the venv's `bin/python` symlink out to the framework interpreter, so the glob targets `/Library/Frameworks/.../lib` and returns `[]` on any stock `python -m venv` layout — which is exactly what `INSTALL_SH` builds. `dev.sh:181` performs the identical fix with the correct un-resolved path, which is the proof this is a bug and not a policy. On macOS, if setuptools leaves `UF_HIDDEN` on a freshly written `_editable_impl_nexus_common.pth`, the step reports `done: True`, schedules the restart, and the restarted agent dies with `ModuleNotFoundError: nexus_common`. The node is permanently offline and `update_software` — the only self-heal path — cannot be re-run; the SSH re-provision path carries no `chflags` either.
**Fix:** drop `.resolve()`, or ask the interpreter (`sysconfig.get_paths()['purelib']`). Add a test asserting the globbed directory is inside the venv, and add the `chflags` line to `INSTALL_SH`.

### M20. gem5 `check()` runs an unguarded probe *after* recording success
`packages/steps/src/nexus_steps/gem5/run_simulation.py:349`
Container mode writes `state["exit_code"] = 0` at `:341`, then runs `docker exec <c> test -f <stats>` at `:349` with `timeout=30` and no try/except. A wedged Docker daemon → `TimeoutExpired` escapes `check()` → `_poll_step` (no handler) → `execute()`'s generic handler → `StepFailed(error="... timed out after 30 seconds", exit_code=0)`. Six hours of simulation is marked failed, the chained `gem5_collect_results` never runs, and the m5out data is never uploaded — while the step's own note at `:321-323` says a missing `stats.txt` must **not** fail the step. Same unguarded shape at `:230-233` (container `mkdir -p`).
**Fix:** `except (subprocess.SubprocessError, OSError): found = False` so a failed probe means "stats not found," not "step failed."

### M21. Provisioner: fixed `/tmp` path with no `O_EXCL`, executed in a separate round trip
`packages/server/src/nexus_server/services/provisioner.py:520`
`sftp.putfo(BytesIO(INSTALL_SH), "/tmp/nexus-install.sh")` uses paramiko's `wb` (`O_WRONLY|O_CREAT|O_TRUNC`, no `O_EXCL`/`O_NOFOLLOW`) and `bash /tmp/nexus-install.sh ...` runs at `:527`. The symlink variant is blocked on default systemd Linux (`fs.protected_symlinks=1`) but works on macOS, which this module explicitly supports. The stronger platform-independent variant: an unprivileged local user pre-creates `/tmp/nexus-install.sh` mode 0666 that *she owns*; `putfo` truncates and writes into her file, she rewrites it in the window before `:527`, and the provisioning user (commonly root) executes her script — plus the api_key is handed to her in argv (H7).
**Fix:** upload to an unpredictable per-run path with exclusive semantics (`mktemp`, or `secrets.token_hex(16)` under the SSH user's `$HOME`), execute, `rm -f`. Combined with moving the key off argv this closes both provisioner findings.

### M22. Pagination is unbounded on one end and absent on the other
`api/routes/jobs.py:141` · `frontend/src/pages/Jobs.tsx:166`
`limit: int = 50, offset: int = 0` carry no `Query(ge=, le=)`, so `?limit=-1` renders as SQLite `LIMIT -1` = unlimited (verified) and `?offset=-3` is silently ignored rather than 422'd. Meanwhile no frontend caller ever sends either, and `Jobs.tsx` renders that 50-row `created_at DESC` window with no pager and no truncation hint — and the WS feed only *patches* jobs already in the array, so a job outside the window can never appear. Queue 60 jobs behind a long gem5 run and the running job is the 61st newest: it is absent from the Jobs table (so it cannot be cancelled — that table holds the only cancel affordance) and the Dashboard "Active Jobs" tile reads 0 while the cluster is busy.
**Fix:** `Query(50, ge=1, le=200)` / `Query(0, ge=0)` server-side; offset/limit state + pager in `Jobs.tsx`; server-side aggregates for the Dashboard tiles.

### M23. `execute()`'s except handlers can raise, producing zero terminal messages and a 2-hour hang
`packages/agent/src/nexus_agent/executor.py:260`
The docstring promises "Never raises," but terminal-message emission inside both handlers is unguarded: `_capture()` calls `state.get(...)` on whatever `startup()` returned (`AttributeError` at `:498`) and `StepFailed(exit_code=...)` validates a state-supplied value. A step whose `startup()` falls off the end returns None → `StepStarted` ValidationError → handler → `AttributeError` escapes; `execute()` is a detached `create_task` so nothing retrieves it, and the runner blocks 7200 s. No shipped step triggers either path (AST-verified), so this is a hardening gap on the pluggable-step boundary — but that boundary is the product's extension point.
**Fix:** a `_send_terminal()` helper wrapped in try/except with a minimal `StepFailed` fallback; `isinstance(state, dict)` check right after `:174`; make `_capture()` honor its own "never raises" docstring.

### M24. Constraint violations and malformed bodies surface as bare 500s
`pools.py:134`, `:271` · `credentials.py:39-51` · `storage.py:164` · `credentials/manager.py:63` · `credentials/strategies/__init__.py:207` · `jobs.py:462`
`pools.name`, `credentials.name`, `storage_backends.name` are all `unique=True`; `pool_node_memberships` has a composite PK. None of the create/update routes pre-checks or catches `IntegrityError`, and `create_app()` registers no exception handler, so a duplicate name (or re-adding a node already in a pool, or a partially-retried batch that leaves earlier nodes committed) returns 500. `auth.py:163-166` flagged exactly this hazard for usernames and handled it with a 409 pre-check; the sibling routes did not get the same treatment — and `Admin.test.tsx:1453` mocks the rejection as `new Error("Credential name in use")`, so the intended contract is clearly a 4xx. Adjacent: `CredentialCreate.fields` is an untyped `dict`, so `{"username": 123}` passes `validate()` and raises `AttributeError: 'int' object has no attribute 'strip'` inside `serialize()` → 500; and the results-manifest handler catches `(tarfile.TarError, OSError)` but not `EOFError`, which is exactly what a truncated (valid-gzip-header) upload raises — the case its own docstring says it exists to report.
**Fix:** one app-level `IntegrityError` → 409 handler plus per-route pre-checks (and idempotent pool membership: skip existing members, return them in `skipped`); `fields: dict[str, str]` on `CredentialCreate`; broaden the manifest handler to `(tarfile.TarError, OSError, EOFError, zlib.error)` and make results uploads atomic (`.part` + `os.replace`). Correct the `pools.py:257, 262-266` note, which says "retrying the same request is safe."

---

## LOW — grouped by root cause

### L1. Stale or false docstrings that contradict the code they annotate
The single largest low-severity cluster, and the one with the highest maintenance cost given this repo's conventions.
`ops.py:887-889, 1251-1253, 1285-1286, 1652-1653` (four notes claim IDs are bound raw; all four lines now call `_sid()`, contradicting the module header at `ops.py:36-40`) · `models.py:219-220` (`Node.status` documented as only offline/online; `nodes.py:584` writes `maintenance`, and `busy` is read by the scheduler with no writer) · `deps.py:60-65` (claims a missing Authorization header yields 403; FastAPI 0.135.3 returns 401, as the tests and `client.ts:104` assume) · `client.ts:381` vs `schemas.py:274` vs `ops.py:819` (three mutually contradictory `priority` conventions — and no scheduler code reads `Job.priority` at all, so the JobBuilder's high=10 would become the *lowest* priority if the schema's "lower wins" is ever implemented) · `Storage.tsx:296-299` (states the JSON-parse early return sits *before* the try/finally; it is inside it, so the documented rule is inverted and the manual clear at `:324` is redundant) · `Nodes.tsx:916-920` (claims `hostname`/`cpu_model` `"pending"` are "the sentinels the rest of the UI keys off"; nothing reads either, and `hostname` is overwritten by the Display Name at `:922` — only `ip_address === "0.0.0.0"` is consumed) · `base.py:264-265` (claims `to_schema()` serializes `context_key` "so the UI can explain the chain"; `InputRuleSchema` at `schemas.py:400-415` has no such field and `extra="ignore"` drops it) · `types/index.ts:177` + `client.ts:312` (`has_log` documented as the flag the detail page uses; `JobDetail.tsx:679-686` gates only on `has_results`, and `JobDetail.test.tsx:551` pins the log tab as always-present, so the *docs* are wrong) · `stores/index.ts:334-335` (`clearLogs` documented as "called when the detail page mounts"; zero callers).
**Fix:** correct each in the same PR as the related code change. Where the claim describes an unimplemented feature, say "not implemented" rather than deleting the sentence.

### L2. Dead code and dead configuration
`executor.py:36-37` (unused `signal`, `tempfile` — two ruff F401s) · `docker/ensure_container.py:33` (unused `tempfile`) · `os_adapters/base.py:56` (`package_install`, `resolve_path`, `os_type` have zero production callers, and `package_install` has already diverged from the authoritative `_INSTALL_COMMANDS` at `package/install.py:77` — sudo present there, deliberately absent in `linux.py:50`; in fact the whole adapter layer is inert, since `shell_command`/`temp_dir` are reached only via `_run_subprocess`, which no shipped step triggers) · `ops.py:1553` (eight helpers — templates ×3, groups ×4, `list_users` — with no router; the `SavedTemplate` table and `TemplateCreate`/`TemplateInfo` schemas are unreachable) · `ops.py:46, 51` (unused `uuid`, `func`, `update` imports; section banner glued onto the `return` at `:994`) · `parser.py:383` (`_captures`, see M1) · `scheduler.py:82` (`get_step` resolved a second time; the documented `KeyError` contract is unreachable from its only caller) · `JobBuilder.tsx:120` (`STEP_CATEGORIES` keys `shell`/`flow`/`system` match no registered step; 8 of 13 steps — including all three `run_*` — land in "Other").
**Fix:** delete, or wire up and test. Note the OSAdapter divergence is the dangerous kind: a maintainer can "fix" `linux.py:50` and change nothing at runtime.

### L3. Resource leaks and cleanup gaps
`shell/run_command.py:147` (+ `run_script.py:141`, `python/run.py:232`, `gem5/run_simulation.py:257`) — eight `NamedTemporaryFile(delete=False)` log files per four step types, unlinked by nobody; verified still on disk after a full startup/check/cancel cycle. Plus `run_simulation.py:246`'s direct-mode `mkdtemp(prefix="nexus_m5out_")` directory and `run.py`'s inline-code temp `.py` on the cancel/`ChildProcessError` paths. **Fix:** unlink `state["stdout_path"]`/`stderr_path` in `StepExecutor.execute()`'s `finally` after `_capture()` has read them — one change covering all four steps. Handle m5out separately (`gem5_collect_results` deliberately leaves it). *Author-flagged.*
`stores/index.ts:91` — `logout()` clears only tokens and `user`; the six resource stores and the live-log store are module singletons that survive into the next session, most visibly on the Dashboard (which deliberately has no loading gate). **Fix:** `resetAllStores()` called from `logout()`.

### L4. Agent robustness edges
`executor.py:413` — `create_subprocess_shell`'s StreamReader has a 64 KiB limit, so `readline()` raises `ValueError` on any output run without a newline; the exception aborts the gather before `process.wait()`, leaves the sibling reader pending, never kills the child, and the `finally` pops `_running_steps` so nothing can reach it again (verified: the child outlived the interpreter). Latent — no shipped step uses this path — but the AI Note at `:406-408` asserts `readline()` is unbounded. `executor.py:531` — `_CAP_BYTES` is documented as a byte cap but enforced on characters, so CJK output overshoots ~3× (bloat only; uvicorn accepts 16 MiB frames). `capability.py:131` — `/etc/os-release` parsing guards only `FileNotFoundError`; a `PermissionError` yields an endless non-registering online/offline flap and a `UnicodeDecodeError` (a `ValueError`, not in `run()`'s transient tuple) kills the process. **Fix:** `encoding="utf-8", errors="replace"` and `except (OSError, ValueError)`. *Author-flagged.* `config.py:212` — `load()`'s file branch drops the uuid fallback its three siblings carry, so `"node_id": null` in a hand-written config yields a `ValidationError` that kills the process on every start.

### L5. Storage/credential service edges
`encryption.py:30` — `str(InvalidToken())` is `''`, and both consumers format with `{e}`, so a key rotation or a restored DB logs literally `Failed to initialize backend primary: ` and `POST /credentials/{id}/test` returns `{"success": false, "error": ""}`. **Fix:** re-raise typed from `FieldEncryptor.decrypt`; use `logger.exception`/`%r`. `manager.py:375` — no `source == dest` guard, so `delete_source=True` onto the same backend would copy an object onto itself and then delete it, leaving a `completed` transfer row and an artifact pointing at empty bytes (doubly latent today: M6 404s first, M7 means no artifacts exist). `manager.py:397` — the failure handler writes again on a session that needs rollback, so a failure originating from `db.commit()` raises `PendingRollbackError` in place of the real error and the row stays `completed`. `storage.py:378` — `transfer_artifact` never stamps `started_at`/`completed_at`, so `TransferInfo` timestamps are always null and the documented "newest first" ordering degrades to rowid order (verified: listing returns creation order). `strategies/__init__.py:27` — `endpoint_url` by blind concatenation, and `use_ssl` selected by bare truthiness on a value the dashboard can only ever send as a string, so `use_ssl: "false"` selects https.

### L6. Provisioner / callback-candidate discovery
`provisioner.py:289` — the `ip -4 addr` fallback is nested in the same `try` as the `ifconfig` call, so a missing `ifconfig` binary (net-tools is not installed by default on modern Ubuntu/Debian/RHEL) raises `FileNotFoundError` past it and the entire second discovery pass is dead; a dual-NIC host returns only the default-route address, and provisioning aborts with `NO_WS_ROUTE` if the mDNS hostname does not resolve on the device. The pinning test justifies the fallback with exactly this motivating case and then never exercises it. `provisioner.py:318` — the loopback guard is an exact-match tuple, so RHEL's default `localhost.localdomain` passes through as the highest-priority candidate (cost is one fast-failing probe, not the 6 s the test docstring claims). **Fix:** split into independent try blocks; match the `localhost.` family or resolve into 127.0.0.0/8.

### L7. Validation-rule edges in the shared contract
`base.py:521` — `ContextSatisfiableRule`'s documented pattern ("required *unless* an upstream step provided it," with a differing `context_key`) cannot be used with a required schema field: pass 3 re-validates `ctx.resolve(params)`, and `resolve` merges strictly by key name, so a differently-named key yields "Field required" *even when a real value is present* and a same-named None placeholder yields "Input should be a valid string." Only `gem5_collect_results` uses the rule, and only in the same-name/all-optional shape that works. `base.py:197`, `:294-295` — `RequiredRule`/`AtLeastOneRule` test key membership, so an explicit JSON `null` satisfies pass 2 and the 400 surfaces as a raw Pydantic dump under the pseudo-field `_schema` with `input_value={}` — matching nothing the caller sent. **Fix:** `params.get(f) is not None`; alias `context_key → field_name` in `resolve`, or document that the field must be Optional (which `test_base.py:478-484` already says and the rule's docstring does not).

### L8. Authorization/UX consistency
`jobs.py:534` — no object-level authz on cancel/delete/log/results (verified 204/200 with a non-admin token against an admin's job). Author-flagged three times as an accepted single-tenant assumption, and job submission already implies shell access on every node, so the only genuine delta is destroying another user's *record* and reading their log/results. `credentials.py:65` — `PUT` reuses `CredentialCreate`, where `fields` is required, so metadata-only edits are impossible and a placeholder secret silently and irrecoverably overwrites the real one (verified: `{'token':'PLACEHOLDER'}` → 200, then `get_by_name` returns it). Latent: `client.ts` has no `updateCredential`. The required `credential_type` in that body is also silently ignored. `Nodes.tsx:729` — the maintenance button is the only control in the panel with no `isAdmin` gate, though the endpoint is `AdminUser`. `runner.py:414` — all four local-failure returns in `_execute_remote_step` omit `node_label`, so a node-bound step's failure is logged as running "on control-plane" — including the two cases (`not connected`, `timed out`) where the node identity is known and is the most useful field. `runner.py:352` — `_run_job` unconditionally overwrites `Job.started_at`, so a resumed job under-reports its duration for the rest of its life (verified). `Storage.tsx:332` — `parseInt` on a `type="number"` value silently records `1e12` as `1`. `client.ts:328` — the four hand-rolled fetches skip the module's documented global 401 policy. `stores/index.ts:210` — no request sequencing, so overlapping fetches are last-response-wins and `/jobs` (which renders `jobs` verbatim, unlike `/nodes`) can display rows outside the selected filter.

---

## Already solid — do not churn

- **`services/auth`** — `decode_token` pins `algorithms=[self._algorithm]` (no alg-confusion), `exp` verified, and the `type` claim enforced in *both* directions (`refresh()` rejects non-refresh; `deps.py:205` rejects non-access), bcrypt with per-call salt. The documented trade-offs (no jti/revocation, role snapshotted, sliding refresh) are accurate, not aspirational.
- **`config.py`** — `JWT_SECRET` is `os.environ[...]` with no fallback; the empty `CREDENTIAL_ENCRYPTION_KEY` default hard-fails at startup via `Fernet("")` rather than degrading to plaintext.
- **`parser.py`** — 119 adversarial + 40 happy-path tests (unicode separators, BOM, CRLF/CR, duplicate keys, 200k-char lines, exact error line numbers). Every permissive behavior probed was an intentional, asserted decision.
- **The `_sid()`/`_sid_kwargs` ID-coercion layer** — genuinely complete at the ORM boundary; UUID args round-trip correctly everywhere, pinned by a UUID matrix test. Only four *docstrings* are stale (L1). Do not add redundant `str()` at call sites.
- **`to_schema()` default handling** — round-trips end to end; every registered step's published defaults re-validate cleanly and JSON-serialize. The remaining gap is the JobBuilder's missing list/object *editor* (H10), not the schema.
- **The runner's local mechanics** — Event-before-send, store-result-before-set-Event, unconditional `finally` cleanup, fresh session for the failure write, deliberate CancelledError silence, write-`current_step`-before-execute, and the two-halves `jump`/`max_jumps` counter. 74 tests pass across both runner suites; this is correct and load-bearing.
- **`NASBackend._full_path`** — `resolve()` + parents check genuinely blocks `..`, absolute keys, and planted symlinks.
- **Provisioner shell quoting** — every interpolation into a remote command (`:366, 493, 503, 508, 527-528`) is `_q()`'d; no missing quote on any operator-supplied value. paramiko blocking is correctly behind `asyncio.to_thread`, and the `try/finally` closes the session on every in-try return.
- **Response projections** — every response goes through an explicit `_x_to_info`, so `Node.api_key` and `User.password_hash` cannot leak through `NodeInfo`/`UserInfo`. The `UTCDateTime`/`_iso_utc` alias is applied consistently to every client-facing datetime.
- **The triplicated `_find_docker`** — hashed byte-identical across all three copies. Every state dict across all 13 steps is JSON-serializable; every `OUTPUT_KEYS` entry is actually written; there are no accidental context-key collisions.
- **`formatRelativeTime`** — the clamp fix is correct (`Math.max(0, ...)` + `isNaN` guard, pinned); `useWebSocket`'s backoff ladder matches its docstring exactly; no `dangerouslySetInnerHTML` anywhere in `frontend/src`, so job logs, filenames and manifest paths are all XSS-safe. `JobDetail`'s poll effect correctly keys on `detail?.job.status`, and `buildTree` materializes missing tar ancestors correctly.

---

## Recommended action order

**Tier 0 — urgent, this week.** Each is small, and each currently makes the system lie to its operator.

1. **H3 — `cancel_job`.** One-line key normalization + a guard so `_run_job` cannot upgrade `cancelled` → `completed`, plus sending `CancelStepCommand`. A cancel that returns 200, flips the row to `cancelled`, keeps dispatching steps, and then reports `completed` is the worst failure mode in the repo. Flip `test_runner_resume.py:859-890`.
2. **H4 — credential authorization.** Cross-user secret destruction with no recovery. Add the owner/admin/shared check to list/update/delete/test and scope the runner's `get_by_name` to the submitter.
3. **H7 — the api_key in `agent.log`.** One-line log change, then **rotate every node key**, since existing 0644 logs already contain them. Fix the argv and reconnect-echo channels in the same PR.
4. **H6 — `Popen` retention in the four polling steps.** Silently reporting a successful multi-hour gem5 run as FAILED with `exit_code -1` is worse than a crash, and the trigger (any concurrent step, or a reconnect's `detect_capabilities`) is routine.
5. **H1 — cascades.** ~5 lines in one file that un-breaks four DELETE endpoints, including "you cannot deregister a node that is in a pool."

**Tier 1 — this sprint.**

6. **H2 + the contract test / OpenAPI codegen.** Five one-line client fixes restore five features that have never worked. Do the drift-proofing at the same time (T1) or they will recur — there were already three instances of the identical query-vs-body/key-name shape in a single subsystem.
7. **H5 + M23.** Two `asyncio.to_thread` calls in `executor.py` fix six step findings and the node-goes-offline-mid-step cascade; add the terminal-send guard while in the file. Then `S3Strategy.test_connection` (`to_thread` + a 5 s botocore `Config`) and the bounded `ws.send_json` (H9).
8. **M18 — authenticate `/ws/dashboard`.** The frontend already sends the token; this is a handful of lines and closes the live-stdout leak. It does *not* fix H9.
9. **M13 + M24 — the two error-surface fixes.** Six store `try/finally`s + an `error` slice + a `detail`-array normalizer in `request()`, and one app-level `IntegrityError` handler. Between them these close roughly fifteen findings and make every future failure legible instead of a spinner or a 500.
10. **M4, M3, M17, M22** — four edge-tightenings on the submit path (`Literal["stop","continue"]` + fail-closed runner, `model_dump(mode="json")`, `role: UserRole`, `Query(ge=,le=)`). Each is one line and each converts a silent wrong outcome or a 500 into a 422.

**Tier 2 — needs a decision before code.**

11. **Decide, per unwired feature, implement-or-delete** (T5/M1/M7/M8/M11/M12 and H10's admin router). Every one of these currently has a docstring asserting it works, so leaving them undecided keeps costing reader-hours and generating bad "fixes." My recommendation: implement `${var}` (M1) and `credential_config` (M11) because steps are unusable without them; implement the admin router (H10) because it unblocks the ACL; delete or explicitly mark the artifact/storage pipeline (M7) and the templates helpers before adding more storage features.
12. **H8 + M9 + M10 + M16 together — the liveness and identity rework.** Mark nodes offline at startup, defer `resume_active_jobs` past `yield`, add a bounded placement retry and a grace-period node reaper, make the WS teardown identity-aware, and put an execution token in `ExecuteStepCommand`. These four are one design change, and fixing H8 without M16 introduces a *new* bug (an orphan's `step.completed` consumed as the re-dispatch's result).
13. **M19, M20, M21, M15, M14** and the L-tier cleanup, with L1's docstring corrections folded into whichever PR touches the adjacent code.

**Two process notes.** First, ten tests currently pin known-broken behavior and must flip with their fixes: `test_db_ops.py:678` (xfail), `test_runner_resume.py:859`, `test_storage_routes.py:529`, `test_ws_routes.py:1745` and `:1760`, `test_shell_steps.py:166`, `test_provisioner.py:619`, `stores/index.test.ts:236`, `client.test.ts:224`, `Admin.test.tsx:1434`. Budget for them. Second, the highest-leverage *test* investment is not more unit tests — it is a small number of cross-boundary contract tests: one asserting every URL/body `client.ts` builds resolves against the real OpenAPI schema, one asserting every `DashboardEvent` subclass has a producer, and one replaying a plan through submit validation and then through the runner's runtime context construction. Those three would have caught H2, M5, and M2 respectively, and they convert this report's whole "prose drift" category into failing tests.

---

# Appendix — Completeness Critic

*What the 11-subsystem audit itself missed: unassigned files (repo-root scripts, packaging, deploy), whole defect categories with no coverage, measured test-coverage hot spots, and 12 additional findings (A–L) found by inspecting the uncovered surface directly.*

## 1. COVERAGE GAPS — files no auditor was assigned

**Repo-root / packaging / deploy (zero coverage, inspected below):**
`dev.sh`, `add_node.sh`, `diagnose.sh`, `nexus_deploy.py`, `docker-compose.yml`, `Dockerfile.api`, `Dockerfile.frontend`, `pyproject.toml`, all five `packages/*/pyproject.toml`, `.env.template`, `.gitignore`, `TESTING.md`. **There is no CI at all** (`.github/`, `.gitlab-ci.yml` absent), so nothing runs the 1419 backend tests, vitest, playwright, or ruff automatically.

**Server modules in no finding:** `main.py` (lifespan/CORS/admin-seed), `config.py`, `db/session.py`, `db/migrations/__init__.py`, `api/routes/steps.py`, `services/storage/{base,minio_backend,nas_backend}.py`, `services/credentials/base.py`, `ws/__init__.py`.
**Steps in no finding:** `flow/jump.py`, `flow/sleep.py`, `package/install.py`, `system/health_check.py`, `git/pull.py`, `python/run.py`, `gem5/collect_results.py`, `file/__init__.py`.
**Frontend in no finding:** `App.tsx`, `main.tsx`, `components/Layout.tsx`, `pages/Login.tsx`, `pages/Dashboard.tsx`, `lib/utils.ts`, `vite.config.ts`, `vitest.config.ts`, `playwright.config.ts`, `e2e/`.
**Package:** `packages/cli/` entirely.

I read `steps.py`, `App.tsx`, `session.py`, `config.py`, `main.py`, `jump.py`, `health_check.py`, `Layout.tsx` (nav section), the vite/vitest/playwright configs, and every root script in full.

---

## 2. CATEGORY GAPS — whole defect classes with zero attention

**Dependency pinning / supply chain (nothing reported).** There is **no `uv.lock`, no `requirements*.txt`, and no `[project.optional-dependencies]` / `[dependency-groups]` anywhere** (verified: `git ls-files` has no lock, all five pyprojects use bare `>=`). This is not abstract: `packages/agent/pyproject.toml:9` says `websockets>=13.0`, the local `.venv` has **websockets 16.0**, and the *already-confirmed* agent crash ("run() catches the deprecated InvalidStatusCode; websockets 16 raises InvalidStatus") is exactly that floating range landing on a breaking major. `nexus_deploy.py`'s `INSTALL_SH:237-239` pip-installs the agent from scratch on every provisioned node, so **every new node gets whatever websockets is newest that day** — the bug is structurally guaranteed to recur after it is fixed. Same shape for `fastapi>=0.115` (a confirmed low finding already turns on FastAPI 0.135 returning 401 vs the documented 403).

**Test reproducibility.** `pytest`, `pytest-asyncio`, `pytest-cov`, and `asyncpg` are installed in `.venv` but declared in **no** manifest. `git clone && ./dev.sh` installs only `common`/`steps`/`server` (`dev.sh:171`) → `.venv/bin/python -m pytest` fails with `No module named pytest`. The 1419-test suite cannot be reproduced from the repo.

**Packaging correctness.** See findings A–D below. Both Dockerfiles are unbuildable/unreferenced; the server's declared dependency graph is wrong; the root project's `readme` points at a nonexistent file.

**Migration safety.** Nobody checked. `db/migrations/__init__.py` and `main.py:120-126` both document that `create_all` never ALTERs. I diffed `Base.metadata` against the live `nexus.db` — **currently clean** (the DB was recently recreated), so no live drift to report, but there is no guard: the recent `Job.log_text` / `has_results` additions would have produced `no such column: jobs.log_text` on any pre-existing file, and nothing in the boot path detects it.

**Observability in outages.** `dev.sh:406` (`stop_all`) does `rm -f .nexus-api.log .nexus-ui.log` — the *only* record of a crash is deleted by the command you run to recover, and `diagnose.sh:105` tells you to read that file. `./dev.sh reset` calls `stop_all` first, so the log of the failure you are resetting because of is destroyed. Combined with `lifespan`'s three `except Exception: logger.warning(...)` swallow points (`main.py:164`, `:197`) and `ws.py:353`'s "reconnect storm rather than an obvious crash" note, a bad boot leaves nothing to read.

**Secrets in the deploy path.** `nexus_deploy.py:508` prints `API_KEY` to stdout; `add_node.sh` is documented as CI-capturable (`nexus_deploy.py:54-55`). `.env.template:5,14,18` ships three literal `changeme_*` secrets and a `JWT_SECRET` placeholder that `Settings.from_env` will happily accept (only *absence* is rejected, `config.py:190`) — so the documented setup path yields a working server with a publicly-known signing secret.

**Loop-primitive design (pinned as intended, so not a bug — noting for completeness).** `flow/jump.py` + `runner.py:441-464`: a `jump(on="always", max_jumps=N)` loop *always* ends with the job marked `failed` ("Max jumps exceeded") unless the user also sets `on_fail: "continue"` **on the jump step itself**, which is documented nowhere. `tests/integration/test_runner_scheduler.py:423-452` pins `status == "failed"` as correct, so this is intentional — but it means there is no way to express a terminating loop that reports COMPLETED.

---

## 3. UNDER-TESTED HOT SPOTS (measured, not guessed)

Ran the suite with coverage (`1419 passed, 1 xfailed`, 93% total):

| Module | Cover | Uncovered risk |
|---|---|---|
| `packages/agent/src/nexus_agent/main.py` | **0%** | The agent's entire CLI. `nexus-agent init` is what `nexus_deploy.py:243` and the provisioner invoke on every node; `parse_args`' own docstring (`main.py:52-57`) admits the documented bare `nexus-agent --server ...` form is broken. Zero tests. |
| `nexus_server/main.py` | **51%** (116-206 missing) | The whole `lifespan`: `init_db`, `create_all`, admin seeding, `init_backends`, and the `resume_active_jobs` wiring — i.e. the exact code path of the confirmed high finding "resume_active_jobs() always fails a job resumed at a remote step". Tests substitute a no-op lifespan (`conftest.py:400`). |
| `db/session.py` | **62%** (63-83 missing) | `init_db` itself never executes in tests; tests build their own engine. Production engine/pool config is untested. |
| `agent/capability.py` | **56%** (118-132, 190-208, 237-254) | Includes the author-flagged `/etc/os-release` path. |
| `agent/connection.py` | 85% (**238-280**) | Exactly the register/api-key-logging + reconnect region carrying two confirmed high findings. |
| `api/routes/credentials.py` | 76% (**60-79**) | The update/delete routes carrying the "any user can overwrite/delete any credential" finding. |
| `gem5/run_simulation.py` | 73% | 220-241, 331-333, 348-349. |

---

## 4. NEW FINDINGS — the highest-value missing check, done

### A. `Dockerfile.frontend` cannot build — wrong COPY paths (blocker)
`Dockerfile.frontend:10` `COPY frontend/src/package.json frontend/src/package-lock.json* ./` and `:15` `COPY frontend/src/ ./`.
`frontend/src/package.json` **does not exist** (verified: only `frontend/package.json`). Failure: `docker build -f Dockerfile.frontend .` → `ERROR: "/frontend/src/package.json": not found` at step 3. Even with the manifest path fixed, `:15` copies only `src/`, so `index.html`, `vite.config.ts` and `tsconfig.json` (all at `frontend/`) are absent and `npm run build` (`tsc -b && vite build`) fails. The frontend image has never been built.

### B. `docker-compose.yml` has no `api`/`frontend` service, but `Dockerfile.frontend` proxies to one
`docker-compose.yml` defines only `redis` and `minio` (verified). `Dockerfile.frontend:42` writes `proxy_pass http://api:8000;` — a hostname that exists in no compose network. Failure: even if A were fixed, the built image returns `502 Bad Gateway` on every `/api/*` request because `api` never resolves. Compounding: `dev.sh:110-113`'s AI Note asserts "docker-compose.yml **also defines API and frontend services**, which dev mode deliberately does NOT use" — that statement is false, so the rationale for `docker compose up redis minio` is documented against a file that doesn't exist in that form.

### C. `.env.template` — the file `dev.sh` tells you to copy — configures a Postgres that isn't deployable
`dev.sh:67`: *".env file not found. Copy .env.template to .env and fill in values."*
`.env.template:17` sets `DATABASE_URL=postgresql+asyncpg://nexus:changeme_postgres@postgres:5432/nexus`, plus `POSTGRES_*` at `:5-7`. But: there is no `postgres` service in `docker-compose.yml`, and `asyncpg` is declared in **no** pyproject (`packages/server/pyproject.toml` ships `aiosqlite` only; it is present in this `.venv` by hand).
Failure: fresh clone → `cp .env.template .env` → `./dev.sh`. `load_env` (`dev.sh:75`) uses `${DATABASE_URL:-sqlite...}`, and the template value is non-empty, so the SQLite default **does not apply**. `lifespan` → `init_db` builds an asyncpg engine → `create_all` fails resolving host `postgres` (or `ModuleNotFoundError: asyncpg` on a clean install). The server never boots, and `dev.sh:334` has already redirected the traceback into `.nexus-api.log` while printing "Nexus is running!". `.env.template` also omits `NEXUS_ADMIN_PASSWORD`, which `diagnose.sh:67` claims it matches.

### D. `nexus-server` does not declare `nexus-steps`, but hard-imports it
`packages/server/src/nexus_server/main.py:65`: `import nexus_steps  # noqa: F401`.
`packages/server/pyproject.toml:6-21` lists `nexus-common` and never `nexus-steps` (verified).
Failure: `pip install ./packages/common ./packages/server` → succeeds → `python -c "import nexus_server.main"` → `ModuleNotFoundError: No module named 'nexus_steps'`. `dev.sh:171` and `Dockerfile.api:20-23` both happen to install all three explicitly, which is the only reason this is latent. Any consumer trusting the metadata (a wheel build, a downstream image, `uv sync --package nexus-server`) gets a server that cannot import.

### E. Root `pyproject.toml` references a `README.md` that does not exist
`pyproject.toml:6` `readme = "README.md"`; `ls README.md` → no such file (verified).
Failure: `pip install .` / any PEP 621 metadata build of the workspace root errors on the missing readme. Also means the repo has no README at all despite four scripts referring readers to setup docs.

### F. `nexus_deploy.py` leaves an orphan node with a live api_key when SFTP fails — contradicting its own documented invariant
`nexus_deploy.py:334-336`: *"If the install itself fails after registration, `_cleanup()` deregisters the node explicitly."*
`_cleanup()` is called **only** on `rc != 0` from the installer (`:532`). Registration happens at `:504`; `client.open_sftp()` at `:513` and `sftp.put(...)` at `:516` are *after* it and outside any `except`.
Failure: target host has the SFTP subsystem disabled (or `/tmp` full/read-only) → `open_sftp()` raises `SSHException: Channel closed` → propagates through `finally: client.close()` (`:539`) as an unhandled traceback → the node row minted at `:504`, **with its api_key**, is never deleted. The operator sees a Python traceback and a permanently-offline phantom node they must delete by hand. Same for a `paramiko.SFTPError` inside `put`.

### G. `nexus_deploy.py` leaks a local temp file on every invocation
`nexus_deploy.py:514` `tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)` — `local_sh` is never `unlink`ed anywhere in the file (read in full). Every `./add_node.sh` run leaves a `/tmp/tmp*.sh` behind. Minor, but note `:516` also writes the fixed remote path `/tmp/nexus-install.sh` with no `O_EXCL` — the same symlink-preplacement issue already confirmed in `provisioner.py:520`, present here too and unreported for this file.

### H. `dev.sh` leaks an immortal `chflags` watchdog whose PID file gets clobbered
`start_pth_watchdog` (`dev.sh:200-209`) writes `$!` to `.nexus-pthwatch.pid` **without stopping an existing watchdog**, and `start_api` calls it at `:229`. `start_all` guards itself (`:308-309`) but `./dev.sh api` does not.
Failure: `./dev.sh` (background stack, watchdog PID 111 recorded) then `./dev.sh api` → PID file overwritten with 222. PID 111 is now unreferenced; `./dev.sh stop` (`stop_pth_watchdog`, `:211-216`) kills only 222. PID 111 runs `chflags` + `sleep 1` forever, surviving every subsequent stop/reset, and `show_status:475` reports ".pth watch: running" for the wrong process. Repeated across a day this accumulates one busy-loop per invocation.

### I. `dev.sh` dies with `JWT_SECRET: unbound variable` after already tearing the stack down
`dev.sh:28` sets `-u`; `:329` interpolates `JWT_SECRET='$JWT_SECRET'` in the *parent* shell. `load_env` (`:65-80`) defaults `DATABASE_URL`/`REDIS_URL`/`MINIO_*` but never validates `JWT_SECRET`, despite `:52-53` claiming ".env is missing … JWT_SECRET and the credential encryption key have no safe defaults."
Failure: an `.env` without `JWT_SECRET` (e.g. hand-written, or the key commented out) → `./dev.sh` runs `check_deps`, `docker compose down` (`:118`), `install_python`, `install_frontend`, `kill_port 8000`, `kill_port 3000`, then aborts at `:329` with `dev.sh: line 329: JWT_SECRET: unbound variable`. The user is left with everything killed, nothing started, and an error naming a shell variable rather than the missing config key. `./dev.sh api` (which never touches `$JWT_SECRET` directly) gets *past* this point and fails later inside Python instead — inconsistent diagnosis for the same misconfiguration.

### J. `diagnose.sh` prescribes a Postgres remedy for a SQLite dev stack
`diagnose.sh:92`: `Tip: Delete the postgres volume to reset: docker compose down -v`. Dev is SQLite (`diagnose.sh:40` says so eleven lines earlier), there is no postgres service, and `down -v` deletes the **MinIO** volume instead. Failure: an operator hitting the stale-admin-password branch runs the suggested command, loses all stored artifacts, and the password is still wrong (the fix is `./dev.sh reset`, which deletes `nexus.db`).

### K. `Layout.tsx:49-51` documents an authorization guarantee that does not exist
*"Authorization is enforced server-side on the admin endpoints, so a non-admin who clicks through sees errors rather than a blocked route."* Two confirmed findings say otherwise: `/api/admin/*` returns 404 (no router exists) and `/api/credentials` enforces authentication only, so a non-admin who clicks through **succeeds** at reading and deleting every credential in the cluster. The comment is what would stop a reviewer from adding the missing role gate.

### L. Committed build artifact / graph unavailable on clone
`frontend/tsconfig.tsbuildinfo` is tracked in git (appears in `git status` as modified) — a `tsc -b` cache that will conflict on every branch. Separately, `.gitignore:43` ignores `graphify-out/`, so a fresh clone cannot follow `CLAUDE.md`'s mandatory "run `graphify query` first" workflow until someone rebuilds the graph.