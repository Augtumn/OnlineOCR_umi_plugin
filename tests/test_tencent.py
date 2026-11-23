# -*- coding: utf-8 -*-
"""TencentOCR 插件的离线测试：解析、签名、超时下限、重试。

运行： python tests/run_tests.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PLUGINS, "TencentOCR"))

import tencent_ocr  # noqa: E402


def cell(text, row, col, polygon=None):
    return {
        "Text": text,
        "RowTl": row,
        "ColTl": col,
        "RowBr": row + 1,
        "ColBr": col + 1,
        "Polygon": polygon or [
            {"X": col * 100, "Y": row * 40},
            {"X": col * 100 + 100, "Y": row * 40},
            {"X": col * 100 + 100, "Y": row * 40 + 40},
            {"X": col * 100, "Y": row * 40 + 40},
        ],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.api = tencent_ocr.Api({
            "secret_id": "id",
            "secret_key": "key",
            "timeout": 10,
            "ssl_verify": True,
            "debug_log": False,
        })

    def start_ok(self, **extra):
        params = {"api_type": "GeneralBasicOCR"}
        params.update(extra)
        self.assertEqual(self.api.start(params), "")


class TestStart(Base):
    def test_unknown_action_has_error_prefix(self):
        msg = self.api.start({"api_type": "NopeOCR"})
        self.assertTrue(msg.startswith("[Error]"), msg)

    def test_missing_credentials(self):
        api = tencent_ocr.Api({})
        self.assertIn("SecretId", api.start({"api_type": "GeneralBasicOCR"}))

    def test_timeout_floor_for_tables(self):
        self.start_ok(api_type="RecognizeTableOCR", timeout=10)
        self.assertEqual(self.api.timeout, 60)

    def test_timeout_user_can_raise(self):
        self.start_ok(api_type="RecognizeTableOCR", timeout=90)
        self.assertEqual(self.api.timeout, 90)

    def test_timeout_untouched_for_text(self):
        self.start_ok(api_type="GeneralAccurateOCR", timeout=10)
        self.assertEqual(self.api.timeout, 10)

    def test_language_normalization(self):
        self.assertEqual(tencent_ocr.Api._normalize_language("ja"), "jap")
        self.assertEqual(tencent_ocr.Api._normalize_language("en"), "auto")
        self.assertEqual(tencent_ocr.Api._normalize_language(None), "auto")
        self.assertEqual(tencent_ocr.Api._normalize_language("  ZH "), "zh")


class TestSign(Base):
    def setUp(self):
        super().setUp()
        self.start_ok(api_type="GeneralBasicOCR")
        self.api.language_type = "jap"

    def test_structure(self):
        orig_time = tencent_ocr.time.time
        tencent_ocr.time.time = lambda: 1700000000
        try:
            auth, body, ts = self.api._build_sign({"image_base64": "ABC"})
        finally:
            tencent_ocr.time.time = orig_time
        self.assertEqual(ts, 1700000000)
        # 2023-11-14 UTC
        self.assertIn("Credential=id/2023-11-14/ocr/tc3_request", auth)
        self.assertIn("SignedHeaders=content-type;host", auth)
        signature = auth.rsplit("Signature=", 1)[1]
        self.assertEqual(len(signature), 64)
        int(signature, 16)  # 必须是十六进制

    def test_payload(self):
        auth, body, ts = self.api._build_sign({"image_base64": "ABC"})
        self.assertIn('"ImageBase64":"ABC"', body)
        self.assertIn('"LanguageType":"jap"', body)

    def test_table_v2_payload_has_table_language(self):
        self.start_ok(api_type="RecognizeTableOCR")
        auth, body, ts = self.api._build_sign({"image_base64": "ABC"})
        self.assertIn('"TableLanguage":"zh"', body)


class TestParseText(Base):
    def setUp(self):
        super().setUp()
        self.start_ok(api_type="GeneralAccurateOCR")

    def test_with_polygon(self):
        resp = {"RequestId": "r1", "TextDetections": [
            {"DetectedText": "你好", "Confidence": 99,
             "Polygon": [{"X": 0, "Y": 0}, {"X": 100, "Y": 0}, {"X": 100, "Y": 30}, {"X": 0, "Y": 30}]},
        ]}
        out = self.api._parse_result(resp)
        self.assertEqual(out["code"], 100)
        self.assertEqual(out["data"][0]["text"], "你好")
        self.assertEqual(out["data"][0]["box"][2], [100, 30])
        self.assertEqual(out["extra"]["request_id"], "r1")

    def test_missing_polygon_gets_fallback_box(self):
        resp = {"TextDetections": [{"DetectedText": "无坐标"}, {"DetectedText": "第二行"}]}
        out = self.api._parse_result(resp)
        self.assertEqual(len(out["data"]), 2)
        self.assertEqual(len(out["data"][0]["box"]), 4)
        self.assertNotEqual(out["data"][0]["box"], out["data"][1]["box"])

    def test_empty(self):
        out = self.api._parse_result({"TextDetections": []})
        self.assertEqual(out["code"], 101)


class TestRetiredActions(Base):
    """腾讯公式识别已下线：旧配置要给出可操作的提示，而不是含糊报错。"""

    def test_formula_offline_message(self):
        msg = self.api.start({"api_type": "FormulaOCR"})
        self.assertTrue(msg.startswith("[Error]"), msg)
        self.assertIn("下线", msg)
        self.assertIn("百度", msg)

    def test_recognize_formula_deprecated_message(self):
        msg = self.api.start({"api_type": "RecognizeFormulaOCR"})
        self.assertTrue(msg.startswith("[Error]"), msg)
        self.assertIn("下线", msg)

    def test_formula_not_in_ui_options(self):
        """公式识别已从界面选项里移除，避免用户再选到死接口。"""
        import types
        if "plugin_i18n" not in sys.modules:
            stub = types.ModuleType("plugin_i18n")
            stub.Translator = lambda *args, **kwargs: (lambda text: text)
            sys.modules["plugin_i18n"] = stub
        import tencent_ocr_config
        options = [opt[0] for opt in tencent_ocr_config.localOptions["api_type"]["optionsList"]]
        self.assertNotIn("FormulaOCR", options)
        self.assertNotIn("RecognizeFormulaOCR", options)
        self.assertIn("RecognizeTableOCR", options)

    def test_action_offline_hint(self):
        """即使绕过 start() 直接调，ActionOffline 也要附带提示。"""
        self.start_ok(api_type="GeneralAccurateOCR")
        orig_send = tencent_ocr.Api._send
        tencent_ocr.Api._send = lambda *a: (
            {"Error": {"Code": "ActionOffline", "Message": "接口下线"}}, None, "", False)
        try:
            out = self.api._ocr_request("BASE64DATA")
        finally:
            tencent_ocr.Api._send = orig_send
        self.assertEqual(out["code"], 102)
        self.assertIn("已下线", out["data"])

    def test_package_run_out_hint(self):
        self.start_ok(api_type="GeneralAccurateOCR")
        orig_send = tencent_ocr.Api._send
        tencent_ocr.Api._send = lambda *a: (
            {"Error": {"Code": "ResourceUnavailable.ResourcePackageRunOut",
                       "Message": "账号资源包耗尽。"}}, None, "", False)
        try:
            out = self.api._ocr_request("BASE64DATA")
        finally:
            tencent_ocr.Api._send = orig_send
        self.assertIn("资源包", out["data"])

    def test_deprecated_warning_kept_in_extra(self):
        self.start_ok(api_type="GeneralAccurateOCR")
        orig_send = tencent_ocr.Api._send
        tencent_ocr.Api._send = lambda *a: (
            {"TextDetections": [{"DetectedText": "A", "Polygon": [{"X": 0, "Y": 0}] * 3}],
             "DeprecatedWarning": "接口下线", "RequestId": "r"}, None, "", False)
        try:
            out = self.api._ocr_request("BASE64DATA")
        finally:
            tencent_ocr.Api._send = orig_send
        self.assertEqual(out["code"], 100)
        self.assertEqual(out["extra"]["deprecated"], "接口下线")


class TestParseTableV1(Base):
    def setUp(self):
        super().setUp()
        self.start_ok(api_type="TableOCR")

    def test_flat_cells(self):
        resp = {"RequestId": "r3", "Angle": 0, "Data": "XLSX",
                "TableDetections": [cell("姓名", 0, 0), cell("年龄", 0, 1), cell("张三", 1, 0)]}
        out = self.api._parse_result(resp)
        self.assertEqual(out["code"], 100)
        self.assertEqual(out["data"][0]["text"], "姓名\t年龄\n张三")
        self.assertEqual(out["extra"]["excel_base64"], "XLSX")
        self.assertEqual(out["extra"]["table_count"], 1)

    def test_legacy_text_detections_key(self):
        """老返回结构用的是 TextDetections 键，也要能读。"""
        resp = {"TextDetections": [cell("A", 0, 0), cell("B", 1, 0)]}
        out = self.api._parse_result(resp)
        self.assertEqual(out["code"], 100)
        self.assertEqual(out["data"][0]["text"], "A\nB")

    def test_empty(self):
        out = self.api._parse_result({"TableDetections": []})
        self.assertEqual(out["code"], 101)


class TestParseTableV2(Base):
    def setUp(self):
        super().setUp()
        self.start_ok(api_type="RecognizeTableOCR")

    def test_nested_cells(self):
        resp = {"RequestId": "r4", "Angle": -270, "Data": "XLSX2",
                "TableDetections": [{
                    "Titles": None,
                    "Cells": [
                        cell("五(1)班卫生值日表", 0, 0, [
                            {"X": 26, "Y": 193}, {"X": 774, "Y": 193},
                            {"X": 774, "Y": 282}, {"X": 26, "Y": 282}]),
                        cell("星期一", 1, 0), cell("梅亚婷", 1, 1),
                    ],
                }]}
        out = self.api._parse_result(resp)
        self.assertEqual(out["code"], 100)
        self.assertEqual(out["data"][0]["text"], "五(1)班卫生值日表\n星期一\t梅亚婷")
        self.assertEqual(out["data"][0]["box"], [[0, 40], [774, 40], [774, 282], [0, 282]])
        self.assertEqual(out["extra"]["table_count"], 1)

    def test_multiple_tables(self):
        resp = {"TableDetections": [
            {"Cells": [cell("T1-1", 0, 0)]},
            {"Cells": [cell("T2-1", 0, 0)]},
        ]}
        out = self.api._parse_result(resp)
        self.assertEqual(out["code"], 100)
        self.assertEqual(out["data"][0]["text"], "T1-1\n\nT2-1")
        self.assertEqual(out["extra"]["table_count"], 2)

    def test_unstructured_cells_fall_back_to_order(self):
        resp = {"TableDetections": [{"Cells": [
            {"Text": "右", "Polygon": [{"X": 200, "Y": 0}, {"X": 300, "Y": 0}, {"X": 300, "Y": 20}]},
            {"Text": "左", "Polygon": [{"X": 0, "Y": 0}, {"X": 100, "Y": 0}, {"X": 100, "Y": 20}]},
        ]}]}
        out = self.api._parse_result(resp)
        self.assertEqual(out["data"][0]["text"], "左\n右")


class TestGridToText(Base):
    def test_empty_cells_keep_columns(self):
        grid = [["A", "", "C"], ["D", "E", ""]]
        self.assertEqual(tencent_ocr.Api._grid_to_text(grid), "A\t\tC\nD\tE")

    def test_empty_row_skipped(self):
        grid = [["A"], ["", ""], ["B"]]
        self.assertEqual(tencent_ocr.Api._grid_to_text(grid), "A\nB")


class TestRetry(Base):
    def setUp(self):
        super().setUp()
        self.start_ok(api_type="GeneralAccurateOCR")
        self._orig_sleep = tencent_ocr.time.sleep
        tencent_ocr.time.sleep = lambda s: None

    def tearDown(self):
        tencent_ocr.time.sleep = self._orig_sleep

    def _patch_send(self, results):
        calls = []
        def fake(*args):
            # 绑定为类属性后会多传一个 self，取最后一个参数
            req = args[-1]
            calls.append(getattr(req, "full_url", "?"))
            return results.pop(0)
        self._orig_send = tencent_ocr.Api._send
        tencent_ocr.Api._send = fake
        self.addCleanup(self._restore)
        return calls

    def _restore(self):
        tencent_ocr.Api._send = self._orig_send

    def test_retry_on_rate_limit(self):
        calls = self._patch_send([
            ({"Error": {"Code": "RequestLimitExceeded", "Message": "too many"}}, None, "", False),
            ({"TextDetections": [{"DetectedText": "OK", "Polygon": [{"X": 0, "Y": 0}] * 3}]}, None, "", False),
        ])
        out = self.api._ocr_request("BASE64DATA")
        self.assertEqual(out["code"], 100)
        self.assertEqual(len(calls), 2)

    def test_no_retry_on_auth_error(self):
        calls = self._patch_send([
            ({"Error": {"Code": "AuthFailure.SignatureFailure", "Message": "bad sig"}}, None, "", False),
        ])
        out = self.api._ocr_request("BASE64DATA")
        self.assertEqual(out["code"], 102)
        self.assertEqual(out["error"]["type"], "auth")
        self.assertEqual(len(calls), 1)

    def test_retryable_network_once(self):
        calls = self._patch_send([
            ({}, "网络异常: timed out", "network", True),
            ({"TextDetections": [{"DetectedText": "OK", "Polygon": [{"X": 0, "Y": 0}] * 3}]}, None, "", False),
        ])
        out = self.api._ocr_request("BASE64DATA")
        self.assertEqual(out["code"], 100)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
