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
