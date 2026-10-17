"""Guard the current browser workflow without opening a browser or reading user data."""

import ast
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/hoyo_roster"
REMOVED = (
    "browser.py",
    "browser_runtime.py",
    "discovery.py",
    "login_identity.py",
    "login_ui.py",
    "login_controls.py",
    "phone_login.py",
    "otp_login.py",
    "terms_login.py",
    "sms.py",
    "terminal.py",
    "games/genshin/browser.py",
    "games/genshin/login_provider.py",
    "games/zzz/browser.py",
    "games/zzz/login_provider.py",
    "games/hsr/browser.py",
    "games/hsr/browser_launch.py",
    "games/hsr/endpoint_specs.py",
    "games/hsr/login_provider.py",
)
OLD_FLAGS = {
    "--auth-root",
    "--login-method",
    "--non-interactive",
    "--phone-stdin",
    "--allow-sms",
    "--allow-terms",
    "--allow-login",
    "--login-timeout",
    "--headless",
    "--resume-discovery",
}
OLD_SYMBOLS = {
    "coordinate_login",
    "AuthService",
    "PhoneLoginAction",
    "OtpLoginAction",
    "TermsLoginAction",
    "SmsReceiptWatch",
    "launch_profile",
    "async_playwright",
}
FORBIDDEN_IMPORTS = {"playwright", "requests", "httpx"}


def document_problems(text: str, relative: str, operations: set[str]) -> list[str]:
    """Check obsolete instructions, not general mentions of browser authentication."""
    problems = []
    if (
        any(flag in text for flag in OLD_FLAGS)
        or re.search(r"--operation\s+logout\b", text)
        or "回退空基线或 logout" in text
    ):
        problems.append("OBSOLETE_WORKFLOW_INSTRUCTION:" + relative)
    if "关闭本工具和确切浏览器" in text or "account/Profile lock" in text:
        problems.append("OBSOLETE_BROWSER_OWNERSHIP:" + relative)
    for operation in re.findall(r"--operation\s+([a-z][a-z-]*)", text):
        if operation not in operations:
            problems.append("UNDOCUMENTED_OPERATION:" + relative + ":" + operation)
    for call in re.findall(r"answerLogin\(([^\n)]*)\)", text):
        if "," not in call:
            problems.append("UNSCOPED_LOGIN_ANSWER:" + relative)
    return problems


def validation_status_problems(text: str) -> list[str]:
    """One current status table; historical evidence must not declare current failure."""
    blocks = re.findall(
        r"<!-- platform-status:start -->(.*?)<!-- platform-status:end -->", text, re.S
    )
    if len(blocks) != 1:
        return ["PLATFORM_STATUS_BLOCK_INVALID"]
    rows = re.findall(r"^\| (Windows|macOS) \| (PASS|PENDING) \| .+ \|$", blocks[0], re.M)
    problems = (
        []
        if sorted(platform for platform, _ in rows) == ["Windows", "macOS"]
        else ["PLATFORM_STATUS_ROWS_INVALID"]
    )
    historical = text.replace(blocks[0], "")
    if re.search(
        r"当前\s*(?:Windows|macOS)[^\n]*(?:未通过|已通过|\bPASS\b|\bFAILED\b|\bSUCCEEDED\b)",
        historical,
    ):
        problems.append("CONFLICTING_CURRENT_PLATFORM_CLAIM")
    return problems


def check():
    from hoyo_roster.cli import build_parser

    operations = set(build_parser()._option_string_actions["--operation"].choices)
    problems = []
    for relative in REMOVED:
        if (PACKAGE / relative).exists():
            problems.append("REMOVED_SOURCE_PRESENT:" + relative)
    if (ROOT / "docs/history").exists() or (ROOT / "docs/login-contract.md").exists():
        problems.append("OLD_WORKFLOW_DOCUMENT_PRESENT")
    modules = 0
    for path in PACKAGE.rglob("*.py"):
        if "contracts" in path.parts:
            continue
        modules += 1
        source = path.read_text(encoding="utf-8")
        problems.extend(document_problems(source, path.relative_to(ROOT).as_posix(), operations))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in OLD_SYMBOLS
            ):
                problems.append("OLD_DEFINITION:" + path.relative_to(ROOT).as_posix())
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [n.name for n in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if any(name.split(".")[0] in FORBIDDEN_IMPORTS for name in names):
                    problems.append(
                        "INDEPENDENT_BROWSER_OR_NETWORK_IMPORT:" + path.relative_to(ROOT).as_posix()
                    )
                if path.name == "login.py" and any("games" in name.split(".") for name in names):
                    problems.append("LOGIN_IMPORTS_GAME")
    help_result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "hoyo_roster", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    if any(option in help_result.stdout for option in OLD_FLAGS):
        problems.append("OLD_PUBLIC_OPTION_PRESENT")
    links = 0
    documents = 0
    for path in (
        [ROOT / name for name in ("README.md", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md")]
        + list((ROOT / "docs").rglob("*.md"))
        + list((ROOT / ".agents").rglob("SKILL.md"))
    ):
        documents += 1
        source = path.read_text(encoding="utf-8")
        problems.extend(document_problems(source, path.relative_to(ROOT).as_posix(), operations))
        if path == ROOT / "docs/release-validation.md":
            problems.extend(validation_status_problems(source))
        if (
            path in {ROOT / "README.md", ROOT / "docs/platform-support.md"}
            and "release-validation.md#当前平台状态" not in source
        ):
            problems.append(
                "MISSING_CURRENT_PLATFORM_STATUS_LINK:" + path.relative_to(ROOT).as_posix()
            )
        for reference in re.findall(r"\]\(([^\n)]+)\)", source):
            reference = reference.strip().strip("<>")
            if re.match(r"^[a-z][a-z0-9+.-]*:", reference, re.I) or reference.startswith("#"):
                continue
            destination = unquote(reference.split("#", 1)[0])
            links += 1
            if not (path.parent / destination).is_file():
                problems.append("BROKEN_LINK:" + path.relative_to(ROOT).as_posix())
    result = {
        "status": "FAIL" if problems else "PASS",
        "scope": "listed static checks and local links; behavioral tests and real-client acceptance are separate",
        "runtime_modules": modules,
        "removed_paths_checked": len(REMOVED),
        "obsolete_cli_flags_checked": len(OLD_FLAGS),
        "local_links_checked": links,
        "workflow_documents_checked": documents,
        "problems": problems,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(check())
