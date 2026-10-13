"""Native, offline platform acceptance in disposable roots; never opens a login page."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from hoyo_roster import platform_io as io
from hoyo_roster.cli import CONTRACT_ROOT
from hoyo_roster.config import Settings
from hoyo_roster.login_identity import LoginIdentityStore
from hoyo_roster.storage.common import tree_digest
from hoyo_roster.games.genshin.candidate import candidate_policy

from hoyo_roster.user_workspace import LocalSnapshotStore, direct_configuration


def check() -> None:
    with tempfile.TemporaryDirectory(prefix="hoyo-platform-") as temporary:
        root = Path(temporary).resolve()
        result, auth = root / "results", root / "auth"
        config = direct_configuration("genshin", "123456789", storage_root=result, auth_root=auth)
        store = LocalSnapshotStore(
            CONTRACT_ROOT, config, "genshin", candidate_policy=candidate_policy()
        )
        code = """import sys, portalocker
from pathlib import Path
from hoyo_roster.platform_io import exclusive_lock
try:
    with exclusive_lock(Path(sys.argv[1])):
        pass
except portalocker.exceptions.LockException:
    sys.exit(23)
"""
        lock = store.auth_user_root / "workspace.lock"
        with store.locked():
            assert (
                subprocess.run(
                    [sys.executable, "-c", code, str(lock)], capture_output=True
                ).returncode
                == 23
            )
            old = store._install(None)
            digest = tree_digest(old)
            with patch.object(io, "sync_file", side_effect=OSError("synthetic flush failure")):
                try:
                    store._install(None)
                except OSError:
                    pass
                else:
                    raise AssertionError("flush failure was ignored")
            assert store.current() == old and tree_digest(old) == digest
            import hoyo_roster.user_workspace as workspace

            remove = workspace.remove_owned_tree

            def fail_cleanup(path):
                if path == old:
                    raise OSError("synthetic cleanup failure")
                return remove(path)

            with patch.object(workspace, "remove_owned_tree", side_effect=fail_cleanup):
                new = store._install(None)
            assert store.cleanup_pending and store.commit_state == "COMMITTED"
            assert old.exists() and store.current() == new
            assert store.recover() == new and not old.exists()
            assert not store.cleanup_pending
            for item in (new, *new.rglob("*")):
                assert io.private(item)
            identity = LoginIdentityStore(Settings(external_root=store.auth_root))
            assert len(identity._key()) == 32
            assert identity._key() == identity._key()
        assert (
            subprocess.run([sys.executable, "-c", code, str(lock)], capture_output=True).returncode
            == 0
        )
        args = [
            sys.executable,
            "-m",
            "hoyo_roster",
            "--game",
            "genshin",
            "--uid",
            "123456789",
            "--storage-root",
            str(result),
            "--auth-root",
            str(auth),
            "--operation",
            "inspect",
        ]
        inspected = subprocess.run(args, capture_output=True)
        assert inspected.returncode == 0
        assert b"CURRENT_RESULT=" in inspected.stdout and b"123456789" not in inspected.stdout
        assert (
            subprocess.run(
                [sys.executable, "-m", "hoyo_roster", "--help"], capture_output=True
            ).returncode
            == 0
        )
    print(f"NATIVE_PLATFORM={sys.platform}")
    print(
        "PRIVATE_PERMISSIONS=PASS\nPROCESS_LOCK=PASS\nSAVE_RECOVER=PASS\nIDENTITY_KEY=PASS\nCLI=PASS"
    )
    if os.name == "nt":
        print("WINDOWS_DIRECTORY_FSYNC=NOT_AVAILABLE\nPOWER_LOSS_ACCEPTANCE=NOT_RUN")
    print("LIVE_LOGIN=NOT_RUN\nLIVE_COLLECTION=NOT_RUN")


if __name__ == "__main__":
    try:
        check()
    except Exception:
        print("PLATFORM_SMOKE=FAIL\nERROR_CODE=PLATFORM_SMOKE_FAILED")
        sys.exit(1)
    print("PLATFORM_SMOKE=PASS")
