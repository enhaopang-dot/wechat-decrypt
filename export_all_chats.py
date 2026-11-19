#!/usr/bin/env python3
"""批量导出所有微信聊天记录为 JSON 文件，可选附带语音转录。

此脚本将导出所有会话的聊天记录，输出格式与 export_chat.py 完全一致。
支持导出到指定目录，默认输出到 ./exported_chats 目录。

语音转录通过 mcp_server 的 backend 配置驱动（config.json 中设置
transcription_backend 为 whisper_cpp / openai / local）。未启用 backend
或缺少依赖时仅导出文本消息，不报错。

用法:
    python3 export_all_chats.py                         # 全量导出所有会话
    python3 export_all_chats.py --write-plan-csv export_plan.csv
    python3 export_all_chats.py output_dir --from-plan-csv export_plan.csv
    python3 export_all_chats.py --with-transcriptions   # 全量导出 + 转录语音
    python3 export_all_chats.py -i                      # 增量（只导出最新消息）
    python3 export_all_chats.py --start 2025-01-01      # 按日期范围
    python3 export_all_chats.py --end 2025-01-31
    python3 export_all_chats.py --start 2025-01-01 --end 2025-01-31 -t
"""

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import mcp_server
import output_layout

# 尝试导入 tqdm 作为进度条（可选）
try:
    from tqdm import tqdm as _tqdm
except ImportError:
    _tqdm = None

from chat_export_helpers import _extract_content, _msg_type_str, _resolve_sender


PLAN_CSV_FIELDS = [
    "export",
    "index",
    "username",
    "chat_name",
    "chat_type",
    "message_count",
    "first_time",
    "last_time",
    "attachment_estimated_bytes",
    "attachment_scanned_bytes",
    "total_estimated_bytes",
    "size_status",
]

EXPORT_INDEX_FILE = "_export_index.json"
EXPORT_INDEX_VERSION = 1
DELTA_SCHEMA_VERSION = 1
PLAN_MODE_BLACKLIST = "blacklist"
PLAN_MODE_WHITELIST = "whitelist"
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')


def _parse_timestamp(ts_str):
    """解析时间字符串返回 unix timestamp。
    支持格式: '2025-01-01', '2025-01-01 14:30', '2025-01-01T14:30:00'
    """
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(ts_str.strip(), fmt)
            return int(dt.timestamp())
        except ValueError:
            pass
    try:
        return int(ts_str)
    except ValueError:
        return None


def _get_last_message_ts(json_path):
    """读取已有 JSON 的最后一条消息时间戳"""
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        msgs = data.get("messages", [])
        if msgs:
            return msgs[-1].get("timestamp", 0)
    except (json.JSONDecodeError, IOError, KeyError):
        pass
    return 0


def _get_existing_messages(json_path):
    """读取已有 JSON 的消息列表（增量合并用）"""
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("messages", [])
    except (json.JSONDecodeError, IOError, KeyError):
        return []


def _existing_messages_or_status(json_path):
    """返回 (messages, status)。status: missing | ok | corrupt。
    用于增量：损坏(存在但解析失败)时要备份+全量重建，而不是静默当成空。"""
    if not os.path.isfile(json_path):
        return [], "missing"
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("messages", []), "ok"
    except (json.JSONDecodeError, OSError, KeyError):
        return [], "corrupt"


def _backup_corrupt_file(path):
    """把损坏的 chat.json 改名备份，避免被覆盖丢失。返回备份路径或 None。"""
    bak = f"{path}.corrupt.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        os.replace(path, bak)
        return bak
    except OSError:
        return None


def _db_max_local_id(message_tables):
    """该聊天在 DB 里的最大 local_id（用于检测微信重装导致的 local_id 回退）。"""
    mx = 0
    for ti in message_tables:
        name = ti.get("table_name", "")
        if not mcp_server._is_safe_msg_table_name(name):
            continue
        try:
            with closing(sqlite3.connect(ti["db_path"])) as conn:
                r = conn.execute(f"SELECT MAX(local_id) FROM [{name}]").fetchone()
                if r and r[0]:
                    mx = max(mx, int(r[0]))
        except Exception:
            pass
    return mx


def _atomic_write_json(path, obj):
    """原子写 JSON：先写 .tmp 再 os.replace，断电/中断不留半文件。"""
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _export_index_path(output_dir):
    return os.path.join(output_dir, EXPORT_INDEX_FILE)


def _empty_export_index():
    return {"version": EXPORT_INDEX_VERSION, "chats": {}}


def _safe_export_filename_part(value):
    cleaned = _UNSAFE_FILENAME_RE.sub("_", str(value or "")).strip()
    return cleaned or "unknown"


def _delta_run_id(now_ts=None):
    ts = int(now_ts if now_ts is not None else time.time())
    return datetime.fromtimestamp(ts).strftime("%Y%m%dT%H%M%S")


def _delta_filename(display_name, is_group, username):
    prefix = "group" if is_group else "single"
    label = _safe_export_filename_part(display_name or username or "unknown")
    user_part = _safe_export_filename_part(username)
    return f"{prefix}_{label}__{user_part}.delta.json"


def _content_hash_for_uid(content):
    if content is None:
        return ""
    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


