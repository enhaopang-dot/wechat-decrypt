"""实时消息落库 —— 占位实现（未启用）。

monitor_web.py 支持把实时抓到的群消息旁路写入一个 PostgreSQL 库，供下游
应用（AI 助手、检索服务等）读取。该功能属于可选扩展，本发布版本不包含
具体的数据库实现，仅保留接口占位：

    is_enabled() 恒返回 False，monitor_web 中所有落库分支自动跳过，
    解密、导出、监控等核心功能不受影响。

若需启用，按下列接口自行实现即可（建议 psycopg 3 + 连接池）：

  is_enabled()                          -> bool     落库是否可用
  record_id(conv_id, db_name, local_id, create_time) -> str   幂等消息 id
  upsert_group_chat(group_id, group_name=None)       群会话表 upsert
  ensure_group(group_id, group_name=None)            确保群存在
  ensure_member(group_id, wxid, display_name=None)   确保成员存在
  reconcile_group_members(group_id, members)         成员全量对齐
  insert_message(rec)                                写入单条消息
  update_message_media(rid, media_path=None, media_caption=None, **kw)
                                                     回填媒体路径/描述
  latest_create_time(group_id)          -> int|None  已落库最大时间戳（补漏用）
  describe_context(group_id, before_ts, author_wxid=None, limit=10)
                                        -> list      给图片/视频识别注入上文
  caption_by_md5(md5)                   -> str|None  同图描述复用缓存
  transcript_done(record_id)            -> bool      语音是否已转写
"""


def is_enabled():
    """落库未启用 —— 所有写函数均为 no-op。"""
    return False


def record_id(conv_id, db_name, local_id, create_time):
    return None


def upsert_group_chat(group_id, group_name=None):
    return None


def ensure_group(group_id, group_name=None):
    return None


def ensure_member(group_id, wxid, display_name=None):
    return None


def reconcile_group_members(group_id, members):
    return None


def insert_message(rec):
    return None


def update_message_media(rid, media_path=None, media_caption=None, **kwargs):
    return None


def latest_create_time(group_id):
    return None


def describe_context(group_id, before_ts, author_wxid=None, limit=10):
    return []


def caption_by_md5(md5):
    return None


def transcript_done(record_id):
    return False
