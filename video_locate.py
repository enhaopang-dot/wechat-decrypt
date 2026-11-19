"""视频文件定位与持久化 —— monitor_web（实时）/ backfill_media（补录）共用。

微信视频是明文 mp4 存 <wechat_base>/msg/video/，文件名是存储 hash，**不等于**
videomsg XML 的 <md5>；但 mp4 内容 md5 == videomsg md5（实测，与
export_all_chats._wechat_video_index 同一结论）。所以按内容 md5 建索引匹配。
只有在微信里播放/下载过的视频本地才有文件——找不到不是错误，跳过即可，
recaption 补跑时（用户可能已点开过）会再试。

找到后复制到 decoded_video_dir（默认 <项目>/decoded_videos/<md5>.mp4）持久化：
微信会清理视频缓存，落库 media_path 必须指向我们自己的副本。
索引增量维护：只 hash 新出现或 mtime/size 变化的文件，实时线程可频繁调用。
"""
import hashlib
import os
import re
import shutil
import threading

from config import load_config

_cfg = load_config()
DECODED_VIDEO_DIR = _cfg.get(
    "decoded_video_dir",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "decoded_videos"),
)

# 与 export_all_chats 的视频 md5 提取同一正则（md5/newmd5/originsourcemd5 都是候选）
_MD5_RE = re.compile(r'(?:md5|newmd5|originsourcemd5)="([0-9a-f]{32})"')

_index = {}    # 内容 md5 -> 微信目录里的源路径
_seen = {}     # 源路径 -> (mtime, size)，避免重复 hash
_lock = threading.Lock()


def extract_md5s(xml_text):
    """从视频消息 XML 提取候选 md5 列表（可能多个，按出现顺序）。"""
    if not xml_text:
        return []
    return _MD5_RE.findall(xml_text)


def _video_root():
    base = _cfg.get("wechat_base_dir") or os.path.dirname(_cfg.get("db_dir", ""))
    return os.path.join(base, "msg", "video")


def refresh_index():
    """增量扫描 msg/video 下的 mp4：新文件/变化的才重新算内容 md5。"""
    root_dir = _video_root()
    if not os.path.isdir(root_dir):
        return
    with _lock:
        for root, _dirs, files in os.walk(root_dir):
            for fn in files:
                if not fn.lower().endswith(".mp4"):
                    continue
                p = os.path.join(root, fn)
                try:
                    st = os.stat(p)
                    sig = (st.st_mtime, st.st_size)
                    if _seen.get(p) == sig:
                        continue
                    h = hashlib.md5()
                    with open(p, "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
                    _index[h.hexdigest()] = p
                    _seen[p] = sig
                except OSError:
                    continue


def _find_in_index(md5s):
    for m in md5s:
        p = _index.get(m)
        if p and os.path.isfile(p):
            return m, p
    return None, None


def locate(md5s):
    """按候选 md5 找视频并持久化。返回 (decoded_videos 里的路径, 命中的 md5)，
    找不到返回 (None, None)。先查已持久化副本，再查索引（miss 时增量刷新一次）。"""
    if isinstance(md5s, str):
        md5s = [md5s]
    md5s = [m.lower() for m in (md5s or []) if m]
    if not md5s:
        return None, None
    for m in md5s:
        dest = os.path.join(DECODED_VIDEO_DIR, f"{m}.mp4")
        if os.path.isfile(dest):
            return dest, m
    m, src = _find_in_index(md5s)
    if src is None:
        refresh_index()
        m, src = _find_in_index(md5s)
    if src is None:
        return None, None
    os.makedirs(DECODED_VIDEO_DIR, exist_ok=True)
    dest = os.path.join(DECODED_VIDEO_DIR, f"{m}.mp4")
    tmp = dest + ".part"
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)   # 原子落位：不留半截文件
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None, None
    return dest, m
