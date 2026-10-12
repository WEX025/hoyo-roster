# hoyo-roster

Local collectors for your own Genshin Impact, Zenless Zone Zero, and Honkai: Star Rail character state.
Repository: https://github.com/WEX025/hoyo-roster

Start with [installation and local use](docs/local-use.md). The code and schemas are shareable;
your configuration, browser login state, and character pool stay in your own external workspace.
The committed `data/` is an empty `not_synced` baseline, not an example person's account.

## Quick start

Use Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/pip install ./skills/hoyo-browser-session ./skills/genshin-character-state-sync ./skills/zzz-character-state-sync ./skills/hsr-character-state-sync
.venv/bin/python -m playwright install chromium
mkdir -p ~/.config/hoyo-game-data
cp examples/users.example.toml ~/.config/hoyo-game-data/users.toml
chmod 600 ~/.config/hoyo-game-data/users.toml
```

Edit the external configuration with your own UID, remove games you do not use, then run:

```bash
.venv/bin/python tools/run_local_hoyo.py --user me init zzz
.venv/bin/python tools/run_local_hoyo.py --user me login zzz
.venv/bin/python tools/run_local_hoyo.py --user me sync zzz
```

Replace `zzz` with `hsr` where needed. UID selects an identity; the collectors still require
your own exact official account Binding and login. First login requires a headed local browser
and your manual completion of the official login page. Never provide cookies, tokens or passwords
to the assistant. Only the currently supported mainland China sources are implemented.

## Current limits

- Genshin's fresh empty-baseline login has a known bootstrap issue: the login Provider currently
  expects an existing formal identity instead of enrolling the configured UID. A new-user Genshin
  `login`/`sync` can fail before Binding; it is not claimed to be ready for first use. See
  `skills/genshin-character-state-sync/src/genshin_character_state_sync/login_provider.py`.
- Normal local sync writes outside the checkout and does not commit or push results to GitHub.
  The legacy `tools/run_hoyo_sync.py` and `publish-main` commands are compatibility code with a
  fixed repository/Runtime identity, not the supported entrypoint for this repository.
- Offline validation verifies schemas, isolation and synthetic behavior; it is not evidence of
  successful real login or live account collection.

[Data handling and sharing](docs/sharing-safety.md) describes the private/public boundary.
No workflow or deployment is included. No LICENSE has been selected; public visibility alone
is not a grant of reuse rights under an open-source license. The owner will choose licensing separately.
