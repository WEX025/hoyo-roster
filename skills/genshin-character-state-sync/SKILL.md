---
name: genshin-character-state-sync
description: Collect the user's own Genshin Impact character state into their isolated local workspace with exact official login Binding.
---

# Genshin Impact local character state

Read `../../README.md`, `../../AGENTS.md` and `../../docs/local-use.md` before collecting.
Use this checkout's installed Python 3.12 environment:

```bash
.venv/bin/python tools/run_local_hoyo.py --user me init genshin
.venv/bin/python tools/run_local_hoyo.py --user me login genshin
.venv/bin/python tools/run_local_hoyo.py --user me sync genshin
```

The external `users.toml` contains this user's selected UID. It must be current-user-owned `0600`.
Use a per-user external browser Profile; first login is headed and manually completed by the user.
UID does not authorize access by itself: require exact official Binding, game and supported region.
Do not accept or export Cookie/token/password/header values, raw responses or login artifacts.
Stop on identity mismatch, missing permissions, captcha, rate limit, risk control or response drift.
Never borrow another user's data or login state or run an automatic third Pass.

The collector performs exactly two independent Business Passes, compares normalized business and
source coverage, enforces lifecycle constraints, validates its candidate and fully validates atomic
installation. The current source produces `partial`; never upgrade that claim beyond Schema/source
coverage. Source mappings and guarded Endpoint contracts are in the fixed package under `src/`.
Preserve unavailable fields instead of fabricating values; preserve roster/Build bijection.

Results stay in the external user/game/UID workspace. Normal local collection does not fetch,
commit or push; `publish-main` is retained compatibility code, not this repository's user workflow.
An explicit request to save the user's own formal data into their own Git repository is a separate
operation whose owner, visibility, scope and base must be verified. Never upload config/login state.

## Known first-use limitation

Genshin empty-baseline first login currently returns unavailable expected identity before adopting
the configured UID. Report this limitation; do not seed unverified identity fields or claim first-use
live acceptance. The implementation is in `src/genshin_character_state_sync/login_provider.py`.
