# -*- coding: utf-8 -*-
"""BaiduOCR 插件的离线测试：不联网，只验证解析、参数裁剪、token 缓存与重试。

运行： python tests/run_tests.py
"""
import os
import struct
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PLUGINS, "BaiduOCR"))

import baidu_ocr  # noqa: E402


def make_png_bytes(width, height):
    """构造只带头部的最小 PNG，够 _probe_size 用。"""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return sig + ihdr + b"\x00" * 32


def make_jpeg_bytes(width, height):
    """构造 SOF0 段可识别的 JPEG 头。"""
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 + sof0 + b"\xff\xd9"


class Base(unittest.TestCase):
    def setUp(self):
        baidu_ocr._TOKEN_CACHE.clear()
        self.api = baidu_ocr.Api({
            "api_key": "ak",
            "secret_key": "sk",
            "timeout": 10,
            "ssl_verify": True,
            "debug_log": False,
        })
        self.api.api_type = "general"
        self.api.access_token = "TOKEN"

    def start_ok(self, **extra):
        params = {"api_type": "general"}
        params.update(extra)
        self.assertEqual(self.api.start(params), "")


class TestProbeSize(Base):
    def test_png(self):
        self.assertEqual(self.api._probe_size(make_png_bytes(1200, 800)), (1200, 800))

    def test_jpeg(self):
        self.assertEqual(self.api._probe_size(make_jpeg_bytes(640, 480)), (640, 480))

    def test_garbage(self):
        self.assertIsNone(self.api._probe_size(b"not an image"))


class TestParseGeneral(Base):
    def test_with_location(self):
        result = {
            "log_id": 123,
            "words_result_num": 2,
            "words_result": [
                {"words": "第一行", "location": {"left": 10, "top": 20, "width": 100, "height": 30}},
                {"words": "第二行", "location": {"left": 10, "top": 60, "width": 120, "height": 30}},
            ],
        }
        out = self.api._parse_result(result)
        self.assertEqual(out["code"], 100)
        self.assertEqual([d["text"] for d in out["data"]], ["第一行", "第二行"])
        self.assertEqual(out["data"][0]["box"], [[10, 20], [110, 20], [110, 50], [10, 50]])
        self.assertEqual(out["extra"]["log_id"], 123)

    def test_no_location_uses_image_size(self):
        """标准版没有 location，应按图片尺寸均分出行框，而不是固定 200x40。"""
        self.api._remember_size(make_png_bytes(1000, 400))
        result = {"words_result": [{"words": "A"}, {"words": "B"}, {"words": "C"}, {"words": "D"}]}
        out = self.api._parse_result(result)
        self.assertEqual(out["code"], 100)
        boxes = [d["box"] for d in out["data"]]
        self.assertEqual(boxes[0], [[0, 0], [1000, 0], [1000, 100], [0, 100]])
        self.assertEqual(boxes[3][0][1], 300)
        self.assertEqual(len(out["data"]), 4)

    def test_no_location_without_size_falls_back(self):
        result = {"words_result": [{"words": "A"}, {"words": "B"}]}
        out = self.api._parse_result(result)
        self.assertEqual(out["data"][0]["box"], [[0, 0], [200, 0], [200, 40], [0, 40]])
        self.assertEqual(out["data"][1]["box"][0][1], 40)

    def test_mixed_items_keeps_order(self):
        """有坐标的项与无坐标的项混在一起时，都不应该被丢弃。"""
        result = {"words_result": [
            {"words": "有坐标", "location": {"left": 0, "top": 0, "width": 50, "height": 20}},
            {"words": "无坐标"},
        ]}
        out = self.api._parse_result(result)
        self.assertEqual([d["text"] for d in out["data"]], ["有坐标", "无坐标"])

    def test_empty_words_result(self):
        out = self.api._parse_result({"words_result": []})
        self.assertEqual(out["code"], 101)
        self.assertEqual(out["data"], "")


