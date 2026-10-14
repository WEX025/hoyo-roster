---
name: local-roster
description: Use when collecting the user's own Genshin or ZZZ data with this installed project. Log in first and discover owned UIDs automatically on every OS. HSR is temporarily unavailable; do not use it. Do not use for other people's accounts or public publication.
---

Read the repository's AGENTS.md and docs/local-use.md as the authoritative workflow.
Start with `hoyo-roster` (both supported games), or `hoyo-roster --game <genshin|zzz>` if the user selected one game. Use `python -m hoyo_roster` if the script is not on PATH.
Do not ask the user to supply a UID before login. On Windows, macOS and Linux, the same public entrypoint obtains the user's allowed manual login, discovers owned roles from official Binding, then verifies each exact game/UID/region before collection. Reuse an existing valid session and saved selection when available. Browser choice does not change this workflow.
Use `--uid` only when the user already supplied an explicit identity or for an explicitly scoped offline maintenance operation; it is not a prerequisite for collecting the user's data.
Honkai: Star Rail is temporarily unavailable; please do not use it. The retained `hsr` interface is not a supported collection workflow. Genshin currently excludes artifacts and full stat panels; its character roster must still satisfy closure checks. See AGENTS.md and docs/data-format.md for the authoritative scope.
Missing tools in an agent host do not imply their presence in a normal terminal. Report installation or user-interaction blockers accurately.

For acceptance testing, freeze the checkout and use the chosen environment's Python to run `tools/verify_installation.py` without `PYTHONPATH=src`. Require a clean source SHA and matching loaded package before treating a console run as evidence for that code. Reinstall that version when it differs; `--version` alone is insufficient. Record the interpreter and check result privately.

Use code for official navigation, request observation, identity checks, collection and persistence. Agent browser tools only locate the current CLI-owned official window and deliver the user handoff. If the known window cannot be selected or one control call fails/times out, report the exact pending stage and stop agent UI attempts for this run. Do not cycle app switching, scan unrelated windows, guess coordinates, or log into another browser as a substitute.

On `LOGIN_PANEL_MANUAL` / `PHONE_AUTOMATION_MANUAL`, leave the same CLI/window running and hand control to the person; do not add agent clicks. On `WAITING_FOR_MANUAL_CAPTCHA`, name the corresponding official window and wait for the person. On `WAITING_FOR_TERMS`, the person reviews/checks the official terms. OTPs and QR confirmation remain in the official UI. Preserve earlier authorization, but an uncertain SMS request is never permission to repeat it.

Treat `SMS_SUBMITTING`, `SMS_REQUESTING`, strict `SMS_ACCEPTED` and authenticated Binding as separate evidence. Unknown/rejected receipts stop the run; never retry Send or use an old code. For no roles, report `NO_ROLES` / `NOT_COLLECTED`, not completed collection. Use fixed terminal states and the user's own action as evidence; do not inspect form values, cookies or screenshots.

For cancellation/timeouts, first wait for the command's close/commit outcome. If close is failed or uncertain, preserve the scope and report that blocker; do not launch another session or remove locks. A retained discovery with `DISCOVERY_RESUME=AVAILABLE` can be explicitly selected with `--game <game> --resume-discovery <DISCOVERY_RUN_ID>` and the same auth root; recheck it with `--headless` first. This does not resolve an existing-profile conflict or renew SMS permission. `UNPROVEN` cannot be resumed; preserve it. See docs/local-use.md for the exact boundaries.

For storage, use `COMMIT_STATE` and `CLEANUP_PENDING`: preserve evidence on `DURABILITY_UNKNOWN`; inspect before selecting a verified recovery; `COMMITTED` with cleanup pending means saved with remaining work. Use only the exact owned identity scope and documented maintenance entrypoints. Report real E2E as incomplete until identity, both collection passes, validation, save and actual close are all confirmed.
