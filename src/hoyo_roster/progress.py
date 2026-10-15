"""Fixed, privacy-safe progress labels and per-command login choices."""

from contextvars import ContextVar
from dataclasses import dataclass, field

LABELS = {
    "INITIALIZING_SESSION": "初始化官方页面会话；随后核验 Binding",
    "CHECKING_SESSION": "检查会话",
    "WAITING_FOR_ENTRY": "等待官方登录入口加载；可在官方页面继续操作，Ctrl-C 取消",
    "OPENING_LOGIN_PANEL": "打开官方普通登录面板",
    "SELECTING_METHOD": "选择已授权的登录方式",
    "LOGIN_PANEL_MANUAL": "页面结构未确认或操控暂不可用；请本人打开/切换登录面板，仍在原窗口等待，Ctrl-C 取消",
    "QR_READY": "二维码已加载；请本人扫码并在手机确认，扫码成功仍须核验身份",
    "QR_SCANNED_WAIT_CONFIRM": "官方页面提示已扫码；请本人在手机确认，尚未证明登录成功",
    "WAITING_FOR_PHONE_AND_TERMS": "请本人在官方窗口输入手机号、阅读并确认协议、点击一次发送；程序不读输入值或自动发送",
    "PHONE_ENTERED": "已在本工具的官方手机号窗口填写临时号码；未发短信，不输出或保存号码",
    "WAITING_FOR_TERMS": "请本人在本工具打开的官方窗口阅读并勾选协议；程序不自动接受，勾选后至多点击一次已授权发码",
    "PHONE_AUTOMATION_MANUAL": "官方手机号/协议/发送控件未能唯一核验；停止自动操作，请在同一窗口本人处理，不猜坐标或重试",
    "PHONE_AUTHORIZATION_USED": "本命令的一次自动发码授权已使用；后续身份不再次读号码或自动发码",
    "SMS_SUBMITTING": "点击一次已授权发码控件；尚未确认实际请求或服务端接受，请勿重复点击",
    "SMS_ACTION_UNCERTAIN": "发码点击结果不确定；不自动重试，不要重复点击，等待实际请求/回执或超时",
    "SMS_REQUEST_NOT_OBSERVED": "未观察到短信 POST；不能确认请求提交或短信发送，不自动重试",
    "SMS_REQUESTING": "已观察到一次官方发码请求，等待回执；不要重复点击",
    "SMS_ACCEPTED": "官方回执确认接受新发码；不代表手机送达，请本人输入验证码",
    "SMS_COOLDOWN": "官方回执表示已有验证码/倒计时，未新发短信；请查看官方页面，不自动重发",
    "SMS_RECEIPT_UNKNOWN": "发码回执未知；停止本次流程，不能确认已发，不自动重试",
    "LOGIN_CONFIRMED": "所选游戏/UID/区服 Binding 已通过；继续完整操作和关闭检查",
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
class PhoneInput:
    used: bool = False


@dataclass
class LoginOptions:
    method: str | None = None
    non_interactive: bool = False
    timeout: float = 900
    allow_login: bool = False
    consent_given: bool = False
    phone_input: PhoneInput | None = field(default=None, repr=False)


def options_from_args(args) -> LoginOptions:
    phone_input = None
    if getattr(args, "phone_stdin", False) and getattr(args, "allow_sms", False):
        # Share the once-only authorization across discovery and exact-role stages.
        if not hasattr(args, "_phone_input"):
            args._phone_input = PhoneInput()
        phone_input = args._phone_input
    return LoginOptions(
        args.login_method,
        args.non_interactive,
        args.login_timeout,
        args.allow_login,
        phone_input=phone_input,
    )


OPTIONS: ContextVar[LoginOptions | None] = ContextVar("hoyo_login_options", default=None)
