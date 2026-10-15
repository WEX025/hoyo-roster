---
name: local-roster
description: Coordinate user login in the AI client's built-in browser, then use that same session for Genshin/ZZZ collection and private local saving when the client provides supported data access. HSR is paused.
---

Read [AGENTS.md](../../../AGENTS.md), [workflow](../../../docs/workflow.md) and [client bridge](../../../docs/client-bridge.md).

Use the current AI client's built-in browser on every OS; Codex uses its in-app browser. Bootstrap and select it through the host's documented browser tools, then retain the actual browser object. Never pass only its ID, launch a separate browser, export cookies or read authentication databases.

## Start the connected task

Import src/hoyo_roster/client_host.mjs in the client's JavaScript runtime and createClientHost(browser). Use host.startTask with the selected Python 3.12 environment, repository cwd and the requested game/operation; default sync handles Genshin then ZZZ. storageRoot and runtimeRoot are optional documented overrides. The host starts a private worker pipe; do not run --client-stdio in a visible terminal.

The installed Python package must match the selected source. Do not substitute a stale installed package. Missing client connection gives CLIENT_BROWSER_REQUIRED; missing supported source access gives CLIENT_SOURCE_UNAVAILABLE before prompting for login or creating business results.

readSource may be supplied only when the client really exposes a supported source interface. Current Codex's game pages expose no WebMCP data tools and its documented browser API does not provide the complete source responses. Leave readSource absent and report this concrete blocker; never use test fixtures or direct HTTP as a real source.

## Login module

Explicitly await task.pump() inside each active client tool call. Pipe callbacks only queue requests; they must never call browser tools in the background. Pump returns when human confirmation is needed. For each new host.pendingQuestion().attempt, proactively ask whether the person finished login in the official page, using the available user-input tool. Retain that attempt value with the question; suppress repeated prompts for the same waiting attempt. Choices: 已完成登录 / 尚未完成 / 取消. The user handles phone, OTP, QR confirmation, agreements, image verification and submission in the page; do not solicit or enter credentials, send SMS or check agreements.

Keep host/task bindings and the same browser/page alive while waiting. Call host.answerLogin("completed", question.attempt) only after explicit confirmation for that attempt. If the person already said “已登录” for this attempt, use that answer without asking again. A new login attempt after explicit expiry requires a fresh confirmation; never reuse an earlier attempt's answer. Use host.answerLogin("waiting", question.attempt) for an explicit not-yet response and wait for the person; do not repeatedly re-prompt. Use host.answerLogin("cancel", question.attempt) or task.cancel() on cancellation. Elapsed time is never a confirmation.

The login module returns at user confirmation, with the same session/page; no identity probes, roster reads or business writes occur inside it. It does not close the shared browser.

## Continue the game task

Keep the connected worker alive after answering and continue task.pump() until task.status().done; each game owns its official identity check and both source passes, normalization, validation and saving. Games reuse the session, reauthenticate only on explicit expiry, and restart only the current uncommitted game's two passes once. Don't stop at user confirmation when collection was requested.

Inspect task.status() for fixed progress, pending questions and final errors. task.completion settles after the worker closes; success requires the exit code and final protocol result to match. Do not await completion alone while requests still need pumping. On missing tools, failed transport, cancellation or page closure, report the explicit outcome and last completed stage. Pump checks the pending login page in the active tool call; a closed page ends the attempt. task.cancel() also interrupts hung SDK/source waits and reaps the worker; wait for completion before starting another host on the same browser. Source adapters receive an AbortSignal and must stop browser actions on abort. Source or capability failure is not successful collection.

HSR is paused. Genshin excludes artifacts and full stat panels; ZZZ retains its full coverage requirements. Report actual supported coverage and saved result paths. For offline inspection/recovery read [local use](../../../docs/local-use.md). Do not explore or migrate existing private directories, and do not generalize browser retention to client restarts or untested OS/client combinations.
