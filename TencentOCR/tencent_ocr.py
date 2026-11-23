# tencent_ocr.py
# 腾讯云 OCR 插件。使用 TC3-HMAC-SHA256 手工签名，走 ocr.tencentcloudapi.com。
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib import request
from urllib.error import URLError, HTTPError
import ssl

# 支持的 Action。表格类包含老版 TableOCR（V1，仅老客户可续费）与 V2 两个接口。
# FormulaOCR 已于 2025 年被腾讯下线（返回 ActionOffline），替代的 RecognizeFormulaOCR
# 同样带 DeprecatedWarning，故不再提供腾讯公式识别，公式请改用百度接口。
_SUPPORTED_ACTIONS = (
    "GeneralBasicOCR",
    "GeneralAccurateOCR",
    "GeneralHandwritingOCR",
    "GeneralEfficientOCR",
    "GeneralFastOCR",
    "TableOCR",
    "RecognizeTableOCR",
    "RecognizeTableAccurateOCR",
    "EnglishOCR",
    "AdvertiseOCR",
)

# 已下线 / 不再可用的 Action：保留提示，便于旧配置直接给出可操作的信息
_RETIRED_ACTIONS = {
    "FormulaOCR": "腾讯云「公式识别（FormulaOCR）」已下线（ActionOffline），"
                  "请改用百度的「数学公式识别」。",
    "RecognizeFormulaOCR": "腾讯云「公式识别（RecognizeFormulaOCR）」已被官方标记下线，"
                           "请改用百度的「数学公式识别」。",
}

# 通用文本类接口（返回 TextDetections）
_TEXT_ACTIONS = (
    "GeneralBasicOCR",
    "GeneralAccurateOCR",
    "GeneralHandwritingOCR",
    "GeneralEfficientOCR",
    "GeneralFastOCR",
    "EnglishOCR",
    "AdvertiseOCR",
)

# 表格类接口
_TABLE_ACTIONS = ("TableOCR", "RecognizeTableOCR", "RecognizeTableAccurateOCR")

# 超时下限（秒）：表格识别慢，10s 默认值基本必然超时
_TIMEOUT_FLOOR = {
    "TableOCR": 60,
    "RecognizeTableOCR": 60,
    "RecognizeTableAccurateOCR": 60,
}

# 错误码 → 可操作的提示
_ERROR_HINTS = {
    "ActionOffline": "该接口已下线，请改选其它接口",
    "ResourceUnavailable.ResourcePackageRunOut": "该接口的资源包已耗尽，请到控制台续购或换用其它接口",
    "AuthFailure.SignatureExpire": "本机时间偏差过大，校准系统时间即可",
    "AuthFailure.SecretIdNotFound": "SecretId 不存在，请检查密钥",
    "LimitExceeded": "超出调用频率或配额，请降低批量速度",
    "RequestLimitExceeded": "触发限流，请降低批量速度",
}

# 可重试的错误码（限流 / 服务端抖动）
_RETRYABLE_CODES = ("RequestLimitExceeded", "InternalError", "FailedOperation.RequestTimeout")


