"""聊天导出共享工具函数。

本模块包含 export_chat.py 和 export_all_chats.py 共用的消息格式化函数。
统一维护，避免两处代码漂移。
"""

import base64
import glob
import hashlib
import os

import mcp_server
from config import load_config


def _sha256_file(path, _chunk: int = 1024 * 1024) -> str:
    """流式计算文件 sha256（大文件不整体读入内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_chunk), b""):
            h.update(chunk)
    return h.hexdigest()

# 图片本体指纹优先读「已解码缓存」decoded_image_dir/<md5>.<ext>，**不碰 live attach 目录**：
# 该目录在本机巨大/极慢（os.listdir 都会卡死），解码是采集管道上游(decode-images)的活，
# 只对已解码产物现算 sha256。
_DECODED_IMAGE_DIR = None


def _decoded_image_dir():
    global _DECODED_IMAGE_DIR
    if _DECODED_IMAGE_DIR is None:
        _DECODED_IMAGE_DIR = (load_config().get("decoded_image_dir") or "decoded_images")
    return _DECODED_IMAGE_DIR


def _find_decoded_image(image_md5):
    """在 decoded_image_dir 找 <md5>.<ext>（已解码产物）。返回路径或 None。"""
    if not image_md5:
        return None
    hits = glob.glob(os.path.join(_decoded_image_dir(), f"{image_md5}.*"))
    return hits[0] if hits else None


MSG_TYPE_MAP = {
    1: "text",
    3: "image",
    34: "voice",
    42: "contact_card",
    43: "video",
    47: "sticker",
    48: "location",
    49: "link_or_file",
    50: "call",
    10000: "system",
    10002: "recall",
}


def _msg_type_str(local_type):
    base, _ = mcp_server._split_msg_type(local_type)
    return MSG_TYPE_MAP.get(base, f"type_{local_type}")


def _resolve_sender(row, ctx, names, id_to_username):
    """Resolve the sender of a message.

    Returns ``(display_label, platform_id)``:
      - display_label: "me" for the logged-in user, otherwise the sender's display
        name (the contact's name in 1-on-1 chats, the member's name in groups).
        Empty string for unattributable messages (e.g. system notifications).
      - platform_id: the sender's wxid/userid, or "" when unresolved.
        platform_id 解析不到一律置空，禁止用显示名硬凑。

    历史上本函数只返回 label 字符串；为输出发送者 id，改为返回元组。
    所有调用方已同步解包。
    """
    local_id, local_type, create_time, real_sender_id, content, ct = row
    decoded = mcp_server._decompress_content(content, ct)
    sender_from_content, _ = mcp_server._format_message_text(
        local_id, local_type, decoded, ctx["is_group"], ctx["username"], ctx["display_name"], names
    )
    platform_id, label = mcp_server._resolve_sender_identity(
        real_sender_id,
        sender_from_content,
        ctx["is_group"],
        ctx["username"],
        ctx["display_name"],
        names,
        id_to_username,
    )
    return (label or ""), (platform_id or "")


def _decode_sticker_desc(b64_desc):
    """WeChat encodes sticker labels as base64 protobuf: repeated (lang, text) pairs.
    Returns the 'default' language label (usually Chinese), or None.

    Limitation: treats the length byte as a single octet rather than a real protobuf
    varint — labels >127 bytes would be misread. In practice sticker descriptions are
    short (<30 chars), so this is adequate. Also sensitive to the bytes b"default"
    appearing inside a preceding value; no such cases observed.
    """
    try:
        raw = base64.b64decode(b64_desc)
    except Exception:
        return None
    # Find the 'default' marker; text follows as: \x12 <varint len> <utf-8>
    i = raw.find(b"default")
    if i < 0 or i + 7 >= len(raw) or raw[i + 7] != 0x12:
        return None
    try:
        text_len = raw[i + 8]
        text_bytes = raw[i + 9 : i + 9 + text_len]
        return text_bytes.decode("utf-8") or None
    except (IndexError, UnicodeDecodeError):
        return None


def _format_sticker_message(content):
    root = mcp_server._parse_xml_root(content) if content else None
    if root is None:
        return "[表情]"
    emoji = root.find(".//emoji")
    if emoji is None:
        return "[表情]"
    desc = emoji.get("desc") or ""
    label = _decode_sticker_desc(desc) if desc else None
    return f"[表情] {label}" if label else "[表情]"


def _format_system_message(content):
    if not content:
        return "[系统消息]"
    if "<sysmsg" not in content:
        return content
    root = mcp_server._parse_xml_root(content)
    if root is None:
        return content
    inner = root.findtext(".//content")
    return inner.strip() if inner else content


def _format_video_message(content):
    root = mcp_server._parse_xml_root(content) if content else None
    if root is None:
        return "[视频]"
    video = root.find(".//videomsg")
    if video is None:
        return "[视频]"
    playlength = video.get("playlength")
    return f"[视频] {playlength}秒" if playlength else "[视频]"


def _extract_transfer_extras(content):
    """Detect appmsg type=2000 and return structured transfer fields, else None.

    Reuses mcp_server._extract_transfer_info so the schema/version-quirks logic
    lives in one place. Empty values are dropped to keep the export compact.
    Numeric timestamps are returned as ints (consistent with the top-level
    `timestamp` field), not iso strings — downstream consumers can format.
    """
    if not content or '<appmsg' not in content:
        return None
    root = mcp_server._parse_app_message_outer(content)
    if root is None:
        return None
    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return None
    app_type = mcp_server._parse_int(
        mcp_server._collapse_text(appmsg.findtext('type') or ''), 0
    )
    if app_type != 2000:
        return None

    info = mcp_server._extract_transfer_info(appmsg)
    if not info:
        return None

    out = {}
    if info['paysubtype_label']:
        out['direction'] = info['paysubtype_label']
    for k in ('paysubtype', 'fee_desc', 'pay_memo',
              'payer_username', 'receiver_username',
              'transfer_id', 'transcation_id', 'pay_msg_id'):
        v = info.get(k)
        if v:
            out[k] = v
    for k in ('begin_transfer_time', 'invalid_time'):
        v = mcp_server._parse_int(info.get(k) or '', 0)
        if v:
            out[k] = v
    return out or None


def _image_dimensions(path):
    """(width, height) via PIL；PIL 不可用/读失败 → (None, None)。媒体宽高允许为空。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None, None


