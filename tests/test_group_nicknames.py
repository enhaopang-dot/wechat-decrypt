"""群昵称（群名片）显示名解析测试。

需求：群消息发送者显示名应与微信群里看到的一致——优先级
**群昵称 > 备注 > 微信昵称 > wxid**。群昵称存在 contact.db 的
chat_room.ext_buffer（protobuf BLOB）里：顶层 repeated field 1 = 成员，
成员子消息 sub-field 1 = wxid、sub-field 2 = 群昵称、sub-field 4 = 邀请人。

这里同时覆盖：
- _decode_room_nicknames 的 protobuf 解码（含畸形输入安全降级、邀请人字段不误判）
- _resolve_sender_identity 群分支的优先级与 self 处理
- _resolve_quote_sender_label 群引用同样走群昵称
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import mcp_server


# ---- 合成 chat_room.ext_buffer protobuf 的小工具 ----

def _uv(n):
    """protobuf varint 编码。"""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _str_field(field_no, text):
    data = text.encode("utf-8")
    return bytes([(field_no << 3) | 2]) + _uv(len(data)) + data


def _varint_field(field_no, value):
    return bytes([(field_no << 3) | 0]) + _uv(value)


def _member(wxid, nick=None, inviter=None, flag=1):
    """一条成员（顶层 repeated field 1）。nick=None 表示没设群昵称。"""
    body = _str_field(1, wxid) + _varint_field(3, flag)
    if nick is not None:
        body += _str_field(2, nick)
    if inviter is not None:
        body += _str_field(4, inviter)
    return bytes([(1 << 3) | 2]) + _uv(len(body)) + body


class DecodeRoomNicknamesTests(unittest.TestCase):
    def test_extracts_only_members_with_nickname(self):
        blob = (
            _member("wxid_a", "群昵称A")
            + _member("wxid_b")                       # 没设群昵称 → 不收
            + _member("wxid_c", "群昵称C", inviter="wxid_a")
            + _varint_field(3, 700000017)             # 顶层尾部标量字段，应被忽略
            + _varint_field(4, 700000017)
        )
        self.assertEqual(
            mcp_server._decode_room_nicknames(blob),
            {"wxid_a": "群昵称A", "wxid_c": "群昵称C"},
        )

    def test_inviter_field_not_mistaken_for_nickname(self):
        # 成员只有 wxid + 邀请人(field4)，没有 field2 → 不应误把邀请人当群昵称
        blob = _member("wxid_x", nick=None, inviter="wxid_example")
        self.assertEqual(mcp_server._decode_room_nicknames(blob), {})

    def test_empty_string_nickname_excluded(self):
        # field2 存在但为空串 → 不算设了群昵称（锁住 `if wxid and nick` 的 falsy 排除）
        self.assertEqual(
            mcp_server._decode_room_nicknames(_member("wxid_e", nick="")), {}
        )

    def test_memoryview_input_supported(self):
        # sqlite3 通常返回 bytes，但 memoryview/buffer 输入也应正确解码而非静默返回 {}
        blob = _member("wxid_a", "群昵称A")
        self.assertEqual(
            mcp_server._decode_room_nicknames(memoryview(blob)),
            {"wxid_a": "群昵称A"},
        )

    def test_malformed_inputs_degrade_safely(self):
        for bad in (None, b"", b"\xff\xff\xff", b"\x0a", b"\x0a\x05abc"):
            self.assertEqual(mcp_server._decode_room_nicknames(bad), {})


class GroupSenderResolutionTests(unittest.TestCase):
    ROOM = "room@chatroom"

    def _resolve(self, real_sender_id, id_to_username, names):
        return mcp_server._resolve_sender_identity(
            real_sender_id=real_sender_id,
            sender_from_content="",
            is_group=True,
            chat_username=self.ROOM,
            chat_display_name="Group",
            names=names,
            id_to_username=id_to_username,
        )

    @patch.object(mcp_server, "_get_self_username", return_value="wxid_self")
    @patch.object(
        mcp_server, "_get_room_member_nicks",
        return_value={"room@chatroom": {"wxid_a": "群昵称A", "wxid_self": "我的群昵称"}},
    )
    def test_group_nickname_beats_remark(self, *_):
        pid, label = self._resolve(1, {1: "wxid_a"}, {"wxid_a": "备注A"})
        self.assertEqual(pid, "wxid_a")
        self.assertEqual(label, "群昵称A")

    @patch.object(mcp_server, "_get_self_username", return_value="wxid_self")
    @patch.object(
        mcp_server, "_get_room_member_nicks",
        return_value={"room@chatroom": {"wxid_a": "群昵称A"}},
    )
    def test_falls_back_to_names_when_no_group_nickname(self, *_):
        # wxid_b 没有群昵称 → 回退到 names 映射（names 本身已是 备注>昵称>wxid 的预解析值；
        # 这里只验证「无群昵称→取 names 值」这一步，备注 vs 昵称 的优先级由 _load_contacts_from 决定）
        _, label = self._resolve(2, {2: "wxid_b"}, {"wxid_b": "备注B"})
        self.assertEqual(label, "备注B")

    @patch.object(mcp_server, "_get_self_username", return_value="wxid_self")
    @patch.object(
        mcp_server, "_get_room_member_nicks",
        return_value={"room@chatroom": {"wxid_a": "群昵称A"}},
    )
    def test_falls_back_to_wxid_when_nothing_known(self, *_):
        # 既无群昵称也不在 names → 回退 wxid 本身
        _, label = self._resolve(3, {3: "wxid_example"}, {})
        self.assertEqual(label, "wxid_example")

    @patch.object(mcp_server, "_get_self_username", return_value="wxid_self")
    @patch.object(
        mcp_server, "_get_room_member_nicks",
        return_value={"room@chatroom": {"wxid_self": "我的群昵称"}},
    )
    def test_self_with_group_nickname_shows_nickname(self, *_):
        _, label = self._resolve(9, {9: "wxid_self"}, {})
        self.assertEqual(label, "我的群昵称")

    @patch.object(mcp_server, "_get_self_username", return_value="wxid_self")
    @patch.object(
        mcp_server, "_get_room_member_nicks",
        return_value={"room@chatroom": {"wxid_a": "群昵称A"}},
    )
    def test_self_without_group_nickname_shows_me(self, *_):
        # self 在该群没设群昵称 → 回退 'me'
        _, label = self._resolve(9, {9: "wxid_self"}, {"wxid_self": "今测-小金"})
        self.assertEqual(label, "me")


class DirectChatUnaffectedTests(unittest.TestCase):
    @patch.object(mcp_server, "_get_self_username", return_value="wxid_self")
    @patch.object(mcp_server, "_get_room_member_nicks", return_value={})
    def test_direct_chat_uses_display_name(self, *_):
        pid, label = mcp_server._resolve_sender_identity(
            real_sender_id=1, sender_from_content="", is_group=False,
            chat_username="wxid_example", chat_display_name="FriendDisplay",
            names={"wxid_example": "FriendRemark"}, id_to_username={1: "wxid_example"},
        )
        self.assertEqual(pid, "wxid_example")
        self.assertEqual(label, "FriendDisplay")


class RoomNickLoaderTests(unittest.TestCase):
    """_get_room_member_nicks：真实 SQLite 上的加载/缺失/不可读路径。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self._tmp, "contact.db")
        mcp_server._room_member_nicks = None     # 清掉上次的进程级缓存

    def tearDown(self):
        mcp_server._room_member_nicks = None
        try:
            os.remove(self.db_path)
        except OSError:
            pass
        os.rmdir(self._tmp)

    def _make_db(self, with_chat_room=True):
        conn = sqlite3.connect(self.db_path)
        if with_chat_room:
            conn.execute("CREATE TABLE chat_room(username TEXT, ext_buffer BLOB)")
            conn.execute(
                "INSERT INTO chat_room VALUES (?, ?)",
                ("room1@chatroom", _member("wxid_a", "群昵称A") + _member("wxid_b")),
            )
            conn.execute(
                "INSERT INTO chat_room VALUES (?, ?)",
                ("room2@chatroom", _member("wxid_c", "群昵称C")),
            )
            conn.execute("INSERT INTO chat_room VALUES (?, ?)", ("room3@chatroom", None))
        else:
            conn.execute("CREATE TABLE other(x INTEGER)")
        conn.commit()
        conn.close()

    def test_loads_nicks_from_chat_room(self):
        self._make_db(with_chat_room=True)
        with patch.object(mcp_server, "_get_contact_db_path", return_value=self.db_path):
            got = mcp_server._get_room_member_nicks()
        self.assertEqual(
            got,
            {"room1@chatroom": {"wxid_a": "群昵称A"}, "room2@chatroom": {"wxid_c": "群昵称C"}},
        )

    def test_missing_db_returns_empty(self):
        with patch.object(mcp_server, "_get_contact_db_path", return_value=None):
            self.assertEqual(mcp_server._get_room_member_nicks(), {})

    def test_missing_chat_room_table_returns_empty(self):
        self._make_db(with_chat_room=False)
        with patch.object(mcp_server, "_get_contact_db_path", return_value=self.db_path):
            self.assertEqual(mcp_server._get_room_member_nicks(), {})


class QuoteSenderGroupNicknameTests(unittest.TestCase):
    @patch.object(mcp_server, "_get_self_username", return_value="wxid_self")
    @patch.object(
        mcp_server, "_get_room_member_nicks",
        return_value={"room@chatroom": {"wxid_a": "群昵称A"}},
    )
    def test_group_quote_uses_room_nickname(self, *_):
        label = mcp_server._resolve_quote_sender_label(
            ref_user="wxid_a", ref_display_name="DisplayA", is_group=True,
            chat_username="room@chatroom", chat_display_name="Group",
            names={"wxid_a": "备注A"},
        )
        self.assertEqual(label, "群昵称A")


if __name__ == "__main__":
    unittest.main()