class TestParseTable(Base):
    def setUp(self):
        super().setUp()
        self.api.api_type = "table"
        self.api._img_size = (800, 600)

    def test_grid(self):
        result = {"tables_result": [{"body": [
            {"row_start": 0, "row_end": 0, "col_start": 0, "col_end": 1, "words": "标题",
             "cell_location": [{"x": 0, "y": 0}, {"x": 800, "y": 0}, {"x": 800, "y": 50}, {"x": 0, "y": 50}]},
            {"row_start": 1, "row_end": 1, "col_start": 0, "col_end": 0, "words": "姓名"},
            {"row_start": 1, "row_end": 1, "col_start": 1, "col_end": 1, "words": "年龄"},
        ]}]}
        out = self.api._parse_result(result)
        self.assertEqual(out["code"], 100)
        # 合并单元格只填左上角，其余留空；行尾空单元格被裁掉
        self.assertEqual(out["data"][0]["text"], "标题\n姓名\t年龄")
        self.assertEqual(out["data"][0]["box"], [[0, 0], [800, 0], [800, 50], [0, 50]])

    def test_empty_tables(self):
        out = self.api._parse_result({"tables_result": []})
        self.assertEqual(out["code"], 101)

    def test_return_excel_shape(self):
        out = self.api._parse_result({"result": {"result_data": "UEsDB..."}})
        self.assertEqual(out["code"], 101)
        self.assertEqual(out["extra"]["result_data"], "UEsDB...")


class TestPayload(Base):
    def test_general_sends_language(self):
        self.api.api_type = "general"
        self.api.language_type = "JAP"
        payload = self.api._build_payload("BASE64")
        self.assertEqual(payload["language_type"], "JAP")
        self.assertEqual(payload["detect_direction"], "false")

    def test_accurate_does_not_send_language(self):
        """高精度版文档不支持 language_type，下发会被判参数错误。"""
        self.api.api_type = "accurate_basic"
        payload = self.api._build_payload("BASE64")
        self.assertNotIn("language_type", payload)
        self.assertIn("paragraph", payload)

    def test_webimage_minimal(self):
        self.api.api_type = "webimage"
        payload = self.api._build_payload("BASE64")
        self.assertEqual(set(payload), {"image", "detect_direction"})

    def test_table_params(self):
        self.api.api_type = "table"
        self.api.return_excel = True
        payload = self.api._build_payload("BASE64")
        self.assertEqual(payload["return_excel"], "true")
        self.assertNotIn("cell_contents", payload)


class TestToken(Base):
    def _fake_post(self, results):
        calls = []
        def fake(*args):
            # 作为类属性绑定后会先传 self，取最后两个参数
            url, payload = args[-2], args[-1]
            calls.append((url, payload))
            item = results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return fake, calls

    def test_token_cached_across_instances(self):
        fake, calls = self._fake_post([({"access_token": "T1", "expires_in": 2592000}, None, "")])
        orig = baidu_ocr.Api._post
        baidu_ocr.Api._post = fake
        try:
            api1 = baidu_ocr.Api({"api_key": "ak", "secret_key": "sk"})
            self.assertEqual(api1._get_access_token(), "T1")
            api2 = baidu_ocr.Api({"api_key": "ak", "secret_key": "sk"})
            self.assertEqual(api2._get_access_token(), "T1")
        finally:
            baidu_ocr.Api._post = orig
        self.assertEqual(len(calls), 1, "第二个实例应复用缓存 token，不再请求 oauth")

    def test_token_refresh_does_not_use_cache(self):
        fake, calls = self._fake_post([
            ({"access_token": "T1", "expires_in": 2592000}, None, ""),
            ({"access_token": "T2", "expires_in": 2592000}, None, ""),
        ])
        orig = baidu_ocr.Api._post
        baidu_ocr.Api._post = fake
        try:
            self.assertEqual(self.api._get_access_token(), "T1")
            self.api._drop_token()
            self.assertEqual(self.api._get_access_token(), "T2")
        finally:
            baidu_ocr.Api._post = orig
        self.assertEqual(len(calls), 2)

    def test_oauth_error_reported(self):
        fake, _ = self._fake_post([({"error": "invalid_client", "error_description": "unknown client id"}, None, "")])
        orig = baidu_ocr.Api._post
        baidu_ocr.Api._post = fake
        try:
            self.assertIsNone(self.api._get_access_token())
        finally:
            baidu_ocr.Api._post = orig
        self.assertIn("unknown client id", self.api._last_error)


