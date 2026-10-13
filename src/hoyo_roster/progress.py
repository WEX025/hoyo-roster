"""Fixed, privacy-safe progress labels and per-command login choices."""

from contextvars import ContextVar
from dataclasses import dataclass

LABELS = {
    "INITIALIZING_SESSION": "初始化官方页面会话；随后核验 Binding",
    "CHECKING_SESSION": "检查会话",
    "WAITING_FOR_ENTRY": "等待官方登录入口加载；可在官方页面继续操作，Ctrl-C 取消",
    "CONTROL_RETRY": "浏览器操控暂时超时；等待恢复，Ctrl-C 取消",
    "WAITING_FOR_USER": "等待用户在官方页面登录",
    "WAITING_FOR_MANUAL_CAPTCHA": (
        "检测到图片 CAPTCHA：请本人在本工具刚打开的官方浏览器窗口（所选游戏的登录页）"
        "点击图片或操作滑块。AI 不自动选择、不解图或绕过验证；保持此窗口和命令运行，"
        "当前会话保留，自动身份探测暂停，不重复发短信。可 Ctrl-C 取消；"
        "页面倒计时不代表短信实际送达。"
    ),
    "CAPTCHA_CLEARED": (
        "图片验证面板已消失；继续在同一官方窗口由本人完成发码、登录，再核验身份。"
        "这不代表验证通过或短信实际送达；未收到短信请查看官方页面提示，不自动重发。"
    ),
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
