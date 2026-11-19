"""统一输出布局：所有"按聊天分组"的产物（聊天记录 / 图片 / 音频 / 文件）
都落到项目外的一个根目录下，每个聊天一个文件夹。

目录结构：
    <output_root>/                                    # config.json: output_root，默认 ~/WeChatExport
      群聊-某项目讨论组-12345678901@chatroom/
        chat.json        # 聊天记录
        images/          # 解密后的高清图片
        audio/           # 转码后的 mp3 语音
        files/           # pdf / word / excel / md ... (未来)
      私聊-张三-wxid_example/
        ...
      公众号-XXX-gh_xxxxxx/
        ...

文件夹名 = 「类型-显示名-username」。username 用于消歧（同名聊天不冲突）。
子目录/文件用英文（chat.json / images / audio / files），避免跨平台中文路径问题。
"""
import hashlib
import os
import re

from config import load_config

# 子目录名（英文）
SUBDIR_IMAGES = "images"
SUBDIR_AUDIO = "audio"
SUBDIR_FILES = "files"
SUBDIR_VIDEO = "video"
CHAT_RECORD_NAME = "chat.json"

# 文件名非法字符（Win/macOS 通吃）；保留 @（@chatroom 需要）
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 特殊服务账号 → 归类“其他”
_OTHER_ACCOUNTS = {
    "filehelper", "weixin", "fmessage", "medianote", "floatbottle",
    "qqmail", "tmessage", "qmessage", "newsapp",
}


def output_root():
    """输出根目录（项目外，可配置）。默认 ~/WeChatExport。"""
    cfg = load_config()
    root = cfg.get("output_root") or "~/WeChatExport"
    return os.path.abspath(os.path.expanduser(root))


def chat_type_label(username):
    """返回聊天类型中文标签：群聊 / 公众号 / 其他 / 私聊。"""
    u = username or ""
    if u.endswith("@chatroom"):
        return "群聊"
    if u.startswith("gh_"):
        return "公众号"
    if u in _OTHER_ACCOUNTS or u.endswith("@openim"):
        return "其他"
    return "私聊"


def _sanitize(value, max_len=80):
    """清掉文件名非法字符、首尾点/空格；过长截断。"""
    s = _UNSAFE.sub("_", str(value or "")).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s or "unknown"


def chat_folder_name(username, display_name=None):
    """「类型-显示名-username」。如 群聊-某项目讨论组-12345678901@chatroom。
    无显示名(name==username)时省略重复段，避免 群聊-xxx@chatroom-xxx@chatroom。"""
    label = chat_type_label(username)
    user = _sanitize(username or "unknown", max_len=120)
    name = _sanitize(display_name or username or "unknown")
    if not display_name or name == user:
        return f"{label}-{user}"
    return f"{label}-{name}-{user}"


def chat_dir(username, display_name=None, create=True, root=None):
    """该聊天的根文件夹绝对路径。root 默认 output_root()，可被调用方覆盖
    （如 export 的自定义输出目录 / 测试临时目录）。"""
    base = root or output_root()
    path = os.path.join(base, chat_folder_name(username, display_name))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def chat_subdir(username, display_name=None, kind=SUBDIR_IMAGES, create=True, root=None):
    """该聊天下的子目录（images / audio / files）。"""
    path = os.path.join(chat_dir(username, display_name, create=create, root=root), kind)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def chat_record_path(username, display_name=None, create_dir=True, root=None):
    """该聊天的聊天记录 JSON 路径 <chat_dir>/chat.json。"""
    return os.path.join(
        chat_dir(username, display_name, create=create_dir, root=root), CHAT_RECORD_NAME
    )


def username_hash_map(usernames):
    """{md5(username): username}，给"只有 chat_hash 目录名"的批量图片解密反查用。"""
    return {hashlib.md5(u.encode("utf-8")).hexdigest(): u for u in usernames if u}
