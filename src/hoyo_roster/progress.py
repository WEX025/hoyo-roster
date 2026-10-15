"""Fixed progress labels, without authentication inputs."""

LABELS = {
    "CHECKING_SESSION": "检查客户端共享会话",
    "WAITING_FOR_USER": "请在客户端内置浏览器自行登录，并确认是否完成",
    "LOGIN_COMPLETED": "用户已确认完成登录，交接同一客户端会话",
    "VERIFYING_IDENTITY": "游戏模块核验本人身份",
    "COLLECTING_1": "采集第1次",
    "COLLECTING_2": "采集第2次",
    "VALIDATING": "校验数据与来源覆盖",
    "SAVING": "正在保存",
    "SAVED": "数据已保存",
    "UNCHANGED": "数据无变化",
    "CLEANUP_PENDING": "保存后的清理尚有待办",
    "CANCELLED": "操作已取消",
}


def emit(state):
    print("PROGRESS=" + state + "\n提示=" + LABELS[state], flush=True)