def _delta_msg_uid(username, db_path, local_id, timestamp, msg_type, content):
    db_name = os.path.basename(str(db_path or ""))
    payload = (
        f"{username}|{db_name}|{int(local_id)}|{int(timestamp)}|"
        f"{msg_type or 'text'}|{_content_hash_for_uid(content)}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _export_filename(display_name, is_group, username=None):
    prefix = "group" if is_group else "single"
    label = display_name or username or "unknown"
    return f"{_safe_export_filename_part(f'{prefix}_{label}')}.json"


def _collision_export_filename(filename, username, suffix=None):
    stem, ext = os.path.splitext(filename)
    user_part = _safe_export_filename_part(username)
    extra = f"__{suffix}" if suffix else ""
    return f"{stem}__{user_part}{extra}{ext or '.json'}"


def _safe_index_filename(filename):
    filename = str(filename or "")
    if not filename:
        return ""
    if filename != os.path.basename(filename):
        return ""
    if filename == EXPORT_INDEX_FILE:
        return ""
    return filename


def _read_json_string_field(prefix, field):
    pattern = rf'"{re.escape(field)}"\s*:\s*("(?:(?:\\.)|[^"\\])*")'
    match = re.search(pattern, prefix)
    if not match:
        return ""
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ""
    return value if isinstance(value, str) else ""


def _read_export_file_identity(path):
    """只读取 JSON 文件头部，避免为建索引加载巨大 messages 数组。"""
    try:
        with open(path, encoding="utf-8") as f:
            prefix = f.read(256 * 1024)
    except OSError:
        return {}

    username = _read_json_string_field(prefix, "username")
    if not username:
        return {}
    return {
        "username": username,
        "chat": _read_json_string_field(prefix, "chat"),
        "is_group": bool(re.search(r'"is_group"\s*:\s*true', prefix)),
        "exported_at": _read_json_string_field(prefix, "exported_at"),
        "date_first_msg": _read_json_string_field(prefix, "date_first_msg"),
        "date_last_msg": _read_json_string_field(prefix, "date_last_msg"),
    }


def _file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return -1


def _index_entry_from_identity(filename, identity):
    return {
        "username": identity["username"],
        "is_group": bool(identity.get("is_group")),
        "current_chat_name": identity.get("chat", ""),
        "current_file": filename,
        "previous_files": [],
        "last_exported_at": identity.get("exported_at", ""),
        "date_first_msg": identity.get("date_first_msg", ""),
        "date_last_msg": identity.get("date_last_msg", ""),
    }


def _bootstrap_export_index(output_dir):
    index = _empty_export_index()
    if not os.path.isdir(output_dir):
        return index

    for filename in os.listdir(output_dir):
        safe_filename = _safe_index_filename(filename)
        if not safe_filename or not safe_filename.lower().endswith(".json"):
            continue
        path = os.path.join(output_dir, safe_filename)
        if not os.path.isfile(path):
            continue
        identity = _read_export_file_identity(path)
        username = identity.get("username")
        if not username:
            continue

        chats = index["chats"]
        entry = chats.get(username)
        if entry is None:
            chats[username] = _index_entry_from_identity(safe_filename, identity)
            continue

        previous = set(entry.get("previous_files") or [])
        current_file = entry.get("current_file")
        current_path = os.path.join(output_dir, current_file)
        if _file_mtime(path) >= _file_mtime(current_path):
            if current_file:
                previous.add(current_file)
            entry.update(_index_entry_from_identity(safe_filename, identity))
        else:
            previous.add(safe_filename)
        entry["previous_files"] = sorted(
            f for f in previous
            if _safe_index_filename(f) and f != entry.get("current_file")
        )

    return index


def _normalize_export_index(data):
    if not isinstance(data, dict) or not isinstance(data.get("chats"), dict):
        return None

    index = _empty_export_index()
    for username, entry in data["chats"].items():
        if not username or not isinstance(entry, dict):
            continue
        current_file = _safe_index_filename(entry.get("current_file"))
        if not current_file:
            continue
        previous = []
        for item in entry.get("previous_files") or []:
            filename = _safe_index_filename(item)
            if filename and filename != current_file and filename not in previous:
                previous.append(filename)
        index["chats"][str(username)] = {
            "username": str(username),
            "is_group": bool(entry.get("is_group")),
            "current_chat_name": entry.get("current_chat_name", ""),
            "current_file": current_file,
            "previous_files": previous,
            "last_exported_at": entry.get("last_exported_at", ""),
            "date_first_msg": entry.get("date_first_msg", ""),
            "date_last_msg": entry.get("date_last_msg", ""),
        }
    return index


def _load_export_index(output_dir):
    path = _export_index_path(output_dir)
    if not os.path.isfile(path):
        return _bootstrap_export_index(output_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _bootstrap_export_index(output_dir)
    return _normalize_export_index(data) or _bootstrap_export_index(output_dir)


def _write_export_index(output_dir, index):
    os.makedirs(output_dir or ".", exist_ok=True)
    path = _export_index_path(output_dir)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _choose_export_filename(output_dir, desired_filename, username):
    desired_path = os.path.join(output_dir, desired_filename)
    if not os.path.exists(desired_path):
        return desired_filename
    if _read_export_file_identity(desired_path).get("username") == username:
        return desired_filename

    for counter in range(1, 1000):
        suffix = None if counter == 1 else str(counter)
        candidate = _collision_export_filename(desired_filename, username, suffix)
        candidate_path = os.path.join(output_dir, candidate)
        if not os.path.exists(candidate_path):
            return candidate
        if _read_export_file_identity(candidate_path).get("username") == username:
            return candidate
    raise RuntimeError(f"无法为 {username} 生成不冲突的导出文件名")


_FILE_MSG_PREFIX = "[文件] "

# ── 未下载/失败媒体台账 _pending_media.json ───────────────────────
# 精确记录"哪条消息的哪个媒体本地没有",交给后续(下次 sync / --retry-media)补抓。
PENDING_MEDIA_FILE = "_pending_media.json"


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pending_media_path(output_root=None):
    return os.path.join(output_root or output_layout.output_root(), PENDING_MEDIA_FILE)


# ── 并发锁 .sync.lock（防定时任务与手动同步同时写同一目录）─────────
def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def _acquire_sync_lock(output_root):
    """获取目录级同步锁。返回锁路径；若另一活进程已持有则返回 None。
    可重入(同 pid)，并能接管僵尸锁(持锁进程已退出)。"""
    path = os.path.join(output_root, ".sync.lock")
    if os.path.exists(path):
        try:
            with open(path) as f:
                pid = json.load(f).get("pid")
        except Exception:
            pid = None
        if pid == os.getpid():
            return path                       # 本进程已持有
        if isinstance(pid, int) and _pid_alive(pid):
            return None                       # 别的活进程持有 → 拒绝
        # 否则视为僵尸锁，接管
    try:
        os.makedirs(output_root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "at": _now_str()}, f)
    except OSError:
        return None
    return path


def _release_sync_lock(output_root):
    path = os.path.join(output_root, ".sync.lock")
    try:
        with open(path) as f:
            if json.load(f).get("pid") == os.getpid():
                os.remove(path)
    except Exception:
        pass


def _load_pending_media(output_root=None):
    try:
        with open(_pending_media_path(output_root), encoding="utf-8") as f:
            d = json.load(f)
        return d.get("pending", []) if isinstance(d, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_pending_media(entries, output_root=None):
    p = _pending_media_path(output_root)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "pending": entries}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _pending_key(e):
    return (e.get("chat"), e.get("local_id"), e.get("kind"))


def _merge_pending(output_root, username, new_pending, resolved_local_ids):
    """把本聊天本次新发现的缺失(new_pending)并入台账；同时移除本次已成功处理(resolved)的旧条目。
    new_pending: [{chat,local_id,kind,ref,target,reason}, ...]（不含时间，本函数补）。
    resolved_local_ids: set，本聊天本次已成功落地的 (local_id, kind)。"""
    entries = _load_pending_media(output_root)
    by_key = {_pending_key(e): e for e in entries}
    now = _now_str()
    # 移除已解决
    for lid, kind in resolved_local_ids:
        by_key.pop((username, lid, kind), None)
    # 并入新缺失（已存在则累加 attempts）
    for np in new_pending:
        k = _pending_key(np)
        old = by_key.get(k)
        if old:
            old["last_checked_at"] = now
            old["attempts"] = old.get("attempts", 1) + 1
            old["reason"] = np.get("reason", old.get("reason"))
        else:
            np["first_seen_at"] = now
            np["last_checked_at"] = now
            np["attempts"] = 1
            by_key[k] = np
    _save_pending_media(list(by_key.values()), output_root)


def _patch_chat_json(path, lid_to_content):
    """把某聊天 chat.json 中指定 local_id 的消息 content 改写。
    lid_to_content[lid]=新content；值为 None 表示只去掉"（未下载…）"标记。"""
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    changed = False
    for m in data.get("messages", []):
        new = lid_to_content.get(m.get("local_id"), False)
        if new is False:
            continue
        if new:
            m["content"] = new
            changed = True
        else:
            c = m.get("content") or ""
            i = c.find("（未下载")
            if i >= 0:
                m["content"] = c[:i].rstrip() or c
                changed = True
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def _retry_pending_media(output_root, only_username=None, describe=False):
    """补抓：重扫台账，对"现在本地已有"的媒体落地（微信后来下载到的），并回补 chat.json、
    从台账移除；仍缺的累加 attempts。返回 {recovered, still_missing}。"""
    entries = _load_pending_media(output_root)
    if not entries:
        return {"recovered": 0, "still_missing": 0}
    index = _load_export_index(output_root)
    chats_meta = index.get("chats", {})
    # 显示名用实时联系人映射（与导出一致），保证补抓落到正确的聊天文件夹；
    # chat.json 路径优先用索引 current_file。
    names = mcp_server.get_contact_names()
    fmd5 = _wechat_file_md5_index()
    findex = _wechat_file_index()
    vindex = _wechat_video_index()
    do_desc = describe and _resolve_image_backend() != "none"
    if do_desc:
        # image_describe 是可选扩展（图片/视频 AI 识别），本发布版本不包含。
        # 缺失时降级为「不识别」，导出其余部分照常进行。
        try:
            import image_describe
        except ImportError:
            do_desc = False
    recovered = 0
    remaining = []
    patches = {}            # username -> {local_id: 新content 或 None(去标)}
    used_names = {}         # username -> 该聊天 files/ 已用名
    now = _now_str()
    for e in entries:
        chat = e.get("chat")
        if only_username and chat != only_username:
            remaining.append(e)
            continue
        display = names.get(chat, chat)  # 与导出同源，确保文件夹名一致
        lid, kind, ref = e.get("local_id"), e.get("kind"), e.get("ref", {})
        ok = False
        try:
            if kind == "video":
                src = vindex.get(ref.get("md5"))
                if src:
                    dd = output_layout.chat_subdir(chat, display, output_layout.SUBDIR_VIDEO)
                    _copy_writable(src, os.path.join(dd, f"video_{lid}.mp4"))
                    cover = os.path.splitext(src)[0] + ".jpg"
                    if os.path.isfile(cover):
                        _copy_writable(cover, os.path.join(dd, f"video_{lid}.jpg"))
                    ok = True
            elif kind == "file":
                md5, fname = ref.get("md5"), ref.get("filename")
                cands = (fmd5.get(md5) if md5 else None) or (findex.get(fname) if fname else None)
                if cands:
                    dd = output_layout.chat_subdir(chat, display, output_layout.SUBDIR_FILES)
                    used = used_names.setdefault(
                        chat, set(os.listdir(dd) if os.path.isdir(dd) else []))
                    dest = _dedupe_dest_name(fname or os.path.basename(cands[0]), used)
                    _copy_writable(cands[0], os.path.join(dd, dest))
                    used.add(dest)
                    ok = True
            elif kind == "image":
                result = mcp_server._image_resolver.decode_image(chat, lid)
                if result.get("success"):
                    dd = output_layout.chat_subdir(chat, display, output_layout.SUBDIR_IMAGES)
                    ext = result.get("format") or "jpg"
                    _copy_writable(result["path"], os.path.join(dd, f"image_{lid}.{ext}"))
                    if do_desc:
                        try:
                            import image_describe
                            if result.get("only_thumbnail"):
                                patches.setdefault(chat, {})[lid] = (
                                    "[图片]（本地仅有微信缩略图，未下载原图，无法分析图片内容）")
                            else:
                                d = image_describe.describe_image_file(
                                    result["path"], image_md5=result.get("md5"))
                                if d:
                                    patches.setdefault(chat, {})[lid] = f"[图片] {d}"
                        except Exception:
                            pass
                    ok = True
        except Exception:
            ok = False
        if ok:
            recovered += 1
            patches.setdefault(chat, {}).setdefault(lid, None)  # 至少去掉"未下载"标
        else:
            e["last_checked_at"] = now
            e["attempts"] = e.get("attempts", 1) + 1
            remaining.append(e)
    for chat, lid_map in patches.items():
        cf = chats_meta.get(chat, {}).get("current_file")
        if cf:
            path = os.path.join(output_root, cf)
        else:  # 索引缺 current_file → 按聊天文件夹推导
            path = os.path.join(
                output_layout.chat_dir(chat, names.get(chat, chat), create=False, root=output_root),
                output_layout.CHAT_RECORD_NAME)
        _patch_chat_json(path, lid_map)
    _save_pending_media(remaining, output_root)
    return {"recovered": recovered, "still_missing": len(remaining)}


_wechat_file_index_cache = None


def _copy_writable(src, dest):
    """copy2 但确保目标可写。微信源文件常是 0444 只读，copy2 会把只读权限带到目标，
    导致下次重导覆盖时 PermissionError 被静默吞掉（复制 0 个）。复制前后都 chmod +w。"""
    if os.path.exists(dest):
        try:
            os.chmod(dest, 0o644)
        except OSError:
            pass
    shutil.copy2(src, dest)
    try:
        os.chmod(dest, 0o644)
    except OSError:
        pass


def _wechat_file_index():
    """一次性扫描微信 msg/file 目录，建 {文件名: [磁盘路径,...]}。聊天文件是明文存储，
    只按年月分目录、不按聊天分，所以靠文件名把"文件消息"映射回磁盘文件。"""
    global _wechat_file_index_cache
    if _wechat_file_index_cache is not None:
        return _wechat_file_index_cache
    from config import load_config
    cfg = load_config()
    base = cfg.get("wechat_base_dir") or os.path.dirname(cfg.get("db_dir", ""))
    file_root = os.path.join(base, "msg", "file")
    index = {}
    if os.path.isdir(file_root):
        for root, _dirs, files in os.walk(file_root):
            for fn in files:
                if fn == ".DS_Store":
                    continue
                index.setdefault(fn, []).append(os.path.join(root, fn))
    _wechat_file_index_cache = index
    return index


_wechat_file_md5_index_cache = None


def _wechat_file_md5_index():
    """{md5: [磁盘路径,...]}。文件名可能撞车(微信存成 name 与 name(1))，靠内容 md5
    才能把"文件消息"精确映射到物理文件、并区分同名不同内容。"""
    global _wechat_file_md5_index_cache
    if _wechat_file_md5_index_cache is not None:
        return _wechat_file_md5_index_cache
    import hashlib
    idx = {}
    for _name, paths in _wechat_file_index().items():
        for p in paths:
            try:
                with open(p, "rb") as f:
                    md5 = hashlib.md5(f.read()).hexdigest()
            except OSError:
                continue
            idx.setdefault(md5, []).append(p)
    _wechat_file_md5_index_cache = idx
    return idx


def _dedupe_dest_name(name, used):
    """仿微信：目标目录同名冲突时加 ' (1)'、' (2)'…，避免覆盖丢数据。"""
    if name not in used:
        return name
    stem, ext = os.path.splitext(name)
    i = 1
    while f"{stem} ({i}){ext}" in used:
        i += 1
    return f"{stem} ({i}){ext}"


def _print_media_summary(display_name, summary):
    """打印单聊天的媒体提取汇总：检测/成功/失败/失败原因。无媒体则不打印。"""
    def _has(s):
        det = s.get("检测")
        return (isinstance(det, int) and det > 0) or bool(s.get("成功"))

    if not any(_has(s) for s in summary.values()):
        return
    lines = []
    for kind in ("语音", "图片", "文件", "视频", "合并转发"):
        s = summary.get(kind)
        if not s or not _has(s):
            continue
        if kind == "合并转发":
            lines.append(f"     合并转发: {s['成功']} {s.get('失败原因', '')}".rstrip())
            continue
        if kind == "图片":
            ok = f"保存 {s.get('_saved', 0)} / 描述 {s.get('_described', 0)}"
        else:
            ok = f"成功 {s.get('成功', 0)}"
        fail = s.get("失败", 0)
        tail = ""
        if fail:
            tail = f" | 失败 {fail}"
            if s.get("失败原因"):
                tail += f"（{s['失败原因']}）"
        lines.append(f"     {kind}: 检测 {s.get('检测')} | {ok}{tail}")
    if lines:
        print(f"   ── 媒体汇总：{display_name} ──")
        for ln in lines:
            print(ln)


def _extract_forwarded_content(username, display_name, messages, record_items, describe):
    """尽力而为：把合并转发里**已下载到本地 Rec/ 目录**的文件/图片提取出来。
    文件(明文)→ files/；图片(加密.dat)→ 解密存 images/ 并可选 VLM 描述、把描述贴回该
    合并转发消息的 content。按 fullmd5 精确匹配 recorditem 条目。
    懒下载约束：没点开过的转发内容本地没有，无法提取。返回 {files, images}。"""
    if not record_items:
        return {}
    import hashlib
    import decode_image
    from config import load_config
    cfg = load_config()
    base = cfg.get("wechat_base_dir") or os.path.dirname(cfg.get("db_dir", ""))
    h = hashlib.md5(username.encode("utf-8")).hexdigest()
    rec_dirs = glob.glob(os.path.join(base, "msg", "attach", h, "*", "Rec", "*"))
    if not rec_dirs:
        return {}
    aes = cfg.get("image_aes_key")
    xor = cfg.get("image_xor_key", 0x88)

    file_idx = {}   # md5 -> 明文文件路径
    img_idx = {}    # md5 -> (解密后字节, fmt)
    for rd in rec_dirs:
        for f in glob.glob(os.path.join(rd, "F", "*", "*")):
            if os.path.isfile(f) and os.path.basename(f) != ".DS_Store":
                try:
                    file_idx.setdefault(
                        hashlib.md5(open(f, "rb").read()).hexdigest(), f
                    )
                except OSError:
                    pass
        for f in glob.glob(os.path.join(rd, "Img", "*")):
            b = os.path.basename(f)
            if b.endswith("_t") or b == ".DS_Store" or not os.path.isfile(f):
                continue
            try:
                out, fmt = decode_image.decrypt_dat_file(
                    f, f"/tmp/_fwd_{h}_{b}.tmp", aes, xor
                )
                if out:
                    data = open(out, "rb").read()
                    os.remove(out)
                    img_idx.setdefault(hashlib.md5(data).hexdigest(), (data, fmt))
            except Exception:
                pass
    if not file_idx and not img_idx:
        return {}

    files_dir = images_dir = None
    n_files = n_imgs = 0
    for msg in messages:
        items = record_items.get(msg.get("local_id"))
        if not items:
            continue
        desc_lines = []
        for it in items:
            md5 = it.get("fullmd5")
            if not md5:
                continue
            if md5 in file_idx:
                if files_dir is None:
                    files_dir = output_layout.chat_subdir(
                        username, display_name, output_layout.SUBDIR_FILES
                    )
                name = it.get("datatitle") or os.path.basename(file_idx[md5])
                try:
                    _copy_writable(file_idx[md5], os.path.join(files_dir, name))
                    n_files += 1
                except OSError:
                    pass
            elif md5 in img_idx:
                data, fmt = img_idx[md5]
                if images_dir is None:
                    images_dir = output_layout.chat_subdir(
                        username, display_name, output_layout.SUBDIR_IMAGES
                    )
                imgpath = os.path.join(images_dir, f"forward_{md5[:12]}.{fmt}")
                try:
                    with open(imgpath, "wb") as w:
                        w.write(data)
                    n_imgs += 1
                except OSError:
                    continue
                if describe:
                    try:
                        import image_describe
                        # 缩略图描述已在 describe_image_file 内自动跳过（返回 None）
                        d = image_describe.describe_image_file(imgpath)
                        if d:
                            desc_lines.append(f"  [转发图片] {d}")
                    except Exception:
                        pass
        if desc_lines and msg.get("content"):
            msg["content"] = msg["content"] + "\n" + "\n".join(desc_lines)
    return {"files": n_files, "images": n_imgs}


_wechat_video_index_cache = None


def _wechat_video_index():
    """扫描 msg/video，建 {内容md5: mp4路径}。
    注意：视频文件名是 packed_info 里的存储 hash（如 6fec2f…），**不等于** videomsg 的
    <md5>；但 mp4 的**内容 md5 == videomsg <md5>**（实测），所以按内容 md5 建索引才能匹配。
    只有播放/下载过的视频才在本地。"""
    global _wechat_video_index_cache
    if _wechat_video_index_cache is not None:
        return _wechat_video_index_cache
    import hashlib
    from config import load_config
    cfg = load_config()
    base = cfg.get("wechat_base_dir") or os.path.dirname(cfg.get("db_dir", ""))
    video_root = os.path.join(base, "msg", "video")
    index = {}
    if os.path.isdir(video_root):
        for root, _dirs, files in os.walk(video_root):
            for fn in files:
                if fn.lower().endswith(".mp4"):
                    p = os.path.join(root, fn)
                    try:
                        with open(p, "rb") as f:
                            index[hashlib.md5(f.read()).hexdigest()] = p
                    except OSError:
                        continue
    _wechat_video_index_cache = index
    return index


def _extract_chat_videos(username, display_name, messages, video_md5s):
    """把本聊天已下载的视频(明文 mp4)复制到 <chat>/video/，命名 video_<local_id>.mp4
    （连封面 .jpg 一起）。按 videomsg <md5> = mp4 内容 md5 匹配。
    video_md5s: {local_id: [md5,...]}。返回 (复制数, pending列表, resolved集合)。"""
    if not video_md5s:
        return 0, [], set()
    vindex = _wechat_video_index()
    dest_dir = None
    copied = 0
    pending = []
    resolved = set()
    for msg in messages:
        if msg.get("type") != "video":
            continue
        lid = msg.get("local_id")
        md5s = video_md5s.get(lid)
        if not md5s:
            continue
        src = next((vindex[h] for h in md5s if h in vindex), None)
        if not src:
            # 视频没播放过 → 本地无 mp4 → 精确记账
            pending.append({"chat": username, "local_id": lid, "kind": "video",
                            "ref": {"md5": md5s[0]}, "target": f"video/video_{lid}.mp4",
                            "reason": "not_downloaded"})
            continue
        if dest_dir is None:
            dest_dir = output_layout.chat_subdir(
                username, display_name, output_layout.SUBDIR_VIDEO
            )
        try:
            _copy_writable(src, os.path.join(dest_dir, f"video_{lid}.mp4"))
            cover = os.path.splitext(src)[0] + ".jpg"  # 同名封面图
            if os.path.isfile(cover):
                _copy_writable(cover, os.path.join(dest_dir, f"video_{lid}.jpg"))
            copied += 1
            resolved.add((lid, "video"))
        except OSError:
            pass
    return copied, pending, resolved


def _extract_chat_files(username, display_name, messages, file_md5s):
    """把本聊天的文件（明文）复制到 <chat>/files/。**一条文件消息存一份**，
    优先按消息的 <md5> 精确匹配物理文件（能区分同名不同内容、并找到微信的 (1) 变体），
    回退按文件名。目标重名时仿微信加 ' (1)'。返回 (复制数, pending列表, resolved集合)。"""
    file_index = _wechat_file_index()
    md5_index = _wechat_file_md5_index() if file_index else {}
    dest_dir = None
    used_names = set()   # 目标目录已用名（冲突加 (1)）
    used_srcs = set()    # 已用物理源（同 md5 多份分别取）
    copied = 0
    pending = []
    resolved = set()
    for msg in messages:
        c = msg.get("content", "")
        if msg.get("type") != "link_or_file" or not c.startswith(_FILE_MSG_PREFIX):
            continue
        fname = c[len(_FILE_MSG_PREFIX):].strip()
        if not fname:
            continue
        lid = msg.get("local_id")
        # 选物理源：md5 优先（精确），否则文件名
        want = file_md5s.get(lid)
        cands = md5_index.get(want) if want else None
        if not cands:
            cands = file_index.get(fname)
        if not cands:
            # 本地没有（未下载）→ 精确记账
            pending.append({"chat": username, "local_id": lid, "kind": "file",
                            "ref": {"md5": want, "filename": fname},
                            "target": f"files/{fname}", "reason": "not_downloaded"})
            continue
        src = next((p for p in cands if p not in used_srcs), cands[0])
        if dest_dir is None:
            dest_dir = output_layout.chat_subdir(
                username, display_name, output_layout.SUBDIR_FILES
            )
            # 增量时目标目录可能已有上次存的文件 → 纳入去重，避免新同名文件覆盖旧的丢数据
            try:
                used_names.update(os.listdir(dest_dir))
            except OSError:
                pass
        dest_name = _dedupe_dest_name(fname, used_names)
        try:
            _copy_writable(src, os.path.join(dest_dir, dest_name))
            used_names.add(dest_name)
            used_srcs.add(src)
            copied += 1
            resolved.add((lid, "file"))
        except OSError:
            pending.append({"chat": username, "local_id": lid, "kind": "file",
                            "ref": {"md5": want, "filename": fname},
                            "target": f"files/{fname}", "reason": "copy_failed"})
    return copied, pending, resolved


def _rename_stale_chat_folder(output_dir, username, new_folder):
    """该 username 的旧聊天文件夹（备注变更前的名字）若与 new_folder 不同，整体改名过去。
    聊天文件夹名恒以 -<sanitized_username> 结尾，据此定位，无需依赖索引。"""
    if os.path.exists(new_folder):
        return
    suffix = "-" + output_layout._sanitize(username, max_len=120)
    new_base = os.path.basename(new_folder)
    try:
        names = os.listdir(output_dir)
    except OSError:
        return
    for name in names:
        if name == new_base or not name.endswith(suffix):
            continue
        old = os.path.join(output_dir, name)
        if os.path.isdir(old):
            try:
                os.rename(old, new_folder)
            except OSError:
                pass
            return


def _resolve_indexed_export_path(output_dir, username, display_name, is_group):
    """聊天记录写到 output_dir 下「每聊天文件夹」的 chat.json。文件夹名含 username 天然
    唯一，不再需要旧的扁平防重名逻辑。若显示名(备注)变了导致文件夹名变化，则把旧文件夹整体
    重命名过去，保住该聊天已累积的 images/audio/chat.json（增量历史）。"""
    os.makedirs(output_dir or ".", exist_ok=True)
    index = _load_export_index(output_dir)
    chats = index.setdefault("chats", {})
    # create_dir=False：先不要建新文件夹，否则下面"新文件夹不存在才改名"会失效
    out_path = output_layout.chat_record_path(
        username, display_name, root=output_dir, create_dir=False
    )
    new_folder = os.path.dirname(out_path)
    # 显示名(备注)变化 → 文件夹名变化时，把该 username 的旧文件夹整体改名过来，
    # 保住已累积的 chat.json / images / audio（增量历史）。按 -username 后缀定位，不依赖索引。
    _rename_stale_chat_folder(output_dir, username, new_folder)

    entry = chats.get(username) or {}
    chats[username] = {
        "username": username,
        "is_group": bool(is_group),
        "current_chat_name": display_name,
        "current_file": os.path.relpath(out_path, output_dir),
        "previous_files": [],
        "last_exported_at": entry.get("last_exported_at", ""),
        "date_first_msg": entry.get("date_first_msg", ""),
        "date_last_msg": entry.get("date_last_msg", ""),
    }
    return out_path, index


def _update_export_index(output_dir, index, username, display_name, is_group,
                         out_path, output):
    # current_file 存「相对 output_dir 的路径」，即 <聊天文件夹>/chat.json
    rel = os.path.relpath(out_path, output_dir)
    chats = index.setdefault("chats", {})
    chats[username] = {
        "username": username,
        "is_group": bool(is_group),
        "current_chat_name": display_name,
        "current_file": rel,
        "previous_files": [],
        "last_exported_at": output.get("exported_at", ""),
        "date_first_msg": output.get("date_first_msg", ""),
        "date_last_msg": output.get("date_last_msg", ""),
    }
    _write_export_index(output_dir, index)


def _load_session_usernames(session_db):
    """读取 SessionTable 中的会话 username，保持数据库原始顺序。"""
    with closing(sqlite3.connect(session_db)) as conn:
        return [
            u for u, _ in conn.execute(
                "SELECT username, type FROM SessionTable"
            )
        ]


def _build_chat_rows(sessions, names, contact_full=None):
    """构建可展示、可选择的会话行。"""
    contact_meta = {
        item.get("username"): item
        for item in (contact_full or [])
        if item.get("username")
    }
    rows = []
    for index, username in enumerate(sessions, 1):
        display_name = names.get(username, username)
        kind = "group" if str(username).endswith("@chatroom") else "single"
        meta = contact_meta.get(username, {})
        rows.append({
            "index": index,
            "username": username,
            "display_name": display_name,
            "kind": kind,
            "remark": meta.get("remark", ""),
            "nick_name": meta.get("nick_name", ""),
        })
    return rows


def _format_plan_time(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _date_from_message_ts(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _contact_metadata_for_export(username, is_group=False):
    if is_group:
        return {}
    contact = {}
    for item in mcp_server.get_contact_full():
        if item.get("username") == username:
            contact = item
            break
    try:
        tag_map = mcp_server.get_contact_tag_names_by_username()
    except Exception:
        tag_map = {}
    return {
        "contact_remark": contact.get("remark", ""),
        "contact_nick_name": contact.get("nick_name", ""),
        "contact_tags": tag_map.get(username, []),
        "contact_memo": contact.get("description", ""),
    }


def _where_for_time_range(start_ts=None, end_ts=None, column="create_time"):
    clauses = []
    params = []
    if start_ts is not None:
        clauses.append(f"{column} >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append(f"{column} <= ?")
        params.append(end_ts)
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params


def _query_message_table_plan_stats(db_path, table_name, start_ts=None, end_ts=None):
    if not mcp_server._is_safe_msg_table_name(table_name):
        raise ValueError(f"非法消息表名: {table_name}")
    where_sql, params = _where_for_time_range(start_ts, end_ts)
    sql = f"""
        SELECT COUNT(*), MIN(create_time), MAX(create_time),
               COALESCE(SUM(
                   COALESCE(length(message_content), 0)
                   + COALESCE(length(compress_content), 0)
                   + COALESCE(length(packed_info_data), 0)
               ), 0)
        FROM [{table_name}]
        {where_sql}
    """
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(sql, params).fetchone()


def _get_message_resource_db_path():
    try:
        path = mcp_server._cache.get("message/message_resource.db")
    except Exception:
        path = None
    candidates = [
        path,
        os.path.join(mcp_server.DECRYPTED_DIR, "message", "message_resource.db"),
        os.path.join(
            mcp_server.DECRYPTED_DIR,
            "_monitor_cache",
            "message_message_resource.db",
        ),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _query_resource_estimated_bytes(username, start_ts=None, end_ts=None):
    resource_db = _get_message_resource_db_path()
    if not resource_db:
        return 0, "resource_missing"
    with closing(sqlite3.connect(resource_db)) as conn:
        chat_row = conn.execute(
            "SELECT rowid FROM ChatName2Id WHERE user_name = ?",
            (username,),
        ).fetchone()
        if not chat_row:
            return 0, None
        clauses = ["i.chat_id = ?"]
        params = [chat_row[0]]
        if start_ts is not None:
            clauses.append("i.message_create_time >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("i.message_create_time <= ?")
            params.append(end_ts)
        where_sql = " AND ".join(clauses)
        row = conn.execute(f"""
            SELECT COALESCE(SUM(COALESCE(d.size, 0)), 0)
            FROM MessageResourceInfo i
            LEFT JOIN MessageResourceDetail d ON d.message_id = i.message_id
            WHERE {where_sql}
        """, params).fetchone()
    return int(row[0] or 0), None


def _query_voice_estimated_bytes(username, start_ts=None, end_ts=None):
    try:
        media_paths = list(mcp_server._iter_media_db_paths())
    except Exception:
        return 0, "media_error"
    if not media_paths:
        return 0, "media_missing"

    total = 0
    for media_db in media_paths:
        try:
            with closing(sqlite3.connect(media_db)) as conn:
                chat_name_id = mcp_server._get_chat_name_id(conn, username)
                if chat_name_id is None:
                    continue
                clauses = ["chat_name_id = ?"]
                params = [chat_name_id]
                if start_ts is not None:
                    clauses.append("create_time >= ?")
                    params.append(start_ts)
                if end_ts is not None:
                    clauses.append("create_time <= ?")
                    params.append(end_ts)
                row = conn.execute(f"""
                    SELECT COALESCE(SUM(COALESCE(length(voice_data), 0)), 0)
                    FROM VoiceInfo
                    WHERE {" AND ".join(clauses)}
                """, params).fetchone()
                total += int(row[0] or 0)
        except sqlite3.Error:
            return total, "media_error"
    return total, None


def _scan_dir_bytes(path):
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
    return total


def _scan_local_attachment_bytes(username):
    base = getattr(mcp_server, "WECHAT_BASE_DIR", "")
    if not base:
        return 0, "scan_base_missing"

    username_hash = hashlib.md5(username.encode()).hexdigest()
    msg_dir = os.path.join(base, "msg")
    roots = [
        os.path.join(msg_dir, "attach", username_hash),
        os.path.join(msg_dir, "file", username_hash),
        os.path.join(msg_dir, "video", username_hash),
    ]
    total = sum(_scan_dir_bytes(path) for path in roots)

    global_roots = [
        os.path.join(msg_dir, "file"),
        os.path.join(msg_dir, "video"),
    ]
    has_unattributed_global_dirs = any(
        os.path.isdir(path) and not os.path.isdir(os.path.join(path, username_hash))
        for path in global_roots
    )
    if has_unattributed_global_dirs:
        return total, "scan_limited"
    return total, None


def _collect_chat_plan_stats(username, message_tables, start_ts=None, end_ts=None,
                             size_mode="estimate"):
    status = []
    message_count = 0
    message_body_bytes = 0
    first_ts = None
    last_ts = None

    if not message_tables:
        status.append("no_message_table")

    for table in message_tables:
        try:
            count, min_ts, max_ts, body_bytes = _query_message_table_plan_stats(
                table["db_path"],
                table["table_name"],
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except (sqlite3.Error, ValueError):
            status.append("message_error")
            continue
        message_count += int(count or 0)
        message_body_bytes += int(body_bytes or 0)
        if min_ts:
            first_ts = min_ts if first_ts is None else min(first_ts, min_ts)
        if max_ts:
            last_ts = max_ts if last_ts is None else max(last_ts, max_ts)

    try:
        resource_bytes, resource_status = _query_resource_estimated_bytes(
            username, start_ts=start_ts, end_ts=end_ts
        )
    except sqlite3.Error:
        resource_bytes, resource_status = 0, "resource_error"
    if resource_status:
        status.append(resource_status)

    voice_bytes, voice_status = _query_voice_estimated_bytes(
        username, start_ts=start_ts, end_ts=end_ts
    )
    if voice_status:
        status.append(voice_status)

    scanned_bytes = ""
    if size_mode == "scan":
        scanned_bytes, scan_status = _scan_local_attachment_bytes(username)
        if scan_status:
            status.append(scan_status)

    attachment_estimated = int(resource_bytes or 0) + int(voice_bytes or 0)
    size_status = "ok" if not status else "partial:" + ",".join(sorted(set(status)))
    return {
        "message_count": message_count,
        "message_body_bytes": message_body_bytes,
        "first_time": _format_plan_time(first_ts),
        "last_time": _format_plan_time(last_ts),
        "attachment_estimated_bytes": attachment_estimated,
        "attachment_scanned_bytes": scanned_bytes,
        "total_estimated_bytes": message_body_bytes + attachment_estimated,
        "size_status": size_status,
    }


def _new_plan_accumulator():
    return {
        "message_count": 0,
        "message_body_bytes": 0,
        "first_ts": None,
        "last_ts": None,
        "attachment_estimated_bytes": 0,
        "attachment_scanned_bytes": "",
        "statuses": set(),
    }


def _message_table_name_for_username(username):
    table_hash = hashlib.md5(username.encode()).hexdigest()
    return f"Msg_{table_hash}"


def _iter_message_db_paths():
    for rel_key in getattr(mcp_server, "MSG_DB_KEYS", []):
        try:
            path = mcp_server._cache.get(rel_key)
        except Exception:
            path = None
        if path:
            yield path


def _fetch_existing_message_tables(conn, table_names):
    if not table_names:
        return set()
    existing = set()
    batch_size = 500
    table_names = list(table_names)
    for i in range(0, len(table_names), batch_size):
        batch = table_names[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            f"WHERE type='table' AND name IN ({placeholders})",
            batch,
        ).fetchall()
        existing.update(row[0] for row in rows)
    return existing


def _query_message_table_plan_stats_conn(conn, table_name, start_ts=None, end_ts=None):
    if not mcp_server._is_safe_msg_table_name(table_name):
        raise ValueError(f"非法消息表名: {table_name}")
    where_sql, params = _where_for_time_range(start_ts, end_ts)
    sql = f"""
        SELECT COUNT(*), MIN(create_time), MAX(create_time),
               COALESCE(SUM(
                   COALESCE(length(message_content), 0)
                   + COALESCE(length(compress_content), 0)
                   + COALESCE(length(packed_info_data), 0)
               ), 0)
        FROM [{table_name}]
        {where_sql}
    """
    return conn.execute(sql, params).fetchone()


def _collect_message_stats_batch(usernames, start_ts=None, end_ts=None):
    table_to_username = {
        _message_table_name_for_username(username): username
        for username in usernames
    }
    stats = {username: _new_plan_accumulator() for username in usernames}
    found = set()
    db_paths = list(_iter_message_db_paths())
    if not db_paths:
        for username in usernames:
            stats[username]["statuses"].add("message_db_missing")
        return stats

    for db_path in db_paths:
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                existing = _fetch_existing_message_tables(conn, table_to_username)
                for table_name in existing:
                    username = table_to_username[table_name]
                    try:
                        count, min_ts, max_ts, body_bytes = (
                            _query_message_table_plan_stats_conn(
                                conn,
                                table_name,
                                start_ts=start_ts,
                                end_ts=end_ts,
                            )
                        )
                    except (sqlite3.Error, ValueError):
                        stats[username]["statuses"].add("message_error")
                        continue
                    found.add(username)
                    stats[username]["message_count"] += int(count or 0)
                    stats[username]["message_body_bytes"] += int(body_bytes or 0)
                    if min_ts:
                        current = stats[username]["first_ts"]
                        stats[username]["first_ts"] = (
                            min_ts if current is None else min(current, min_ts)
                        )
                    if max_ts:
                        current = stats[username]["last_ts"]
                        stats[username]["last_ts"] = (
                            max_ts if current is None else max(current, max_ts)
                        )
        except sqlite3.Error:
            for username in usernames:
                stats[username]["statuses"].add("message_error")

    for username in usernames:
        if username not in found:
            stats[username]["statuses"].add("no_message_table")
    return stats


def _collect_resource_estimates_batch(usernames, start_ts=None, end_ts=None):
    values = {username: 0 for username in usernames}
    statuses = {username: set() for username in usernames}
    resource_db = _get_message_resource_db_path()
    if not resource_db:
        for username in usernames:
            statuses[username].add("resource_missing")
        return values, statuses

    try:
        with closing(sqlite3.connect(resource_db)) as conn:
            usernames = list(usernames)
            batch_size = 500
            for i in range(0, len(usernames), batch_size):
                batch = usernames[i:i + batch_size]
                placeholders = ",".join("?" for _ in batch)
                params = list(batch)
                clauses = [f"c.user_name IN ({placeholders})"]
                if start_ts is not None:
                    clauses.append("i.message_create_time >= ?")
                    params.append(start_ts)
                if end_ts is not None:
                    clauses.append("i.message_create_time <= ?")
                    params.append(end_ts)
                where_sql = " AND ".join(clauses)
                rows = conn.execute(f"""
                    SELECT c.user_name, COALESCE(SUM(COALESCE(d.size, 0)), 0)
                    FROM ChatName2Id c
                    JOIN MessageResourceInfo i ON i.chat_id = c.rowid
                    LEFT JOIN MessageResourceDetail d ON d.message_id = i.message_id
                    WHERE {where_sql}
                    GROUP BY c.user_name
                """, params).fetchall()
                for username, size in rows:
                    values[username] = int(size or 0)
    except sqlite3.Error:
        for username in usernames:
            statuses[username].add("resource_error")
    return values, statuses


def _collect_voice_estimates_batch(usernames, start_ts=None, end_ts=None):
    values = {username: 0 for username in usernames}
    statuses = {username: set() for username in usernames}
    try:
        media_paths = list(mcp_server._iter_media_db_paths())
    except Exception:
        for username in usernames:
            statuses[username].add("media_error")
        return values, statuses

    if not media_paths:
        for username in usernames:
            statuses[username].add("media_missing")
        return values, statuses

    usernames = list(usernames)
    try:
        for media_db in media_paths:
            with closing(sqlite3.connect(media_db)) as conn:
                batch_size = 500
                for i in range(0, len(usernames), batch_size):
                    batch = usernames[i:i + batch_size]
                    placeholders = ",".join("?" for _ in batch)
                    params = list(batch)
                    clauses = [f"n.user_name IN ({placeholders})"]
                    if start_ts is not None:
                        clauses.append("v.create_time >= ?")
                        params.append(start_ts)
                    if end_ts is not None:
                        clauses.append("v.create_time <= ?")
                        params.append(end_ts)
                    where_sql = " AND ".join(clauses)
                    rows = conn.execute(f"""
                        SELECT n.user_name,
                               COALESCE(SUM(COALESCE(length(v.voice_data), 0)), 0)
                        FROM Name2Id n
                        JOIN VoiceInfo v ON v.chat_name_id = n.rowid
                        WHERE {where_sql}
                        GROUP BY n.user_name
                    """, params).fetchall()
                    for username, size in rows:
                        values[username] += int(size or 0)
    except sqlite3.Error:
        for username in usernames:
            statuses[username].add("media_error")
    return values, statuses


def _finalize_plan_stats(acc):
    status = sorted(acc["statuses"])
    size_status = "ok" if not status else "partial:" + ",".join(status)
    return {
        "message_count": acc["message_count"],
        "message_body_bytes": acc["message_body_bytes"],
        "first_time": _format_plan_time(acc["first_ts"]),
        "last_time": _format_plan_time(acc["last_ts"]),
        "attachment_estimated_bytes": acc["attachment_estimated_bytes"],
        "attachment_scanned_bytes": acc["attachment_scanned_bytes"],
        "total_estimated_bytes": (
            acc["message_body_bytes"] + acc["attachment_estimated_bytes"]
        ),
        "size_status": size_status,
    }


def _collect_all_plan_stats(chat_rows, start_ts=None, end_ts=None,
                            size_mode="estimate"):
    usernames = [row["username"] for row in chat_rows]
    print("[*] 批量统计消息表...", flush=True)
    stats = _collect_message_stats_batch(
        usernames,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    print("[*] 批量统计资源附件...", flush=True)
    resource_values, resource_statuses = _collect_resource_estimates_batch(
        usernames,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    for username in usernames:
        stats[username]["attachment_estimated_bytes"] += resource_values[username]
        stats[username]["statuses"].update(resource_statuses[username])

    print("[*] 批量统计语音数据...", flush=True)
    voice_values, voice_statuses = _collect_voice_estimates_batch(
        usernames,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    for username in usernames:
        stats[username]["attachment_estimated_bytes"] += voice_values[username]
        stats[username]["statuses"].update(voice_statuses[username])

    if size_mode == "scan":
        print("[*] 扫描本地附件目录...", flush=True)
        iterable = _tqdm(usernames, desc="扫描附件") if _tqdm else usernames
        for username in iterable:
            scanned_bytes, scan_status = _scan_local_attachment_bytes(username)
            stats[username]["attachment_scanned_bytes"] = scanned_bytes
            if scan_status:
                stats[username]["statuses"].add(scan_status)

    return {
        username: _finalize_plan_stats(stats[username])
        for username in usernames
    }


def _build_plan_csv_rows(chat_rows, start_ts=None, end_ts=None, size_mode="estimate"):
    stats_by_username = _collect_all_plan_stats(
        chat_rows,
        start_ts=start_ts,
        end_ts=end_ts,
        size_mode=size_mode,
    )
    rows = []
    iterable = _tqdm(chat_rows, desc="统计会话") if _tqdm else chat_rows
    for row in iterable:
        username = row["username"]
        stats = stats_by_username[username]
        rows.append({
            "export": "",
            "index": row["index"],
            "username": username,
            "chat_name": row["display_name"],
            "chat_type": row["kind"],
            "message_count": stats["message_count"],
            "first_time": stats["first_time"],
            "last_time": stats["last_time"],
            "attachment_estimated_bytes": stats["attachment_estimated_bytes"],
            "attachment_scanned_bytes": stats["attachment_scanned_bytes"],
            "total_estimated_bytes": stats["total_estimated_bytes"],
            "size_status": stats["size_status"],
        })
    return rows


def _validate_plan_mode(plan_mode):
    if plan_mode not in (PLAN_MODE_BLACKLIST, PLAN_MODE_WHITELIST):
        raise ValueError(f"未知导出计划模式: {plan_mode}")
    return plan_mode


def _write_plan_csv(path, rows, plan_mode=PLAN_MODE_BLACKLIST):
    plan_mode = _validate_plan_mode(plan_mode)
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=PLAN_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            full = {field: "" for field in PLAN_CSV_FIELDS}
            full.update(row)
            writer.writerow(full)


def _load_selected_usernames_from_plan_csv(
    path, valid_usernames, plan_mode=PLAN_MODE_BLACKLIST
):
    plan_mode = _validate_plan_mode(plan_mode)
    selected = []
    seen = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "username" not in reader.fieldnames:
            raise ValueError("CSV 缺少 username 列")
        for line_no, row in enumerate(reader, 2):
            username = (row.get("username") or "").strip()
            if not username:
                raise ValueError(f"第 {line_no} 行缺少 username")
            if username in seen:
                raise ValueError(
                    f"CSV 中 username 重复: {username} "
                    f"(第 {seen[username]} 行和第 {line_no} 行)"
                )
            seen[username] = line_no

            flag = (row.get("export") or "").strip()
            if plan_mode == PLAN_MODE_WHITELIST:
                should_export = flag == "1"
            else:
                should_export = flag != "0"
            if not should_export:
                continue
            if username not in valid_usernames:
                raise ValueError(f"第 {line_no} 行 username 当前不存在: {username}")
            selected.append(username)
    return selected


def export_one(username, output_dir, names, transcribe=False,
               start_ts=None, end_ts=None, incremental=False,
               describe_images=False, extract_files=False, reconcile=False):
    """
    导出单个会话。

    参数:
        start_ts: 消息起始时间戳（None = 全部）
        end_ts: 消息结束时间戳（None = 全部）
        incremental: 增量模式（追加到已有消息，跳过重复）

    返回: (成功标志, 总消息数, 新增消息数, 错误信息)
    """
    ctx = mcp_server._resolve_chat_context(username)
    if ctx is None:
        return False, 0, 0, f"Cannot resolve: {username}"

    display_name = ctx["display_name"]
    message_tables = ctx["message_tables"]

    if not message_tables:
        return False, 0, 0, "no tables"

    # 输出文件名可随备注变化；索引用 username 维持稳定匹配。
    try:
        out_path, export_index = _resolve_indexed_export_path(
            output_dir, username, display_name, ctx["is_group"]
        )
    except Exception as e:
        return False, 0, 0, f"export index error: {e}"

    # 增量模式：读取已有消息和最后时间戳（含边界兜底）
    existing_msgs = []
    last_ts = 0
    if incremental and os.path.isfile(out_path):
        existing_msgs, status = _existing_messages_or_status(out_path)
        if status == "corrupt":
            # chat.json 损坏 → 备份原文件 + 退化为全量重建，绝不追加到坏文件
            bak = _backup_corrupt_file(out_path)
            print(f"   [!] {display_name} 的 chat.json 损坏，已备份({os.path.basename(bak) if bak else '失败'})，改为全量重建")
            incremental = False
            existing_msgs = []
        elif existing_msgs and _db_max_local_id(message_tables) < max(
                (m.get("local_id") or 0) for m in existing_msgs):
            # DB 最大 local_id < 已存最大 → 微信重装/数据重置 → 全量重建，避免错配/漏消息
            print(f"   [!] {display_name} 检测到 local_id 回退(疑似微信重装)，改为全量重建")
            incremental = False
            existing_msgs = []
        elif not reconcile:
            # 正常增量：用水位线只查新消息。reconcile 模式则全量扫描+按 local_id 去重(补回回填的旧消息)
            last_ts = _get_last_message_ts(out_path)
            if last_ts and (start_ts is None or start_ts < last_ts):
                start_ts = last_ts

    # 如果提供了 start_ts/end_ts 但没有增量数据，仍需查询
    if start_ts is not None and incremental and not existing_msgs:
        # 无增量目标文件，退化为普通导出
        incremental = False

    new_rows = []
    for table_info in message_tables:
        db_path = table_info["db_path"]
        table_name = table_info["table_name"]
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                id_to_username = mcp_server._load_name2id_maps(conn)

                # 增量模式：只查 start_ts 之后的消息
                if start_ts is not None or end_ts is not None:
                    rows = mcp_server._query_messages(
                        conn, table_name,
                        start_ts=start_ts, end_ts=end_ts,
                        limit=None, oldest_first=True,
                    )
                else:
                    rows = mcp_server._query_messages(
                        conn, table_name, limit=None, oldest_first=True
                    )

                for row in rows:
                    new_rows.append((row, id_to_username))
        except Exception as e:
            return False, 0, 0, f"DB query error: {e}"

    new_rows.sort(key=lambda pair: pair[0][2] or 0)

    local_ids_existing = {m.get("local_id") for m in existing_msgs}

    # 构建已有消息的 local_id → message 映射（用于合并时保留 transcription）
    existing_by_lid = {m.get("local_id"): m for m in existing_msgs}

    new_messages = []
    video_md5s = {}    # local_id -> [md5,...]，供视频提取按 md5 匹配本地 mp4
    file_md5s = {}     # local_id -> md5，供文件提取按 md5 匹配(区分同名/(1)变体)
    record_items = {}  # local_id -> [{datatype,fullmd5,datatitle},...]，合并转发嵌套条目
    for row, id_to_username in new_rows:
        local_id, local_type, create_time, real_sender_id, content, ct = row

        # 增量模式：跳过已存在的消息
        if incremental and local_id in local_ids_existing:
            continue

        sender, sender_id = _resolve_sender(row, ctx, names, id_to_username)
        type_str = _msg_type_str(local_type)
        rendered, extras = _extract_content(
            local_id, local_type, content, ct, username, display_name
        )
        raw = mcp_server._decompress_content(content, ct) or ""
        if type_str == "video":
            md5s = re.findall(r'(?:md5|newmd5|originsourcemd5)="([0-9a-f]{32})"', raw)
            if md5s:
                video_md5s[local_id] = md5s
        # 文件消息(appmsg type 6)：抓 <md5>，供文件提取按 md5 精确匹配(同名/(1)变体也能区分)
        if rendered and rendered.startswith(_FILE_MSG_PREFIX):
            fm5 = re.search(r'<md5>([0-9a-fA-F]{32})</md5>', raw)
            if fm5:
                file_md5s[local_id] = fm5.group(1).lower()
        # 合并转发(49/19)：抓 recorditem 各条目 fullmd5，供 Rec/ 内容按 md5 匹配
        if "<dataitem" in raw:
            items = []
            for dm in re.finditer(
                r'<dataitem\b[^>]*?datatype="(\d+)"(.*?)</dataitem>', raw, re.S
            ):
                block = dm.group(2)
                fm = re.search(r'<fullmd5>([0-9a-f]{32})</fullmd5>', block)
                tt = re.search(r'<datatitle>(.*?)</datatitle>', block, re.S)
                items.append({
                    "datatype": dm.group(1),
                    "fullmd5": fm.group(1) if fm else None,
                    "datatitle": tt.group(1).strip() if tt else None,
                })
            if items:
                record_items[local_id] = items

        msg = {"local_id": local_id, "timestamp": create_time, "sender": sender}
        if sender_id:
            msg["sender_id"] = sender_id   # 发送者 wxid/userid；空则省略
        effective_type = (extras or {}).get("type") or type_str
        if effective_type != "text":
            msg["type"] = effective_type
        if rendered is not None:
            msg["content"] = rendered
        if extras:
            for k, v in extras.items():
                if k == "type":
                    continue
                msg[k] = v
        new_messages.append(msg)

    # 合并消息
    messages = existing_msgs + new_messages
    new_count = len(new_messages)

    if not messages:
        return False, 0, 0, "empty"

    # 媒体提取汇总：{类型: {"检测","成功","失败","失败原因"}}
    media_summary = {}

    # 增量模式下，媒体只处理"本次新增的消息"，已处理过的不重复解码/复制/描述（P1 核心）。
    # 全量模式处理全部消息。
    media_scope = new_messages if incremental else messages

    def _count(t):
        if t == "文件":
            return sum(1 for m in media_scope if m.get("type") == "link_or_file"
                       and (m.get("content") or "").startswith(_FILE_MSG_PREFIX))
        return sum(1 for m in media_scope if m.get("type") == t)

    # ── 语音转录 ──────────────────────────────────────────────
    # 转录文字直接写进 content（形如 "[语音] …"），不再单独留 transcription 字段。
    # 去重靠 content：已有 content 的语音视为转过，重导/增量不重复跑。
    if transcribe:
        voices_to_transcribe = new_messages if incremental else [
            m for m in messages
            if m.get("type") == "voice" and not m.get("content")
        ]
        transcribed = 0
        failed = 0
        for msg in voices_to_transcribe:
            if msg.get("type") != "voice":
                continue
            lid = msg["local_id"]
            try:
                result = mcp_server.transcribe_voice_cached(username, lid)
                if result and result.get("text"):
                    msg["content"] = f"[语音] {result['text']}"
                    transcribed += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        media_summary["语音"] = {
            "检测": _count("voice"), "成功": transcribed, "失败": failed,
            "失败原因": "解码/转录失败" if failed else "",
        }

    # 收集本次所有缺失/已解决，统一写台账；并给缺失媒体的正文打"未下载"标
    all_pending = []
    all_resolved = set()
    _msg_by_lid = {m.get("local_id"): m for m in media_scope}

    # ── 图片：解码保存到 images/ + 可选 VLM 描述 ────────────────
    if extract_files or (describe_images and _resolve_image_backend() != "none"):
        do_describe = describe_images and _resolve_image_backend() != "none"
        if do_describe:
            # 可选扩展缺失时降级为「不识别」，图片解码/保存照常。
            try:
                import image_describe
            except ImportError:
                do_describe = False
        img_msgs = [m for m in media_scope if m.get("type") == "image"]
        images_dir = None
        saved = described = img_failed = 0
        reason = ""
        for msg in img_msgs:
            lid = msg["local_id"]
            try:
                result = mcp_server._image_resolver.decode_image(username, lid)
                if not result.get("success"):
                    img_failed += 1
                    reason = "图片未下载/解密失败"
                    all_pending.append({"chat": username, "local_id": lid, "kind": "image",
                                        "ref": {"md5": result.get("md5")},
                                        "target": f"images/image_{lid}", "reason": "not_downloaded"})
                    continue
                # 存图到 <chat>/images/image_<lid>.<ext>
                if extract_files:
                    if images_dir is None:
                        images_dir = output_layout.chat_subdir(
                            username, display_name, output_layout.SUBDIR_IMAGES
                        )
                    ext = result.get("format") or "jpg"
                    _copy_writable(result["path"],
                                   os.path.join(images_dir, f"image_{lid}.{ext}"))
                    saved += 1
                    all_resolved.add((lid, "image"))
                # 描述（仅未有 content 的）
                if do_describe and not msg.get("content"):
                    if result.get("only_thumbnail"):
                        # 本地只有缩略图（微信未点开过原图），不调 VLM（白等/卡住）；
                        # 贴友好说明标记为缩略图不可分析
                        msg["content"] = "[图片]（本地仅有微信缩略图，未下载原图，无法分析图片内容）"
                        described += 1
                    else:
                        desc = image_describe.describe_image_file(
                            result["path"], image_md5=result.get("md5")
                        )
                        if desc:
                            msg["content"] = f"[图片] {desc}"
                            described += 1
            except Exception:
                img_failed += 1
                reason = "图片处理异常"
        media_summary["图片"] = {
            "检测": _count("image"), "成功": saved if extract_files else described,
            "失败": img_failed, "失败原因": reason,
            "_described": described, "_saved": saved,
        }

    # ── 文件 / 视频 / 合并转发 ─────────────────────────────────
    if extract_files:
        n, f_pending, f_resolved = _extract_chat_files(
            username, display_name, media_scope, file_md5s)
        all_pending += f_pending
        all_resolved |= f_resolved
        media_summary["文件"] = {
            "检测": _count("文件"), "成功": n, "失败": len(f_pending),
            "失败原因": "文件未下载到本地" if f_pending else "",
        }
        nv, v_pending, v_resolved = _extract_chat_videos(
            username, display_name, media_scope, video_md5s
        )
        all_pending += v_pending
        all_resolved |= v_resolved
        media_summary["视频"] = {
            "检测": _count("video"), "成功": nv, "失败": len(v_pending),
            "失败原因": "视频未播放/未下载" if v_pending else "",
        }
        fwd = _extract_forwarded_content(
            username, display_name, media_scope, record_items,
            describe=describe_images and _resolve_image_backend() != "none",
        )
        if fwd.get("files") or fwd.get("images"):
            media_summary["合并转发"] = {
                "检测": "-", "成功": f"{fwd.get('files', 0)}文件+{fwd.get('images', 0)}图",
                "失败": "", "失败原因": "(仅已点开下载的)",
            }

        # 给"未下载"媒体的正文打标，并写入台账（精确到 local_id+md5，交给后续补抓）
        for p in all_pending:
            m = _msg_by_lid.get(p["local_id"])
            if m is not None:
                base = m.get("content") or {"image": "[图片]", "video": "[视频]",
                                            "file": f"[文件] {p.get('ref', {}).get('filename', '')}".strip()
                                            }.get(p["kind"], "[媒体]")
                if "（未下载" not in base:
                    m["content"] = base + "（未下载，待补抓）"
        if all_pending or all_resolved:
            _merge_pending(output_dir, username, all_pending, all_resolved)

    # ── 打印媒体汇总 ──────────────────────────────────────────
    _print_media_summary(names.get(username, username), media_summary)

    # ── 写文件 ────────────────────────────────────────────────
    output = {
        "chat": display_name,
        "username": username,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_first_msg": _date_from_message_ts(messages[0].get("timestamp")),
        "date_last_msg": _date_from_message_ts(messages[-1].get("timestamp")),
    }
    if ctx["is_group"]:
        output["is_group"] = True
    else:
        output.update(_contact_metadata_for_export(username, ctx["is_group"]))
    output["messages"] = messages

    _atomic_write_json(out_path, output)   # 原子写：断电/中断不留半文件
    _update_export_index(
        output_dir, export_index, username, display_name, ctx["is_group"],
        out_path, output
    )

    return True, len(messages), new_count, None


def _transcribe_delta_voice_messages(username, messages):
    for msg in messages:
        if msg.get("type") != "voice" or msg.get("content"):
            continue
        lid = msg["local_id"]
        try:
            result = mcp_server.transcribe_voice_cached(username, lid)
            if result and result.get("text"):
                msg["content"] = f"[语音] {result['text']}"
        except Exception:
            continue


def export_delta_one(username, delta_root, names, run_id, start_ts,
                     end_ts=None, transcribe=False):
    if start_ts is None:
        raise ValueError("delta export requires start_ts")

    ctx = mcp_server._resolve_chat_context(username)
    if ctx is None:
        return {
            "success": False,
            "username": username,
            "message_count": 0,
            "reason": f"Cannot resolve: {username}",
        }

    display_name = ctx["display_name"]
    message_tables = ctx["message_tables"]
    if not message_tables:
        return {
            "success": False,
            "username": username,
            "message_count": 0,
            "reason": "no tables",
        }

    rows_for_delta = []
    for table_info in message_tables:
        db_path = table_info["db_path"]
        table_name = table_info["table_name"]
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                id_to_username = mcp_server._load_name2id_maps(conn)
                rows = mcp_server._query_messages(
                    conn,
                    table_name,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    limit=None,
                    oldest_first=True,
                )
                for row in rows:
                    rows_for_delta.append((row, table_info, id_to_username))
        except Exception as e:
            return {
                "success": False,
                "username": username,
                "message_count": 0,
                "reason": f"DB query error: {e}",
            }

    rows_for_delta.sort(key=lambda item: item[0][2] or 0)
    messages = []
    for row, table_info, id_to_username in rows_for_delta:
        local_id, local_type, create_time, real_sender_id, content, ct = row
        raw_content = content
        sender, sender_id = _resolve_sender(row, ctx, names, id_to_username)
        type_str = _msg_type_str(local_type)
        rendered, extras = _extract_content(
            local_id, local_type, raw_content, ct, username, display_name
        )
        effective_type = (extras or {}).get("type") or type_str
        msg = {
            "msg_uid": _delta_msg_uid(
                username=username,
                db_path=table_info.get("db_path"),
                local_id=local_id,
                timestamp=create_time,
                msg_type=effective_type,
                content=raw_content,
            ),
            "local_id": local_id,
            "timestamp": create_time,
            "sender": sender,
        }
        if sender_id:
            msg["sender_id"] = sender_id   # 发送者 wxid/userid；空则省略
        if effective_type != "text":
            msg["type"] = effective_type
        if rendered is not None:
            msg["content"] = rendered
        if extras:
            for key, value in extras.items():
                if key != "type":
                    msg[key] = value
        messages.append(msg)

    if not messages:
        return {
            "success": True,
            "skipped": True,
            "username": username,
            "chat": display_name,
            "message_count": 0,
            "reason": "no messages in delta window",
        }

    if transcribe:
        _transcribe_delta_voice_messages(username, messages)

    delta_rel_path = os.path.join(
        "chats",
        _delta_filename(display_name, ctx["is_group"], username),
    )
    delta_path = os.path.join(delta_root, "deltas", run_id, delta_rel_path)
    output = {
        "schema_version": DELTA_SCHEMA_VERSION,
        "export_kind": "wechat_delta",
        "chat": display_name,
        "username": username,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "range": {
            "start": _date_from_message_ts(start_ts),
            "end": _date_from_message_ts(end_ts) if end_ts is not None else "",
        },
    }
    if messages:
        output["date_first_msg"] = _date_from_message_ts(
            messages[0].get("timestamp")
        )
        output["date_last_msg"] = _date_from_message_ts(
            messages[-1].get("timestamp")
        )
    if ctx["is_group"]:
        output["is_group"] = True
    else:
        output.update(_contact_metadata_for_export(username, ctx["is_group"]))
    output["message_count"] = len(messages)
    output["messages"] = messages

    os.makedirs(os.path.dirname(delta_path), exist_ok=True)
    with open(delta_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    return {
        "success": True,
        "username": username,
        "chat": display_name,
        "path": delta_rel_path.replace("\\", "/"),
        "message_count": len(messages),
    }


def _write_delta_manifest(delta_root, run_id, start_ts, end_ts,
                          chats_checked, results):
    files = []
    errors = []
    for result in results:
        if result.get("success") and result.get("message_count", 0) > 0:
            files.append({
                "username": result["username"],
                "chat": result.get("chat", result["username"]),
                "path": result["path"],
                "message_count": result.get("message_count", 0),
            })
        elif not result.get("success"):
            errors.append({
                "username": result.get("username", ""),
                "reason": result.get("reason", "unknown"),
            })
    manifest = {
        "schema_version": DELTA_SCHEMA_VERSION,
        "export_kind": "wechat_delta_run",
        "run_id": run_id,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "range": {
            "start": _date_from_message_ts(start_ts),
            "end": _date_from_message_ts(end_ts) if end_ts is not None else "",
        },
        "chats_checked": chats_checked,
        "chats_with_messages": len(files),
        "messages_exported": sum(item["message_count"] for item in files),
        "files": files,
        "errors": errors,
    }
    manifest_path = Path(delta_root) / "deltas" / run_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest_path


_BACKEND_CACHE = None


def _resolve_backend():
    """解析转录 backend，结果缓存以避免重复检测。"""
    global _BACKEND_CACHE
    if _BACKEND_CACHE is None:
        try:
            _BACKEND_CACHE = mcp_server._resolve_active_backend()
        except Exception:
            _BACKEND_CACHE = "local"
    return _BACKEND_CACHE


_IMAGE_BACKEND_CACHE = None


def _resolve_image_backend():
    """解析图片描述 backend (ollama / none)，结果缓存。未运行时尝试自动唤醒。"""
    global _IMAGE_BACKEND_CACHE
    if _IMAGE_BACKEND_CACHE is None:
        try:
            import image_describe
            image_describe.ensure_ollama()
            _IMAGE_BACKEND_CACHE = image_describe.resolve_backend()
        except Exception:
            _IMAGE_BACKEND_CACHE = "none"
    return _IMAGE_BACKEND_CACHE


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="批量导出所有微信聊天记录为 JSON 文件，可选附带语音转录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python3 export_all_chats.py                           全量导出所有会话
    python3 export_all_chats.py --write-plan-csv export_plan.csv
    python3 export_all_chats.py --write-plan-csv export_plan.csv --plan-mode whitelist
    python3 export_all_chats.py --write-plan-csv export_plan.csv --size-mode scan
    python3 export_all_chats.py output_dir --from-plan-csv export_plan.csv
    python3 export_all_chats.py output_dir --from-plan-csv export_plan.csv --plan-mode whitelist
    python3 export_all_chats.py -t                        全量导出 + 转录语音
    python3 export_all_chats.py -i                        增量（追加新消息）
    python3 export_all_chats.py --start 2025-01-01        按日期范围导出
    python3 export_all_chats.py --end 2025-01-31          按日期范围导出
    python3 export_all_chats.py --start 2025-01-01 --end 2025-01-31 -t
""",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help="输出目录路径 (默认: ./exported_chats)",
    )
    parser.add_argument(
        "-t",
        "--with-transcriptions",
        action="store_true",
        help="导出时一并转录语音消息（依赖 config.json 配置的 backend）",
    )
    parser.add_argument(
        "-d",
        "--describe-images",
        action="store_true",
        help="导出时一并用 VLM 描述图片内容并贴进正文"
             "（依赖 config.json image_describe_backend=dashscope 或 ollama）",
    )
    parser.add_argument(
        "-f",
        "--extract-files",
        action="store_true",
        help="导出时把聊天里的文件(pdf/word/excel…明文)复制到该聊天的 files/ 目录",
    )
    parser.add_argument(
        "--write-plan-csv",
        default=None,
        help="生成可人工编辑的导出计划 CSV，不导出聊天",
    )
    parser.add_argument(
        "--from-plan-csv",
        default=None,
        help="读取导出计划 CSV，按 --plan-mode 判断哪些 username 需要导出",
    )
    parser.add_argument(
        "--plan-mode",
        choices=(PLAN_MODE_BLACKLIST, PLAN_MODE_WHITELIST),
        default=PLAN_MODE_BLACKLIST,
        help=(
            "导出计划模式：blacklist=只有 export=0 跳过；"
            "whitelist=只有 export=1 导出"
        ),
    )
    parser.add_argument(
        "--size-mode",
        choices=("estimate", "scan"),
        default="estimate",
        help="生成计划 CSV 时的大小统计方式：estimate=快估，scan=尝试扫描本地附件",
    )
    parser.add_argument(
        "-i",
        "--incremental",
        action="store_true",
        help="增量导出：只追加新消息到已有 JSON 文件",
    )
    parser.add_argument(
        "--delta-only",
        action="store_true",
        help="只导出 --start/--end 时间窗口内的增量 delta JSON，不读取或覆盖已有完整 JSON",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="起始日期 (如 2025-01-01 或 Unix 时间戳)",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="结束日期 (如 2025-01-31 或 Unix 时间戳)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式：显示将导出的会话，不实际写入",
    )
    parser.add_argument(
        "--retry-media",
        action="store_true",
        help="只重试补抓未下载媒体台账(_pending_media.json)，不导出消息",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="对账模式：全量扫描已有聊天并按 local_id 去重，补回增量会漏的消息"
             "(如手机回填的历史消息)；不重复处理已有消息",
    )
    parser.add_argument(
        "--users",
        default=None,
        help="只导出指定 username 的会话, 逗号分隔 (如 wxid_xxx,12345@chatroom). "
             "为空时导出全部 (旧行为). 也可用 env WECHAT_EXPORT_USERS",
    )
    args = parser.parse_args(argv)

    if args.write_plan_csv and args.from_plan_csv:
        parser.error("--write-plan-csv 和 --from-plan-csv 只能使用一个")

    # 默认输出到项目外的统一根目录（每个聊天一个文件夹，见 output_layout.py）。
    # 仍可用位置参数 output_dir 显式覆盖。
    output_dir = args.output_dir or output_layout.output_root()

    # 并发锁：避免定时任务与手动同步同时写同一目录（dry-run / 仅生成计划CSV 不需要）
    if not args.dry_run and not args.write_plan_csv:
        if _acquire_sync_lock(output_dir) is None:
            print("[!] 另一个同步任务正在运行(.sync.lock)，本次退出以避免写冲突。")
            return
        import atexit
        atexit.register(_release_sync_lock, output_dir)

    # --retry-media：只补抓未下载媒体台账，不导出消息
    if args.retry_media:
        print("[*] 补抓未下载媒体台账...")
        r = _retry_pending_media(
            output_dir, describe=args.describe_images and _resolve_image_backend() != "none")
        print(f"补抓完成：恢复 {r['recovered']} 个，仍缺 {r['still_missing']} 个")
        return

    start_ts = _parse_timestamp(args.start) if args.start else None
    end_ts = _parse_timestamp(args.end) if args.end else None
    if args.start and start_ts is None:
        print(f"错误: 无法解析起始时间: {args.start}", file=sys.stderr)
        print("支持格式: 2025-01-01, 2025-01-01 14:30, 2025-01-01T14:30:00", file=sys.stderr)
        sys.exit(1)
    if args.end and end_ts is None:
        print(f"错误: 无法解析结束时间: {args.end}", file=sys.stderr)
        print("支持格式: 2025-01-01, 2025-01-01 14:30, 2025-01-01T14:30:00", file=sys.stderr)
        sys.exit(1)
    if args.delta_only and start_ts is None:
        parser.error("--delta-only requires --start to avoid accidental full export")

    if args.with_transcriptions:
        try:
            backend = _resolve_backend()
            print(f"语音转录: 启用 (backend={backend})")
        except Exception as e:
            print(f"语音转录: backend 解析失败: {e}", file=sys.stderr)
            args.with_transcriptions = False

    if args.describe_images:
        img_backend = _resolve_image_backend()
        if img_backend != "none":
            print(f"图片描述: 启用 (backend={img_backend})")
        else:
            print("图片描述: 未启用 (需 config image_describe_backend=dashscope 或 ollama)，已跳过",
                  file=sys.stderr)
            args.describe_images = False

    if not os.path.exists(mcp_server.DECRYPTED_DIR):
        print(f"错误: 解密目录不存在: {mcp_server.DECRYPTED_DIR}", file=sys.stderr)
        sys.exit(1)

    session_db = os.path.join(mcp_server.DECRYPTED_DIR, "session", "session.db")
    try:
        sessions = _load_session_usernames(session_db)
    except sqlite3.Error as e:
        print(f"会话数据库查询失败: {e}", file=sys.stderr)
        sys.exit(1)

    # username 白名单过滤 (--users 参数 / WECHAT_EXPORT_USERS 环境变量)
    users_filter_raw = args.users or os.environ.get("WECHAT_EXPORT_USERS", "")
    if users_filter_raw.strip():
        wanted = {u.strip() for u in users_filter_raw.split(",") if u.strip()}
        before = len(sessions)
        sessions = [u for u in sessions if u in wanted]
        print(f"按 --users 过滤: {before} → {len(sessions)} 会话")
        if not sessions:
            print(f"[!] 指定的 username 列表跟会话表没交集 (wanted={list(wanted)[:5]}...)",
                  file=sys.stderr)
            sys.exit(1)

    names = mcp_server.get_contact_names()
    contact_full = mcp_server.get_contact_full()
    chat_rows = _build_chat_rows(sessions, names, contact_full)

    # 显示模式信息
    mode = ""
    if args.delta_only:
        mode = "Delta 增量导出"
    elif args.incremental:
        mode = "增量模式"
    if start_ts:
        start_dt = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M")
        mode += f" 起始={start_dt}"
    if end_ts:
        end_dt = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M")
        mode += f" 结束={end_dt}"
    if not mode:
        mode = "全量模式"
    if args.dry_run:
        mode += " (预览)"

    print(f"会话总数: {len(sessions)}")
    print(f"联系人映射: {len(names)}")
    print(f"输出目录: {output_dir}")
    print(f"模式: {mode}")
    print("=" * 60)

    if args.write_plan_csv:
        rows = _build_plan_csv_rows(
            chat_rows,
            start_ts=start_ts,
            end_ts=end_ts,
            size_mode=args.size_mode,
        )
        try:
            _write_plan_csv(args.write_plan_csv, rows, plan_mode=args.plan_mode)
        except OSError as e:
            print(
                f"写入导出计划 CSV 失败: {e}\n"
                "请确认目标文件没有被 Excel/WPS 打开，或换一个输出文件名。",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"已生成导出计划 CSV: {args.write_plan_csv}")
        if args.plan_mode == PLAN_MODE_WHITELIST:
            print("白名单模式：请在 export 列填写 1 后，使用 --from-plan-csv 导出。")
        else:
            print("黑名单模式：请将不导出的行改为 export=0，再使用 --from-plan-csv 导出。")
        return

    if args.from_plan_csv:
        try:
            sessions = _load_selected_usernames_from_plan_csv(
                args.from_plan_csv,
                {row["username"] for row in chat_rows},
                plan_mode=args.plan_mode,
            )
        except (OSError, ValueError) as e:
            print(f"读取导出计划 CSV 失败: {e}", file=sys.stderr)
            sys.exit(1)
        if not sessions:
            print("未选择任何会话，已取消。")
            return

    if args.from_plan_csv:
        print(f"本次选择: {len(sessions)} 个会话")
        print("=" * 60)

    if args.dry_run:
        print("预览模式：未写入任何导出文件。")
        return

    os.makedirs(output_dir, exist_ok=True)

    # 增量同步开始前，先补抓上次记录的未下载媒体（微信可能已下载到本地）
    if args.incremental and not args.delta_only:
        r = _retry_pending_media(
            output_dir, describe=args.describe_images and _resolve_image_backend() != "none")
        if r["recovered"] or r["still_missing"]:
            print(f"[*] 媒体补抓：恢复 {r['recovered']} 个，仍缺 {r['still_missing']} 个")

    t0 = time.time()
    ok, skip, err, total = 0, 0, 0, 0
    total_new = 0

    total_sessions = len(sessions)
    run_id = _delta_run_id()
    delta_results = []
    for i, username in enumerate(sessions, 1):
        display = names.get(username, username)
        chat_t0 = time.time()
        print(f"[{i}/{total_sessions}] 开始导出: {display} ({username})", flush=True)
        if args.delta_only:
            result = export_delta_one(
                username=username,
                delta_root=output_dir,
                names=names,
                run_id=run_id,
                start_ts=start_ts,
                end_ts=end_ts,
                transcribe=args.with_transcriptions,
            )
            delta_results.append(result)
            success = result.get("success", False)
            total_msgs = result.get("message_count", 0)
            new_msgs = total_msgs
            reason = result.get("reason")
        else:
            success, total_msgs, new_msgs, reason = export_one(
                username, output_dir, names,
                transcribe=args.with_transcriptions,
                start_ts=start_ts,
                end_ts=end_ts,
                incremental=args.incremental or args.reconcile,
                describe_images=args.describe_images,
                extract_files=args.extract_files,
                reconcile=args.reconcile,
            )
        if success:
            elapsed = time.time() - t0
            chat_elapsed = time.time() - chat_t0
            eta = (elapsed / i) * (total_sessions - i) if i > 0 else 0
            if args.delta_only and result.get("skipped"):
                skip += 1
                print(
                    f"[{i}/{total_sessions}] 跳过: {display} ({reason}) "
                    f"(本会话 {chat_elapsed:.1f}s, ETA {eta/60:.1f}分)",
                    flush=True,
                )
                continue

            ok += 1
            total += total_msgs
            total_new += new_msgs
            if new_msgs > 0 or args.incremental:
                label = f"+{new_msgs} new" if args.incremental else f"{total_msgs} msgs"
            else:
                label = f"{total_msgs} msgs"
            print(
                f"[{i}/{total_sessions}] 完成导出: {display} - {label} "
                f"(本会话 {chat_elapsed:.1f}s, ETA {eta/60:.1f}分)",
                flush=True,
            )
        else:
            if "no tables" in str(reason) or "empty" in str(reason):
                skip += 1
                elapsed = time.time() - t0
                chat_elapsed = time.time() - chat_t0
                eta = (elapsed / i) * (total_sessions - i) if i > 0 else 0
                print(
                    f"[{i}/{total_sessions}] 跳过: {display} ({reason}) "
                    f"(本会话 {chat_elapsed:.1f}s, ETA {eta/60:.1f}分)",
                    flush=True,
                )
            else:
                err += 1
                elapsed = time.time() - t0
                chat_elapsed = time.time() - chat_t0
                eta = (elapsed / i) * (total_sessions - i) if i > 0 else 0
                print(
                    f"[{i}/{total_sessions}] 失败: {display} - {reason} "
                    f"(本会话 {chat_elapsed:.1f}s, ETA {eta/60:.1f}分)",
                    flush=True,
                )

    if args.delta_only:
        manifest_path = _write_delta_manifest(
            delta_root=output_dir,
            run_id=run_id,
            start_ts=start_ts,
            end_ts=end_ts,
            chats_checked=total_sessions,
            results=delta_results,
        )
        print(f"Delta manifest: {manifest_path}")

    elapsed = time.time() - t0
    print()
    print("=" * 60)
    extra = f" (新增 {total_new} 条)" if args.incremental and total_new > 0 else ""
    print(
        f"完成! 成功={ok} 跳过={skip} 失败={err} "
        f"总消息={total}{extra} 耗时={elapsed/60:.1f}分"
    )


if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()  # 单独运行时也记录日志(经 main.py 启动则已初始化,此处幂等)
    main()
