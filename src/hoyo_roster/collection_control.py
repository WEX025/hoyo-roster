"""Private JSON control boundary for the packaged foreground workflow."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys

from .collection_environment import prepare, inventory
from .collection_state import RunStore, TERMINAL, utc
from .errors import WorkspaceError
from .paths import default_private_root, verified_repository
from .storage.common import load_metadata
from .user_workspace import private_directory


def public(value):
    state = value["state"]
    action = (
        "DONE"
        if state == "SUCCEEDED"
        else "STOP"
        if state in {"FAILED", "CANCELLED"}
        else "CONFIRM_LOGIN"
        if state == "WAITING_LOGIN"
        else "ATTACH_BROWSER"
        if state == "WAITING_BROWSER"
        else "CONTINUE"
    )
    return {
        "runId": value["id"],
        "state": state,
        "nextAction": action,
        "done": state in TERMINAL,
        "errorCode": value["error_code"],
        "question": value["question"],
        "attempts": len(value["attempts"]),
        "completedGames": sorted(value["completed"]),
    }


def config_options(options):
    allowed = {
        "parallelGames",
        "runtimeRoot",
        "update",
        "repairInstallation",
        "installationArtifact",
        "baseline",
    }
    if set(options) - allowed:
        raise WorkspaceError("WORKFLOW_OPTIONS_INVALID")
    for key in ("parallelGames", "update", "repairInstallation"):
        if key in options and type(options[key]) is not bool:
            raise WorkspaceError("WORKFLOW_OPTIONS_INVALID")
    package = Path(__file__).resolve().parent
    repository = verified_repository(package.parents[1]) if package.parent.name == "src" else None
    absolute = (
        lambda key: str(Path(options[key]).expanduser().absolute()) if options.get(key) else None
    )
    return {
        "package_directory": str(package),
        "repository": str(repository) if repository else None,
        "python": sys.executable,
        "parallel_games": options.get("parallelGames", False),
        "runtime_root": absolute("runtimeRoot") or str(default_private_root()),
        "update": options.get("update", False),
        "repair_installation": options.get("repairInstallation", True),
        "installation_artifact": absolute("installationArtifact"),
        "baseline": absolute("baseline"),
    }


def dispatch(request):
    action = request["action"]
    directory = Path(request["directory"])
    if not directory.is_absolute():
        raise WorkspaceError("WORKFLOW_OPTIONS_INVALID")
    if action == "create":
        store = RunStore.create(
            directory, config_options(request.get("options", {})), request["owner"]
        )
        with store.edit() as value:
            if request.get("bootstrap"):
                bootstrap = request["bootstrap"]
                value["created_at"] = bootstrap["start_at"]
                value["phases"].append({"phase": "BOOTSTRAP", **bootstrap, "started_epoch": 0})
            store.phase(value, "PREPARING")
    else:
        store = RunStore(directory)
    if action == "claim":
        store.claim(request["owner"])
    elif action not in {"create", "inspect"}:
        value = store.read()
        store.owned(value, request["token"])
        if action == "prepare":
            if value["state"] != "PREPARING":
                raise WorkspaceError("WORKFLOW_TRANSITION_INVALID")
            with store.edit() as current:
                store.owned(current, request["token"])
                current["preparation_pid"] = os.getpid()
            try:
                version = prepare(store)
                with store.edit() as current:
                    store.owned(current, request["token"])
                    current["version"] = version
                    store.phase(current, "WAITING_BROWSER")
            finally:
                with store.edit() as current:
                    if current["preparation_pid"] == os.getpid():
                        current["preparation_pid"] = None
        elif action == "begin":
            if value["state"] != "WAITING_BROWSER":
                raise WorkspaceError("WORKFLOW_TRANSITION_INVALID")
            number = len(value["attempts"]) + 1
            private_directory(directory / "attempts" / f"{number:04d}")
            with store.edit() as current:
                store.owned(current, request["token"])
                current["attempts"].append(
                    {
                        "number": number,
                        "started_at": utc(),
                        "closed_at": None,
                        "worker_pid": None,
                        "outcome": "RUNNING",
                    }
                )
                store.phase(current, "COLLECTING")
        elif action == "observe":
            if value["state"] not in {"COLLECTING", "WAITING_LOGIN"}:
                raise WorkspaceError("WORKFLOW_TRANSITION_INVALID")
            attempt = request.get("attempt")
            with store.edit() as current:
                question = (
                    {
                        "attempt": f"{current['attempts'][-1]['number']}:{attempt}",
                        "question": "请在官方页面自行完成登录，然后确认是否已完成。",
                        "options": ["已完成登录", "尚未完成", "取消"],
                    }
                    if attempt is not None
                    else None
                )
                target = "WAITING_LOGIN" if question else "COLLECTING"
                if current["state"] != target:
                    store.phase(current, target)
                current["question"] = question
        elif action == "finish":
            if value["state"] not in {"COLLECTING", "WAITING_LOGIN"}:
                raise WorkspaceError("WORKFLOW_TRANSITION_INVALID")
            attempt = value["attempts"][-1]
            result = load_metadata(
                directory / "attempts" / f"{attempt['number']:04d}" / "result.json", private=True
            )
            error = result["errorCode"]
            if not error:
                error = next(
                    (
                        e.split("=", 1)[1]
                        for e in reversed(result["events"])
                        if re.fullmatch(r"ERROR_CODE=[A-Z_]+", e)
                    ),
                    None,
                )
            with store.edit() as current:
                store.owned(current, request["token"])
                current["attempts"][-1].update(
                    closed_at=utc(),
                    worker_pid=None,
                    outcome="PASS" if result["code"] == 0 else "FAIL",
                )
                if result["code"] == 0 and result["errorCode"] is None:
                    store.phase(current, "VERIFYING")
                else:
                    store.phase(current, "FAILED", error=error or "WORKFLOW_COLLECTION_FAILED")
        elif action == "verify":
            from .collection_report import validate_completed

            if value["state"] != "VERIFYING":
                raise WorkspaceError("WORKFLOW_TRANSITION_INVALID")
            current_digest = sha256(
                json.dumps(
                    inventory(Path(value["config"]["package_directory"])), sort_keys=True
                ).encode()
            ).hexdigest()
            if current_digest != value["version"]["package_digest"]:
                raise WorkspaceError("WORKFLOW_VERSION_CHANGED")
            validate_completed(store)
            with store.edit() as current:
                store.owned(current, request["token"])
                store.phase(current, "REPORTING")
        elif action == "report":
            from .collection_report import make_report

            if value["state"] != "REPORTING":
                raise WorkspaceError("WORKFLOW_TRANSITION_INVALID")
            make_report(store)
            with store.edit() as current:
                store.owned(current, request["token"])
                store.phase(current, "SUCCEEDED")
        elif action == "retry":
            allowed = {
                "WORKFLOW_PREPARATION_FAILED",
                "WORKFLOW_SOURCE_DIRTY",
                "WORKFLOW_INSTALLATION_MISMATCH",
                "WORKFLOW_INSTALLATION_SOURCE_REQUIRED",
                "CLIENT_REQUEST_FAILED",
                "CLIENT_PAGE_UNAVAILABLE",
                "CLIENT_BROWSER_UNAVAILABLE",
                "WORKFLOW_EXECUTION_FAILED",
            }
            if value["state"] != "CANCELLED" and (
                value["state"] != "FAILED" or value["error_code"] not in allowed
            ):
                raise WorkspaceError("WORKFLOW_RETRY_FORBIDDEN")
            with store.edit() as current:
                store.owned(current, request["token"])
                store.phase(current, "PREPARING")
        elif action in {"fail", "cancel"}:
            with store.edit() as current:
                store.owned(current, request["token"])
                if current["state"] not in TERMINAL:
                    if current["attempts"] and current["attempts"][-1]["closed_at"] is None:
                        current["attempts"][-1].update(
                            closed_at=utc(), worker_pid=None, outcome="FAIL"
                        )
                    store.phase(
                        current,
                        "CANCELLED" if action == "cancel" else "FAILED",
                        error=request.get("error", "WORKFLOW_EXECUTION_FAILED"),
                    )
        else:
            raise WorkspaceError("WORKFLOW_ACTION_INVALID")
    result = store.read()
    return {
        "status": public(result),
        "config": result["config"],
        "version": result["version"],
        "attempt": result["attempts"][-1]["number"] if result["attempts"] else None,
    }


def main():
    try:
        if sys.argv[1:] == ["--entrypoint"]:
            print(
                json.dumps(
                    {
                        "entrypoint": Path(__file__).with_name("client_workflow.mjs").as_uri(),
                        "python": sys.executable,
                        "format": "hoyo-roster-workflow-v1",
                    }
                )
            )
            return 0
        if len(sys.argv) == 3 and sys.argv[1] == "--status":
            print(json.dumps(public(RunStore(sys.argv[2]).read())))
            return 0
        if sys.argv[1:] == ["--help"]:
            print("hoyo-roster-workflow --entrypoint | --status ABSOLUTE_RUN_DIRECTORY")
            return 0
        if sys.argv[1:]:
            raise WorkspaceError("WORKFLOW_OPTIONS_INVALID")
        request = json.load(sys.stdin)
        result = dispatch(request)
        print(json.dumps({"ok": True, **result}))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, WorkspaceError) else "WORKFLOW_CONTROL_FAILED"
        print(json.dumps({"ok": False, "errorCode": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
