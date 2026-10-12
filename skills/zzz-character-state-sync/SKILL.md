---
name: zzz-character-state-sync
description: Collect the user's own Zenless Zone Zero character state into their isolated local workspace with exact official login Binding.
---

# Zenless Zone Zero local character state

Read `../../README.md`, `../../AGENTS.md` and `../../docs/local-use.md` before collecting.
Use this checkout's installed Python 3.12 environment:

```bash
.venv/bin/python tools/run_local_hoyo.py --game zzz --uid YOUR_UID --operation init
.venv/bin/python tools/run_local_hoyo.py --game zzz --uid YOUR_UID --operation login
.venv/bin/python tools/run_local_hoyo.py --game zzz --uid YOUR_UID
```

Direct mode requires only `--game` and the user's own `--uid`; it does not read users.toml.
Advanced external configuration remains optional and must be current-user-owned `0600`.
Use a per-game/UID external browser Profile; first login is headed and manually completed by the user.
UID does not authorize access by itself: require exact official Binding, game and supported region.
Do not accept or export Cookie/token/password/header values, raw responses or login artifacts.
Stop on identity mismatch, missing permissions, captcha, rate limit, risk control or response drift.
Never borrow another user's data or login state or run an automatic third Pass.

The collector performs exactly two independent Business Passes, compares normalized business and
source coverage, enforces lifecycle constraints, validates its candidate and fully validates atomic
installation. The current source produces `complete`; never upgrade that claim beyond Schema/source
coverage. Source mappings and guarded Endpoint contracts are in the fixed package under `src/`.
Preserve unavailable fields instead of fabricating values; preserve roster/Build bijection.

Results stay in the external user/game/UID workspace. Normal local collection does not fetch,
commit or push; `publish-main` is retained compatibility code, not this repository's user workflow.
An explicit request to save the user's own formal data into their own Git repository is a separate
operation whose owner, visibility, scope and base must be verified. Never upload config/login state.
