---
name: hoyo-browser-session
description: Shared local collector infrastructure, strict user configuration and isolated snapshot storage for hoyo-roster.
---

# Shared local infrastructure

Read `../../AGENTS.md` and `../../docs/local-use.md`. The supported independent user entrypoint is
`tools/run_local_hoyo.py`. `user_workspace.SnapshotStore` separates the existing collectors from
installation, and `LocalSnapshotStore` keeps config/login/personal data out of the source checkout.
Each user/game/UID has one current validated generation. Serialize each user's browser access;
validate before atomically switching the current pointer and preserve data/config on source upgrades.

Do not use the legacy fixed Browser Owner/Broker `publish-main` path for this new repository.
The compatibility implementation remains protected by its fixed repository and Runtime identity;
do not weaken those checks or treat it as a generalized publisher.

Keep browser Profile, databases, HMAC keys, temporary results and authentication artifacts private.
Never print or export UID, cookies, tokens, device-header values, raw responses or browser artifacts.
Use exact official login Binding; no account guessing, credential automation or risk-control bypass.
Offline validation and synthetic fixtures do not constitute real-account login/collection acceptance.
