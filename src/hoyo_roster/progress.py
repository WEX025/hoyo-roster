"""Fixed, privacy-safe progress labels and per-command login choices."""

from contextvars import ContextVar
from dataclasses import dataclass

LABELS = {
    "INITIALIZING_SESSION": "初始化官方页面会话；随后核验 Binding",
    "CHECKING_SESSION": "检查会话",
    "WAITING_FOR_ENTRY": "等待官方登录入口加载；可在官方页面继续操作，Ctrl-C 取消",
    "CONTROL_RETRY": "浏览器操控暂时超时；等待恢复，Ctrl-C 取消",
    "WAITING_FOR_USER": "等待用户在官方页面登录",
    "VERIFYING_IDENTITY": "核验游戏、UID和区服身份",
    "COLLECTING_1": "采集第1次",
    "COLLECTING_2": "采集第2次",
    "VALIDATING": "验证双次结果和候选数据",
    "SAVING": "保存数据",
    "UNCHANGED": "数据无变化",
    "SAVED": "数据已保存",
    "CLEANUP_PENDING": "保存已成功，清理待办；执行 --operation recover",
    "CANCELLED": "已取消；可重新运行命令",
    "TIMEOUT": "登录超时；检查官方页面后重新运行",
    "METHOD_UNAVAILABLE": "官方页面未确认所选登录入口；取消后检查页面支持或明确选择另一方式",
}


def emit(code: str) -> None:
    print(f"PROGRESS={code}\n提示={LABELS.get(code, code)}", flush=True)


@dataclass
class LoginOptions:
    method: str | None = None
    non_interactive: bool = False
    timeout: float = 900
    allow_login: bool = False
    consent_given: bool = False


OPTIONS: ContextVar[LoginOptions | None] = ContextVar("hoyo_login_options", default=None)