class TestRequestRetry(Base):
    def test_invalid_token_triggers_one_reauth(self):
        seq = [
            ({"error_code": 110, "error_msg": "Access token invalid or no longer valid"}, None, ""),  # 第一次识别
            ({"access_token": "T-new", "expires_in": 2592000}, None, ""),          # 重新鉴权
            ({"words_result": [{"words": "OK", "location": {"left": 0, "top": 0, "width": 10, "height": 10}}],
              "log_id": 1}, None, ""),                                              # 重试成功
        ]
        calls = []
        def fake(*args):
            calls.append(args[-2])
            return seq.pop(0)
        orig = baidu_ocr.Api._post
        baidu_ocr.Api._post = fake
        try:
            out = self.api._ocr_request("BASE64DATA")
        finally:
            baidu_ocr.Api._post = orig
        self.assertEqual(out["code"], 100)
        self.assertEqual(out["data"][0]["text"], "OK")
        self.assertEqual(len(calls), 3)
        self.assertIn("oauth/2.0/token", calls[1])

    def test_qps_error_retried_once(self):
        """18 = QPS 超限，退避一次后应成功。"""
        seq = [
            ({"error_code": 18, "error_msg": "Open api qps request limit reached"}, None, ""),
            ({"words_result": [{"words": "OK", "location": {"left": 0, "top": 0, "width": 10, "height": 10}}]},
             None, ""),
        ]
        sleeps = []
        orig_post = baidu_ocr.Api._post
        orig_sleep = baidu_ocr.time.sleep

        def fake(*args):
            return seq.pop(0)

        baidu_ocr.Api._post = fake
        baidu_ocr.time.sleep = lambda s: sleeps.append(s)
        try:
            out = self.api._ocr_request("BASE64DATA")
        finally:
            baidu_ocr.Api._post = orig_post
            baidu_ocr.time.sleep = orig_sleep
        self.assertEqual(out["code"], 100)
        self.assertEqual(len(sleeps), 1)
        self.assertFalse(seq, "第二次请求应当成功，不应再消耗样例")

    def test_daily_limit_not_retried(self):
        """17 = 今日额度用尽，重试没有意义，不应等待。"""
        orig_post = baidu_ocr.Api._post
        orig_sleep = baidu_ocr.time.sleep
        tries = []
        sleeps = []

        def fake(*args):
            tries.append(1)
            return {"error_code": 17, "error_msg": "Open api daily request limit reached"}, None, ""

        baidu_ocr.Api._post = fake
        baidu_ocr.time.sleep = lambda s: sleeps.append(s)
        try:
            out = self.api._ocr_request("BASE64DATA")
        finally:
            baidu_ocr.Api._post = orig_post
            baidu_ocr.time.sleep = orig_sleep
        self.assertEqual(out["code"], 102)
        self.assertEqual(len(tries), 1)
        self.assertEqual(sleeps, [])

    def test_api_error_message_contains_code_and_hint(self):
        def fake(*args, **kwargs):
            return {"error_code": 17, "error_msg": "Open api daily request limit reached"}, None, ""
        orig = baidu_ocr.Api._post
        baidu_ocr.Api._post = fake
        try:
            out = self.api._ocr_request("BASE64DATA")
        finally:
            baidu_ocr.Api._post = orig
        self.assertEqual(out["code"], 102)
        self.assertIn("17", out["data"])
        self.assertIn("今日请求量", out["data"])

    def test_network_error(self):
        def fake(*args, **kwargs):
            return None, "网络异常: timed out", "network"
        orig = baidu_ocr.Api._post
        baidu_ocr.Api._post = fake
        try:
            out = self.api._ocr_request("BASE64DATA")
        finally:
            baidu_ocr.Api._post = orig
        self.assertEqual(out["code"], 102)
        self.assertEqual(out["error"]["type"], "network")


class TestStart(Base):
    def test_missing_keys(self):
        api = baidu_ocr.Api({})
        self.assertIn("API Key", api.start({"api_type": "general"}))

    def test_unknown_api(self):
        self.api._get_access_token = lambda: "T"
        msg = self.api.start({"api_type": "nope"})
        self.assertTrue(msg.startswith("[Error]"), msg)
        self.assertIn("不支持的接口", msg)

    def test_relative_timeout_string(self):
        """Umi 可能把数字配置传成字符串，转不了就退回默认值。"""
        self.assertEqual(baidu_ocr.Api._as_number("15", 10), 15.0)
        self.assertEqual(baidu_ocr.Api._as_number("abc", 10), 10)
        self.assertEqual(baidu_ocr.Api._as_number(None, 10), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