def media_fingerprint(username, local_id, base_type):
    """对**原件字节现算 sha256**（绝不复用 packed_info_data 里的 md5——
    那只用于在磁盘定位原件）。四种媒体同口径：media_sha256 一律 = sha256(原件字节)。

    返回 media[] 单条 dict（含 media_sha256/mime/bytes/duration_ms/width/height/filename）
    或 None（原件取不到，如图片未下载/语音缺失——交由调用方按 §6 申报，不静默假装有）。

    本轮覆盖 image(3) / voice(34)——库内可解码的两类，占媒体本体绝大多数。
    video(43)/file 本体在 attach 文件系统，按 §6「未下载」路径处理，待提取后同口径补指纹。
    """
    if base_type == 3:  # 图片：local_id → md5（静态库，快） → 已解码缓存 → 现算 sha256
        try:
            image_md5 = mcp_server._image_resolver.get_image_md5(username, local_id)
        except Exception:
            image_md5 = None
        path = _find_decoded_image(image_md5)
        if not path:               # 未解码（管道上游尚未 decode）→ 不出 media 条目（§6 待补）
            return None
        fmt = os.path.splitext(path)[1].lstrip(".").lower() or "jpg"
        mime = "image/jpeg" if fmt in ("jpg", "jpeg") else f"image/{fmt}"
        w, h = _image_dimensions(path)
        return {
            "media_sha256": _sha256_file(path),
            "mime": mime,
            "bytes": os.path.getsize(path),
            "duration_ms": None, "width": w, "height": h,
            "filename": os.path.basename(path),
        }

    if base_type == 34:  # 语音：silk → wav
        row = mcp_server._fetch_voice_row(username, local_id)
        if not row:
            return None
        voice_data, create_time = row
        try:
            wav_path, pcm_len = mcp_server._silk_to_wav(
                voice_data, create_time, username, local_id
            )
        except Exception:
            return None
        # _silk_to_wav 输出 24kHz/16bit 单声道 PCM；duration = pcm 字节数 / (24000*2)
        duration_ms = int(pcm_len / (24000 * 2) * 1000) if pcm_len else None
        return {
            "media_sha256": _sha256_file(wav_path),
            "mime": "audio/wav",
            "bytes": os.path.getsize(wav_path),
            "duration_ms": duration_ms, "width": None, "height": None,
            "filename": os.path.basename(wav_path),
        }

    return None