class Api:
    def __init__(self, global_argd):
        self.secret_id = global_argd.get("secret_id", "").strip()
        self.secret_key = global_argd.get("secret_key", "").strip()
        self.timeout = self._as_number(global_argd.get("timeout"), 10)
        self.ssl_verify = global_argd.get("ssl_verify", True)
        self.debug_log = global_argd.get("debug_log", False)
        self.host = "ocr.tencentcloudapi.com"
        self.endpoint = "https://ocr.tencentcloudapi.com"
        self.region = "ap-guangzhou"
        self.version = "2018-11-19"
        self.api_type = None
        self.language_type = "auto"

    # ---------- 基础工具 ----------

    @staticmethod
    def _as_number(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _normalize_error_type(self, kind):
        allowed = {
            "auth", "network", "http", "api", "input", "io", "encode", "request", "config"
        }
        return kind if kind in allowed else "request"

    def _err(self, message, kind="request", code=102, extra=None):
        if not message:
            message = "未知错误"
        if message.startswith("[Error]"):
            data = message
            clean = message[len("[Error]"):].strip()
        else:
            data = f"[Error] {message}"
            clean = message
        result = {
            "code": code,
            "data": data,
            "error": {"message": clean, "type": self._normalize_error_type(kind)},
        }
        if extra:
            result["extra"] = extra
        return result

    def _ssl_context(self):
        if self.ssl_verify:
            return None
        return ssl._create_unverified_context()

    def _debug(self, message):
        if self.debug_log:
            print(f"[TencentOCR] {message}")

    # ---------- 生命周期 ----------

    def start(self, argd):
        api_type = argd.get("api_type", "GeneralBasicOCR")
        if api_type in _RETIRED_ACTIONS:
            # 旧配置里可能还存着已下线的接口，这里给出明确的替代方案
            return "[Error] " + _RETIRED_ACTIONS[api_type]
        if api_type not in _SUPPORTED_ACTIONS:
            return f"[Error] 不支持的接口: {api_type}"
        self.api_type = api_type

        if not self.secret_id:
            return "[Error] SecretId 不能为空，请在插件设置中填写。"
        if not self.secret_key:
            return "[Error] SecretKey 不能为空，请在插件设置中填写。"

        self.region = argd.get("region", self.region)
        self.ssl_verify = argd.get("ssl_verify", self.ssl_verify)
        self.debug_log = argd.get("debug_log", self.debug_log)
        self.language_type = self._normalize_language(argd.get("language_type", "auto"))

        # 局部 timeout 优先；同时给慢接口兜一个下限，避免 10s 默认值必然超时
        timeout = self._as_number(argd.get("timeout"), self.timeout)
        floor = _TIMEOUT_FLOOR.get(self.api_type, 0)
        self.timeout = max(timeout or 0, floor) or self.timeout

        return ""

    def stop(self):
        pass

    @staticmethod
    def _normalize_language(user_lang):
        if not isinstance(user_lang, str):
            return "auto"
        value = user_lang.strip().lower()
        mapping = {
            "auto": "auto", "zh": "zh", "chinese": "zh", "zh_rare": "zh_rare",
            "mix": "mix", "jap": "jap", "ja": "jap", "jp": "jap", "japanese": "jap",
            "kor": "kor", "ko": "kor", "korean": "kor", "spa": "spa", "spanish": "spa",
            "fre": "fre", "fr": "fre", "french": "fre", "ger": "ger", "de": "ger",
            "german": "ger", "por": "por", "pt": "por", "portuguese": "por",
            "vie": "vie", "vietnamese": "vie", "may": "may", "malay": "may",
            "rus": "rus", "ru": "rus", "russian": "rus", "ita": "ita",
            "it": "ita", "italian": "ita", "hol": "hol", "dutch": "hol", "swe": "swe",
            "swedish": "swe", "fin": "fin", "finnish": "fin", "dan": "dan",
            "danish": "dan", "nor": "nor", "norwegian": "nor", "hun": "hun",
            "hungarian": "hun", "tha": "tha", "thai": "tha", "hi": "hi",
            "hindi": "hi", "ara": "ara", "arabic": "ara",
            # 腾讯通用印刷体不做纯英文区分，英文交给 auto/EnglishOCR
            "en": "auto", "english": "auto",
        }
        return mapping.get(value, "auto")

    # ---------- 签名 ----------

    def _build_sign(self, params):
        algorithm = "TC3-HMAC-SHA256"
        service = "ocr"
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")

        payload = {}
        image_base64 = params.get("image_base64")
        if image_base64:
            payload["ImageBase64"] = image_base64

        if self.api_type == "GeneralBasicOCR":
            payload["LanguageType"] = self.language_type
        elif self.api_type == "EnglishOCR":
            payload["EnableCoordPoint"] = params.get("enable_coord_point", False)
            payload["EnableCandWord"] = params.get("enable_cand_word", False)
            payload["Preprocess"] = params.get("preprocess", True)
        elif self.api_type == "RecognizeTableOCR":
            # 表格识别 V2 支持中/日文表格；高精度版文档未列出该参数，不下发
            payload["TableLanguage"] = params.get("table_language", "zh")

        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        hashed_payload = hashlib.sha256(body.encode("utf-8")).hexdigest()

        canonical_uri = "/"
        canonical_querystring = ""
        canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{self.host}\n"
        signed_headers = "content-type;host"
        canonical_request = (
            "POST\n"
            f"{canonical_uri}\n"
            f"{canonical_querystring}\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{hashed_payload}"
        )

        credential_scope = f"{date}/{service}/tc3_request"
        hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_canonical}"

        def sign(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = sign(("TC3" + self.secret_key).encode("utf-8"), date)
        secret_service = sign(secret_date, service)
        secret_signing = sign(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        auth_header = (
            f"{algorithm} "
            f"Credential={self.secret_id}/{date}/{service}/tc3_request, "
            f"SignedHeaders=content-type;host, "
            f"Signature={signature}"
        )
        return auth_header, body, timestamp

    # ---------- 请求 ----------

    def _ocr_request(self, image_base64, allow_retry=True):
        if not image_base64 or len(image_base64) < 10:
            return self._err("图像数据为空或无效", kind="input")
        if not self.api_type:
            return self._err("接口未初始化，请重新开始识别", kind="config")

        auth_header, body, timestamp = self._build_sign({"image_base64": image_base64})

        req = request.Request(self.endpoint, data=body.encode("utf-8"), method="POST")
        req.add_header("Authorization", auth_header)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Host", self.host)
        req.add_header("X-TC-Action", self.api_type)
        req.add_header("X-TC-Version", self.version)
        req.add_header("X-TC-Region", self.region)
        req.add_header("X-TC-Timestamp", str(timestamp))

        resp, err, kind, retryable = self._send(req)
        if err:
            if retryable and allow_retry:
                self._debug(f"可重试错误（{err[:60]}），0.6s 后重试一次")
                time.sleep(0.6)
                return self._ocr_request(image_base64, allow_retry=False)
            return self._err(err, kind=kind)

        if "Error" in resp:
            error = resp["Error"]
            code = str(error.get("Code", ""))
            message = error.get("Message", "")
            text = f"{code}: {message}" if code else message
            if self._is_retryable_code(code) and allow_retry:
                self._debug(f"可重试错误（{code}），0.6s 后重试一次")
                time.sleep(0.6)
                return self._ocr_request(image_base64, allow_retry=False)
            hint = self._hint_for(code)
            if hint and hint not in message:
                text += f"（{hint}）"
            extra = {}
            if resp.get("DeprecatedWarning"):
                extra["deprecated"] = resp["DeprecatedWarning"]
            return self._err(text, kind=self._error_kind(code), extra=extra or None)

        return self._parse_result(resp)

    @staticmethod
    def _hint_for(code):
        if code in _ERROR_HINTS:
            return _ERROR_HINTS[code]
        # 形如 LimitExceeded.QpsLimitExceeded 的变体也命中最上层提示
        for key, hint in _ERROR_HINTS.items():
            if code.startswith(key + "."):
                return hint
        return ""

    def _send(self, req):
        """返回 (Response字典, 错误信息, 错误类型, 是否可重试)"""
        try:
            with request.urlopen(req, timeout=self.timeout, context=self._ssl_context()) as f:
                raw = f.read().decode("utf-8")
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            detail = detail or str(e.reason)
            retryable = e.code >= 500 or e.code == 429
            return {}, f"HTTP {e.code}: {detail}", "http", retryable
        except URLError as e:
            return {}, f"网络异常: {str(e.reason)}", "network", True
        except Exception as e:
            return {}, f"请求失败: {str(e)}", "request", False

        try:
            data = json.loads(raw)
        except Exception:
            return {}, f"响应不是合法 JSON: {raw[:200]}", "api", False
        resp = data.get("Response")
        if not isinstance(resp, dict):
            return {}, f"响应缺少 Response 字段: {raw[:200]}", "api", False
        return resp, None, "", False

    def _is_retryable_code(self, code):
        return any(code == c or code.startswith(c + ".") for c in _RETRYABLE_CODES)

    @staticmethod
    def _error_kind(code):
        if code.startswith(("AuthFailure", "UnauthorizedOperation")):
            return "auth"
        if code.startswith(("LimitExceeded", "RequestLimitExceeded")):
            return "api"
        if code.startswith("InvalidParameter"):
            return "input"
        return "api"

    # ---------- 入口 ----------

    def runPath(self, img_path: str):
        try:
            with open(img_path, "rb") as f:
                image_bytes = f.read()
        except Exception as e:
            return self._err(f"读取图片失败: {str(e)}", kind="io")
        return self._run_bytes(image_bytes)

    def runBytes(self, image_bytes):
        return self._run_bytes(image_bytes)

    def runBase64(self, image_base64: str):
        if not isinstance(image_base64, str):
            return self._err("图像数据无效", kind="input")
        return self._ocr_request(image_base64)

    def _run_bytes(self, image_bytes):
        try:
            image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        except Exception as e:
            return self._err(f"图片编码失败: {str(e)}", kind="encode")
        return self._ocr_request(image_base64)

    # ---------- 结果解析 ----------

    def _parse_result(self, result):
        extra = {}
        if "RequestId" in result:
            extra["request_id"] = result["RequestId"]
        if result.get("DeprecatedWarning"):
            extra["deprecated"] = result["DeprecatedWarning"]

        if self.api_type in _TEXT_ACTIONS:
            data = []
            for index, item in enumerate(result.get("TextDetections") or []):
                text = item.get("DetectedText", "")
                polygon = item.get("Polygon") or []
                box = [[pt.get("X", 0), pt.get("Y", 0)] for pt in polygon if isinstance(pt, dict)]
                if len(box) < 3:
                    # 没有坐标时退回等距堆叠，保证上层排版解析不会拿到空 box
                    top = index * 40
                    box = [[0, top], [200, top], [200, top + 40], [0, top + 40]]
                score = item.get("Confidence", 1.0)
                data.append({"text": text, "box": box, "score": score})
            if data:
                return {"code": 100, "data": data, "extra": extra}
            return {"code": 101, "data": "", "extra": extra}

        if self.api_type in _TABLE_ACTIONS:
            return self._parse_table(result, extra)

        return self._err(f"不支持的接口: {self.api_type}", kind="api")

    # --- 表格 ---

    def _parse_table(self, result, extra):
        detections = result.get("TableDetections")
        if detections is None:
            detections = result.get("TextDetections")  # 兼容早期返回结构
        detections = detections or []

        extra["angle"] = result.get("Angle", 0)
        excel = result.get("Data") or ""
        if excel:
            extra["excel_base64"] = excel

        # 把 V1（扁平单元格列表）与 V2（每张表带 Cells）统一成“多张表 → 单元格列表”
        tables = []
        has_nested = any(
            isinstance(det, dict) and isinstance(det.get("Cells"), list) for det in detections
        )
        if not has_nested:
            # V1：顶层就是一张表的所有单元格
            tables.append([det for det in detections if isinstance(det, dict)])
        else:
            for det in detections:
                if not isinstance(det, dict):
                    continue
                cells = det.get("Cells")
                if isinstance(cells, list):
                    merged = [c for c in cells if isinstance(c, dict)]
                    titles = det.get("Titles")
                    if isinstance(titles, list):
                        merged.extend(t for t in titles if isinstance(t, dict))
                    tables.append(merged)
                else:
                    # 混杂结构：缺少 Cells 的项按单张表处理
                    tables.append([det])

        texts = []
        all_boxes = []
        for cells in tables:
            text, boxes = self._table_to_text(cells)
            if text.strip():
                texts.append(text)
            all_boxes.extend(boxes)
        extra["table_count"] = len(texts)

        if not texts:
            return {"code": 101, "data": "", "extra": extra}

        if all_boxes:
            box = self._union_box(all_boxes)
        else:
            box = [[0, 0], [1, 0], [1, 1], [0, 1]]

        return {
            "code": 100,
            "data": [{"text": "\n\n".join(texts), "box": box, "score": 1.0}],
            "extra": extra,
        }

    def _table_to_text(self, cells):
        """单元格列表 → 制表符分隔的文本 + 单元格坐标列表"""
        boxes = []
        structured = []
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            polygon = cell.get("Polygon") or cell.get("polygon")
            if isinstance(polygon, list):
                points = []
                for pt in polygon:
                    if isinstance(pt, dict):
                        x = pt.get("X", pt.get("x"))
                        y = pt.get("Y", pt.get("y"))
                        if x is not None and y is not None:
                            points.append([x, y])
                if len(points) >= 3:
                    boxes.append(points)
            if cell.get("RowTl", -1) is not None and cell.get("ColTl", -1) is not None:
                try:
                    if int(cell.get("RowTl", -1)) >= 0 and int(cell.get("ColTl", -1)) >= 0:
                        structured.append(cell)
                except (TypeError, ValueError):
                    pass

        if not structured:
            # 无线表格：没有行列号，只能按坐标顺序拼接
            ordered = sorted(cells, key=self._cell_sort_key)
            text = "\n".join(
                (c.get("Text") or c.get("text") or "").strip()
                for c in ordered
                if (c.get("Text") or c.get("text") or "").strip()
            )
            return text, boxes

        grid = self._grid_from_cells(structured)
        return self._grid_to_text(grid), boxes

    def _grid_from_cells(self, cells):
        try:
            max_row = max(int(c.get("RowBr", 0)) for c in cells)
            max_col = max(int(c.get("ColBr", 0)) for c in cells)
        except (TypeError, ValueError):
            return []
        if max_row <= 0 or max_col <= 0:
            return []
        grid = [["" for _ in range(max_col)] for _ in range(max_row)]
        for cell in cells:
            try:
                row = int(cell.get("RowTl", -1))
                col = int(cell.get("ColTl", -1))
            except (TypeError, ValueError):
                continue
            text = (cell.get("Text") or cell.get("text") or "").strip()
            if 0 <= row < max_row and 0 <= col < max_col and grid[row][col] == "":
                grid[row][col] = text
        return grid

    @staticmethod
    def _grid_to_text(grid):
        """保留列位置，只裁掉行尾的空单元格，避免合并单元格把后面的列挤到左边"""
        lines = []
        for row in grid:
            cells = [str(c) if c is not None else "" for c in row]
            while cells and not cells[-1].strip():
                cells.pop()
            if cells:
                lines.append("\t".join(cells))
        return "\n".join(lines)

    @staticmethod
    def _union_box(boxes):
        xs = [p[0] for box in boxes for p in box]
        ys = [p[1] for box in boxes for p in box]
        return [[min(xs), min(ys)], [max(xs), min(ys)], [max(xs), max(ys)], [min(xs), max(ys)]]

    @staticmethod
    def _cell_sort_key(cell):
        row = cell.get("RowTl", -1)
        col = cell.get("ColTl", -1)
        try:
            row = int(row)
            col = int(col)
            if row >= 0 and col >= 0:
                return (0, row, col, 0, 0)
        except (TypeError, ValueError):
            pass

        polygon = cell.get("Polygon") or cell.get("polygon")
        if isinstance(polygon, list) and polygon:
            pt = polygon[0]
            if isinstance(pt, dict):
                return (1, pt.get("Y", pt.get("y", 0)), pt.get("X", pt.get("x", 0)), 0, 0)
        return (1, 0, 0, 0, 0)
