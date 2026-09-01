# Background translation and same-chat monitoring

Read this reference when translation should continue after the initiating assistant turn ends. The background process performs subtitle work; current-chat heartbeats only observe durable state and decide whether to continue, report, sync, or stop monitoring.

## Process topology

A normal `--stage-only` run has one long-lived Python runner. It processes requested videos sequentially and has at most one active model child at a time:

- Iris runs as a `codex exec` child while translating a chunk.
- Atlas runs as a `codex exec` child only after an Iris chunk has validated and terminology candidates need curation.
- A future episode in the same command is queued work, not a process that should already exist.

Do not require Iris and Atlas to run simultaneously. “All processes are healthy” means the Python runner is alive and, whenever the episode log says a model call is active, the matching current `codex exec` descendant is also alive.

## Prepare and launch

1. Finish `--source-only` for every requested video and require `SOURCE READY` before starting model work. Do not background a copy from SMB or removable media together with translation.
2. Resolve the exact Skill, workspace, inputs, and Codex executable before launch. Reject a duplicate live runner targeting the same episode set.
3. Start `main.py translate --stage-only ...` with a persistent local execution mechanism that provides a process or session handle and preserves output after the initiating turn ends. Do not rely on an untracked shell `&` alone.
4. Record the start time, exact command, runner PID or session handle, workspace, requested videos, episode progress paths, and episode log paths. These concrete values belong in both heartbeat prompts.

If the process finishes while it is being checked, evaluate its exit status and durable outputs instead of calling the launch healthy merely because it started.

## Mandatory launch health gate

Do not end the initiating turn until every applicable check below passes:

1. **Runner:** Observe the Python runner alive on two checks separated by a short interval. Its command must identify this Skill, the intended workspace, and the intended inputs.
2. **Durable state:** The current episode's progress JSON exists, parses, has a fresh `updated_at`, contains no `last_error`, and has advanced beyond an uncorroborated `starting` state.
3. **Episode log:** The compact log contains `VIDEO START`. For the first uncached model request, it must also contain `CODEX START` with the expected role, model, and request ID.
4. **Active child:** If the latest unmatched log event is `CODEX START`, inspect the runner's descendants and confirm the corresponding `codex exec` process is alive. A logged start without a live child or a later durable chunk update does not pass.
5. **Fast/cache path:** If no child remains because cached work advanced immediately, require an observable chunk advance or a valid terminal result. Do not wait for a child that the workflow correctly did not need.
6. **No immediate failure:** Recheck the process/session output and progress after the preceding observations. Any nonzero exit, `FAILED`, `status="failed"`, stale state, or missing expected child fails the gate.
7. **Monitoring:** Create and verify both current-chat heartbeat schedules described below. A healthy runner without an active follow-up path is not a completed background handoff.

If the gate fails, keep the initiating turn open and diagnose it. Do not create orphan heartbeat schedules for a launch that never became healthy. If all requested work reaches a valid terminal state before the gate completes, report the result directly and do not create heartbeats.

## Staged current-chat heartbeat schedule

Use the product's current-chat scheduled-task facility, not a standalone task:

- **Startup phase:** run every 10 minutes during the first hour, then end.
- **Steady phase:** start after the first hour and run every 60 minutes.

Create both schedules only after the process health checks pass. Give them unique names derived from the run, and capture both automation IDs. Update both saved prompts with the exact run start time, the two IDs, process/session handle, workspace, input list, progress paths, and log paths. Verify that both schedules are active before ending the initiating turn.

If the current surface cannot create a scheduled task that returns to this chat, say so and keep the turn attached or ask the user how to proceed. Do not substitute `codex exec resume`, another App Server, session-file mutation, or an undocumented desktop pipe.

## Heartbeat run contract

On every heartbeat:

1. Read only the compact `REPORT.md` and relevant `progress/<episode>.json` files first. Read the compact episode log only when state is ambiguous; use full Codex JSONL only for debugging.
2. Check the recorded runner/session and exact command. Treat a missing runner plus nonterminal progress as a stalled run, even if the last progress status says `translating`.
3. Report compactly: runner state, current episode, completed/total chunks, next chunk, glossary state, last error, and whether output is staged or synced.
4. If the run is healthy and nonterminal, leave it alone. Do not launch a duplicate runner or model call.
5. If all requested outputs are staged and the original destinations are available and already authorized, run `--sync-only`. Otherwise report that local outputs are ready to sync.
6. On success, failure, pause, stale process, pending user decision, or another terminal state, disable the steady heartbeat and any startup heartbeat that remains active. Never leave an hourly monitor running after it has nothing to monitor.

Do not automatically retry failed model work merely because a heartbeat noticed it. Preserve caches and report the exact resumable state unless the user's request already authorizes continued recovery.

## Initial handoff report

After the launch gate passes, the initiating turn may end. Its final report must include:

- the runner PID or persistent session handle and exact scope;
- the health evidence used for the runner and current child or fast/cache path;
- the current progress status and durable state paths;
- confirmation of the first-hour and steady heartbeat schedules, including their next expected checks;
- the condition that will stop monitoring.