def render_content_for_db(local_id, local_type, content, ct, chat_username, chat_display_name):
    """给 realtime 落库线渲染 chat_message.content 的人话文本。

    与导出线 _extract_content 同一套解析（appmsg 文件/引用/链接、系统消息等），
    差异有二：
    - 图片/语音消息给占位符——落库表另有 media_caption / voice_transcript 专列，
      content 不该是 <img>/<voicemsg> 的原始 XML；
    - 渲染不出来（空文本/未知类型）回退解压+去群前缀后的原文，宁可存原文
      不丢信息，也绝不把原始 bytes/XML 以外的东西弄丢。
    """
    base, _sub = mcp_server._split_msg_type(local_type)
    if base == 3:
        return "[图片]"
    if base == 34:
        return "[语音]"
    rendered, _extras = _extract_content(
        local_id, local_type, content, ct, chat_username, chat_display_name)
    if rendered:
        return rendered
    raw = mcp_server._decompress_content(content, ct)
    if not isinstance(raw, str):
        return raw
    is_group = bool(chat_username) and chat_username.endswith('@chatroom')
    if is_group:
        _, raw = mcp_server._parse_message_content(raw, local_type, True)
    return raw


def _extract_content(local_id, local_type, content, ct, chat_username, chat_display_name):
    """Return (rendered_text, extras_dict). Either may be None.

    extras carries structured fields for non-text message types where caller
    wants more than the human-readable string (currently: transfer). Future
    additions (video号 metadata, merged-forward expansion, …) can flow through
    the same channel without changing the caller signature.
    """
    content = mcp_server._decompress_content(content, ct)
    if content is None:
        return None, None

    # 群消息的 content 形如 'wxid_xxx:\n<xml...>'。Issue #88: 之前直接把
    # 带前缀的字符串喂给 XML 解析器，群里的引用回复 / 卡片 / 视频等都因
    # 解析失败导致 type 渲染成 link_or_file 且 content 为空。
    is_group = bool(chat_username) and chat_username.endswith('@chatroom')
    if is_group:
        _, content = mcp_server._parse_message_content(content, local_type, True)

    # names 用于群引用回复的发送者名解析（_resolve_quote_sender_label）。
    # 1-on-1 场景也能用到（按 wxid 查显示名）。
    names = mcp_server.get_contact_names()

    base, _ = mcp_server._split_msg_type(local_type)
    if base == 1:
        return (content or ""), None
    if base == 42:
        return mcp_server._format_namecard_text(content), None
    if base == 43:
        return _format_video_message(content), None
    if base == 47:
        return _format_sticker_message(content), None
    if base == 48:
        return mcp_server._format_location_text(content), None
    if base == 49:
        rendered = mcp_server._format_app_message_text(
            content, local_type, is_group, chat_username, chat_display_name, names
        )
        transfer = _extract_transfer_extras(content)
        extras = {'type': 'transfer', 'transfer': transfer} if transfer else None
        return rendered, extras
    if base == 50:
        return mcp_server._format_voip_message_text(content), None
    if base == 10000:
        return _format_system_message(content), None
    if base == 10002:
        return "[撤回消息]", None
    return None, None
