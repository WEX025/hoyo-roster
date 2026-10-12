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
.venv/bin/pip install -e ./skills/hoyo-browser-session -e ./skills/genshin-character-state-sync -e ./skills/zzz-character-state-sync -e ./skills/hsr-character-state-sync
.venv/bin/python -m playwright install chromium
```

No configuration file is needed for normal use. Replace `YOUR_UID` with your own game UID:

```bash
.venv/bin/python tools/run_local_hoyo.py --game genshin --uid YOUR_UID
.venv/bin/python tools/run_local_hoyo.py --game zzz --uid YOUR_UID
.venv/bin/python tools/run_local_hoyo.py --game hsr --uid YOUR_UID
```

Default operation is `sync` with a headed official browser. The program automatically creates
private external data and browser directories isolated by game and UID. On first use or expired
login, complete the official login yourself; the command resumes only after exact Binding passes.
UID selects an identity and does not bypass login, privacy or permissions. Supported sources are
mainland China only (Genshin `hk4e_cn`, supported regions `cn_gf01`/`cn_qd01`; ZZZ `nap_cn` and
HSR `hkrpg_cn` use `prod_gf_cn`). Never give cookies, tokens or passwords to the assistant.

Use `--operation login` for a login-only check, `--operation init` to create an empty workspace
without launching a browser, or `--operation inspect` for residue inspection. Already verified
sessions may use `--headless`; missing or mismatched authentication then fails immediately.
The advanced [configuration mode](docs/local-use.md) remains available. Do not combine direct
`--game/--uid` arguments with `--config/--user` or the old positional command syntax.

## Current limits

- Local sync writes outside the checkout and does not commit or push results to GitHub. The
  legacy `tools/run_hoyo_sync.py` and `publish-main` commands are compatibility code with a fixed
  repository/Runtime identity, not the supported entrypoint for this repository.
- Genshin first-use identity selection now uses the explicit UID in memory and verifies the
  actual game, UID and supported region through official Binding before collection. It does not
  seed fake Profile fields. Offline tests preserve the real Provider/coordinator and two-pass
  collector, with simulated network/browser responses; they are not live login acceptance.
- Changing source code preserves external personal data and configuration. Browser Profiles are
  now isolated per game/UID in both modes. Legacy advanced-mode shared Profile files are retained
  but not automatically copied or reused; the first run after that isolation upgrade may need login.

[Data handling and sharing](docs/sharing-safety.md) describes the private/public boundary.
No workflow or deployment is included. No LICENSE has been selected; public visibility alone
is not a grant of reuse rights under an open-source license. The owner will choose licensing separately.
