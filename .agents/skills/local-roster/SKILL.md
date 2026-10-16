---
name: local-roster
description: Coordinate user login in the AI client's built-in browser, then use that same session for Genshin/ZZZ collection and private local saving when the client provides supported data access. HSR is paused.
---

Read [AGENTS.md](../../../AGENTS.md), [workflow](../../../docs/workflow.md) and [client bridge](../../../docs/client-bridge.md).

Use the current AI client's built-in browser on every OS; Codex uses its in-app browser. Bootstrap and select it through the host's documented browser tools, then retain the actual browser object. Never pass only its ID, launch a separate browser, export cookies or read authentication databases.

## Start the connected task

The installed Python package must match the selected source. Do not substitute a stale installed package. Missing client connection gives CLIENT_BROWSER_REQUIRED. Each game checks its own required sources before identity reads or business writes; a failed game's capability check does not skip the next game.

For sync/discover, import src/hoyo_roster/client_runtime.mjs in the client's JavaScript runtime and call createRosterHost(browser). This production composition root connects the bundled game readers; do not substitute the low-level createClientHost(browser) without adapters. Do not ask the person or the executing agent to implement readSource. The readers discover the selected tab's advertised capabilities at runtime. They use the documented CDP capability only when it is actually available; they do not obtain CDP through another process, debugging address or backend. An unavailable capability gives per-game CLIENT_SOURCE_UNAVAILABLE and missing sources. A configured implementation is different from a source that the current client can actually read. Never insert fixtures, export authentication material, use independent authenticated HTTP, or invent response origin/coverage evidence. See the dated live evidence in the validation document.

For standalone login, import src/hoyo_roster/client_host.mjs and createClientHost(browser); login has no data-reader dependency. Use host.startTask with the selected Python 3.12 environment, repository cwd and requested game/operation; default sync handles Genshin then ZZZ. storageRoot and runtimeRoot are optional documented overrides. The host starts a private worker pipe; do not run --client-stdio in a visible terminal.

## Login module

Explicitly await task.pump({maxRequests: 1}) inside each active client tool call. Pipe callbacks only queue requests; they must never call browser tools in the background. Each bundled source step has a 45-second total deadline, including browser preparation; cancellation prevents later browser actions. Pump returns when human confirmation is needed. For each new host.pendingQuestion().attempt, proactively ask whether the person finished login in the official page, using the available user-input tool. Retain that attempt value with the question; suppress repeated prompts for the same waiting attempt. Choices: 已完成登录 / 尚未完成 / 取消. The user handles phone, OTP, QR confirmation, agreements, image verification and submission in the page; do not solicit or enter credentials, send SMS or check agreements.

Keep host/task bindings and the same browser/page alive while waiting. Call host.answerLogin("completed", question.attempt) only after explicit confirmation for that attempt. If the person already said “已登录” for this attempt, use that answer without asking again. A new login attempt after explicit expiry requires a fresh confirmation; never reuse an earlier attempt's answer. Use host.answerLogin("waiting", question.attempt) for an explicit not-yet response and wait for the person; do not repeatedly re-prompt. Use host.answerLogin("cancel", question.attempt) or task.cancel() on cancellation. Elapsed time is never a confirmation.

The login module returns at user confirmation, with the same session/page; no identity probes, roster reads or business writes occur inside it. It does not close the shared browser.

## Continue the game task

Keep the connected worker alive after answering and continue task.pump({maxRequests: 1}) until task.status().done; each game owns its official identity check and both source passes, normalization, validation and saving. ZZZ establishes its cultivate service session inside the shared browser and refreshes that service once on expiry before requesting human reauthentication. Its initialization returns only a verified receipt; no initialization response body or authentication headers cross the pipe. Games reuse the session and restart only the current uncommitted game's two passes once on account-level auth recovery. Don't stop at user confirmation when collection was requested.

Inspect task.status() for fixed progress, pending questions and final errors. task.completion settles after the worker closes; success requires the exit code and final protocol result to match. Do not await completion alone while requests still need pumping. On missing tools, failed transport, cancellation or page closure, report the explicit outcome and last completed stage. Pump checks the pending login page in the active tool call; a closed page ends the attempt. task.cancel() also interrupts hung SDK/source waits and reaps the worker; wait for completion before starting another host on the same browser. Source adapters receive an AbortSignal and must stop browser actions on abort. Source or capability failure is not successful collection.

HSR is paused. Genshin excludes artifacts and full stat panels; ZZZ retains its full coverage requirements. Report actual supported coverage and saved result paths. For offline inspection/recovery read [local use](../../../docs/local-use.md). Do not explore or migrate existing private directories, and do not generalize browser retention to client restarts or untested OS/client combinations.
