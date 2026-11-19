"""render_content_for_db（realtime 落库线人话渲染）+ format_msg_type 复合类型细分。

背景：realtime 落库线曾把 _decode_content 的结果直接存 chat_message.content，
appmsg（文件/引用/链接）、图片/语音/系统等消息存成了原始 XML（下游 huineng_2
发现 442 条）；msg_type_label 对微信 v4 的 64 位复合类型码（如 (57<<32)|49 =
引用回复）显示成 "type=244813135921"。

本测试锁定修复后的行为：
- render_content_for_db 与导出线同一解析器（_extract_content）渲染人话，
  图片/语音给占位符（media_caption/voice_transcript 另有专列），
  渲染不出来回退解压去前缀后的原文（未知类型宁可存原文不丢信息）。
- mcp_server.format_msg_type 对复合类型拆 base+sub，49 按子类型细分 label。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_export_helpers
import mcp_server


def _file_appmsg(title="报价单.xlsx", app_type=6):
    return (
        '<msg><appmsg appid="" sdkver="0">'
        f'<title>{title}</title>'
        f'<type>{app_type}</type>'
        '</appmsg></msg>'
    )


class _PassthroughMixin(unittest.TestCase):
    """与 test_chat_export_helpers 同款：跳过解压、固定联系人表。"""

    def setUp(self):
        self._p1 = patch.object(
            mcp_server, '_decompress_content',
            side_effect=lambda content, ct: content)
        self._p1.start()
        self.addCleanup(self._p1.stop)
        self._p2 = patch.object(
            mcp_server, 'get_contact_names',
            return_value={'wxid_orig_sender': 'Alice'})
        self._p2.start()
        self.addCleanup(self._p2.stop)


class RenderContentForDbTests(_PassthroughMixin):
    GROUP = 'g123@chatroom'

    def _render(self, local_type, content):
        return chat_export_helpers.render_content_for_db(
            1, local_type, content, 0, self.GROUP, '测试群')

    def test_file_appmsg_rendered(self):
        out = self._render((6 << 32) | 49, _file_appmsg())
        self.assertEqual(out, "[文件] 报价单.xlsx")

    def test_file_appmsg_type74_rendered_not_xml(self):
        out = self._render((74 << 32) | 49, _file_appmsg(app_type=74))
        self.assertNotIn('<appmsg', out)
        self.assertIn('报价单.xlsx', out)

    def test_refer_appmsg_rendered_not_xml(self):
        xml = (
            '<msg><appmsg appid="" sdkver="0">'
            '<title>好的收到</title>'
            '<type>57</type>'
            '<refermsg><type>1</type><content>原始消息</content>'
            '<fromusr>wxid_orig_sender</fromusr>'
            '<displayname>Original Sender</displayname></refermsg>'
            '</appmsg></msg>'
        )
        out = self._render((57 << 32) | 49, xml)
        self.assertNotIn('<appmsg', out)
        self.assertIn('好的收到', out)

    def test_image_placeholder(self):
        out = self._render(3, '<msg><img aeskey="k" md5="m"/></msg>')
        self.assertEqual(out, "[图片]")

    def test_voice_placeholder(self):
        out = self._render(34, '<msg><voicemsg length="1200"/></msg>')
        self.assertEqual(out, "[语音]")

    def test_plain_text_kept(self):
        self.assertEqual(self._render(1, "你好"), "你好")

    def test_group_prefix_stripped_for_text(self):
        self.assertEqual(self._render(1, "wxid_abc:\n你好"), "你好")

    def test_unknown_type_falls_back_to_raw(self):
        self.assertEqual(self._render(999, "raw-stuff"), "raw-stuff")

    def test_unknown_type_strips_group_prefix(self):
        self.assertEqual(self._render(999, "wxid_abc:\nraw-stuff"), "raw-stuff")


class FormatMsgTypeCompoundTests(unittest.TestCase):
    def test_compound_file(self):
        self.assertEqual(mcp_server.format_msg_type((6 << 32) | 49), '文件')
        self.assertEqual(mcp_server.format_msg_type((74 << 32) | 49), '文件')

    def test_compound_refer(self):
        self.assertEqual(mcp_server.format_msg_type((57 << 32) | 49), '引用回复')

    def test_compound_others(self):
        self.assertEqual(mcp_server.format_msg_type((5 << 32) | 49), '链接')
        self.assertEqual(mcp_server.format_msg_type((19 << 32) | 49), '聊天记录')
        self.assertEqual(mcp_server.format_msg_type((2000 << 32) | 49), '转账')

    def test_plain_types_unchanged(self):
        self.assertEqual(mcp_server.format_msg_type(1), '文本')
        self.assertEqual(mcp_server.format_msg_type(49), '链接/文件')
        self.assertEqual(mcp_server.format_msg_type(10000), '系统')

    def test_unknown_still_shows_raw(self):
        self.assertEqual(mcp_server.format_msg_type(31337), 'type=31337')


if __name__ == "__main__":
    unittest.main()
