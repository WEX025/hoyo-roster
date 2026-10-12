# hoyo-roster agent instructions

## Supported entrypoint

Read `README.md` and `docs/local-use.md`. Use `tools/run_local_hoyo.py` with this checkout's
Python 3.12 environment for independent local collection. The supported flow does not use
`tools/run_hoyo_sync.py`, Broker publication, or `publish-main`; those are legacy compatibility
paths and are not generalized publishers for a new repository.

A user request to collect their own game state authorizes only the named game and identity.
Use the UID supplied by that user; never infer it from someone else's data. Direct mode
automatically selects an isolated scope; in advanced mode only update an explicitly selected
external alias without overwriting other identities. Preserve existing user configuration and data when installing or upgrading code.
If game/identity/alias selection is ambiguous, ask for the missing information.

First login requires the user's manual interaction with a headed official browser. Do not automate
credentials, account selection or verification codes. Do not accept, print, copy, export or commit
Cookie, token, passwords, device headers, browser state, HAR, traces, screenshots or raw responses.
Honor exact Binding, configured UID, region, authentication, privacy, rate-limit and risk failures.
Do not retry or bypass risk control or borrow another browser's login state.

## Direct user request routing

For a request naming a game and the user's own UID, run the documented direct command:
`.venv/bin/python tools/run_local_hoyo.py --game <genshin|zzz|hsr> --uid <user-supplied UID>`.
No users.toml is required. Do not print the UID. Install the four local packages if needed, preserve
external data on upgrades, and let the user complete the official headed login when required.
Use `--operation login` only for a requested login check; default direct operation is sync.
`--config/--user` plus the old positional syntax is advanced mode and conflicts with direct flags.

Genshin empty-baseline identity is established only as an in-memory selector from the explicit UID;
exact official Binding still proves game, UID and supported region before PASS. Never seed unverified
Profile fields. Keep existing formal identity mismatch rejection. Offline first-use tests do not
constitute real-account live acceptance; report that limitation accurately.

## Data and storage rules

- Local configuration, browser state, candidates and the current personal workspace stay outside
  the source checkout. User/game/UID output is isolated. The scope lock serializes browser use. Browser Profiles are also isolated per game/UID.
- In each scoped workspace only the selected game has personal data; other games have empty baselines.
- Reuse the existing game two-pass comparison, lifecycle, candidate validation and complete installation
  validation. An error must retain the previous current data. Never weaken identity or Schema checks.
- Every roster identity is a map key; every Build is `builds/<positive-character-id>.json`. Preserve
  exact roster/Build bijection and stable string IDs, decimal percentages and canonical JSON.
- `sync.state` controls completeness. Genshin is partial, ZZZ is complete, HSR is partial and may
  preserve existing character data when the source cannot prove roster coverage. Do not invent fields.
- Store current equipped weapons/equipment only; no inventory, resources, currencies, recommendations,
  target builds, analysis, public-mechanics datasets or field-level coverage metadata.
- Install atomically after full validation. Do not delete ambiguous interrupted residue.

## Git and sharing boundary

The committed `data/` is an intentionally empty baseline. Normal local collection never changes
this checkout, commits or pushes. Saving a user's personal formal results to their own Git repository
is a separate explicitly authorized action: verify destination owner, visibility, selected game and
expected base; include only the selected formal JSON paths, never config/login/private artifacts.
This release has no automatic personal-data GitHub publication command. Do not assume a local refresh
request authorizes upload to a public repository.

Never include another person's role pool or prior Git history when sharing source. Keep user data
private unless that owner explicitly authorizes its destination and visibility. Do not choose a
license, change visibility, add collaborators, merge or deploy unless explicitly requested.

## Offline verification

Run `python tools/validate_repository.py --root . --require-empty-baseline` for this untouched
source baseline, and normal full Validator for a populated personal workspace. Use synthetic
identities/responses for development. Do not perform real login or collection just to test a release.
See `dependency-authority.toml` for pinned Python, runtime and development dependency contracts.
