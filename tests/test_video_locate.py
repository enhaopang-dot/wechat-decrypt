"""视频定位：XML md5 提取 / 内容 md5 索引 / 持久化复制 / 增量扫描。

微信视频文件名 ≠ videomsg <md5>，但内容 md5 == videomsg md5（与
export_all_chats._wechat_video_index 同一结论），测试用真实临时文件验证全链路。
"""
import hashlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import video_locate


VIDEO_XML = ('<msg><videomsg aeskey="k" playlength="15" '
             'md5="{md5}" newmd5="{new}" /></msg>')


class ExtractTests(unittest.TestCase):
    def test_extract_md5s(self):
        m1, m2 = "a" * 32, "b" * 32
        got = video_locate.extract_md5s(VIDEO_XML.format(md5=m1, new=m2))
        self.assertEqual(got, [m1, m2])

    def test_extract_empty(self):
        self.assertEqual(video_locate.extract_md5s(""), [])
        self.assertEqual(video_locate.extract_md5s(None), [])
        self.assertEqual(video_locate.extract_md5s("<msg>无md5</msg>"), [])


class LocateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video_root = os.path.join(self.tmp.name, "msg", "video", "2026-07")
        os.makedirs(self.video_root)
        self.decoded = os.path.join(self.tmp.name, "decoded_videos")
        # 写一个"微信视频"：文件名是存储 hash(随意)，内容 md5 才是匹配键
        self.content = b"fake-mp4-bytes-0123456789"
        self.md5 = hashlib.md5(self.content).hexdigest()
        with open(os.path.join(self.video_root, "6fec2fdeadbeef.mp4"), "wb") as f:
            f.write(self.content)
        for p in (
            patch.object(video_locate, "DECODED_VIDEO_DIR", self.decoded),
            patch.object(video_locate, "_video_root",
                         return_value=os.path.join(self.tmp.name, "msg", "video")),
            patch.object(video_locate, "_index", {}),
            patch.object(video_locate, "_seen", {}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_locate_copies_to_decoded_dir(self):
        path, hit = video_locate.locate([self.md5])
        self.assertEqual(hit, self.md5)
        self.assertEqual(path, os.path.join(self.decoded, f"{self.md5}.mp4"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.content)

    def test_locate_prefers_existing_persisted_copy(self):
        os.makedirs(self.decoded, exist_ok=True)
        pre = os.path.join(self.decoded, f"{self.md5}.mp4")
        with open(pre, "wb") as f:
            f.write(b"already-persisted")
        with patch.object(video_locate, "refresh_index") as ri:
            path, hit = video_locate.locate(self.md5)   # str 入参也接受
            ri.assert_not_called()                       # 有持久副本不扫描
        self.assertEqual(path, pre)
        self.assertEqual(hit, self.md5)

    def test_locate_miss_returns_none(self):
        path, hit = video_locate.locate(["f" * 32])
        self.assertEqual((path, hit), (None, None))

    def test_locate_empty_input(self):
        self.assertEqual(video_locate.locate([]), (None, None))
        self.assertEqual(video_locate.locate(None), (None, None))

    def test_refresh_incremental_skips_unchanged(self):
        video_locate.refresh_index()
        with patch.object(video_locate.hashlib, "md5",
                          side_effect=AssertionError("不应重复 hash 未变化文件")):
            video_locate.refresh_index()   # mtime/size 未变 → 不再 hash


if __name__ == "__main__":
    unittest.main()
