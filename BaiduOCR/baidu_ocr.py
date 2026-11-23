# baidu_ocr.py
# 百度智能云 OCR 插件。调用链：
#   oauth 拿 access_token（进程内缓存复用） -> POST 识别接口 -> 解析成 Umi-OCR 的
#   {"code":100, "data":[{"text","box","score"}]} 结构。
import base64
import json
import ssl
import struct
import time
from urllib import request, parse
from urllib.error import URLError, HTTPError

# access_token 进程内缓存：key -> {"token": str, "expire": float}
# Umi-OCR 每次任务都会调用 start()，若每次都去 oauth，会额外增加一次往返，
# 且百度对 token 接口本身也有频率限制，所以缓存到进程级。
_TOKEN_CACHE = {}

# 各接口支持的请求参数。只发送接口文档里明确支持的参数，
# 避免把 language_type 之类塞给不支持的接口（如高精度版 / 网络图片版）。
_API_PARAMS = {
    "general_basic": ("language_type", "detect_direction", "paragraph"),
    "general": ("language_type", "detect_direction", "paragraph"),
    "accurate_basic": ("detect_direction", "paragraph"),
    "accurate": ("detect_direction", "paragraph"),
    "webimage": ("detect_direction",),
    "numbers": (),
    "handwriting": (),
    "formula": (),
    "table": ("return_excel", "cell_contents"),
}

# 无需位置坐标（words_result 里没有 location）的接口
_NO_LOCATION_APIS = ("general_basic", "accurate_basic", "webimage")

# 常见错误码提示
_ERROR_HINTS = {
    4: "集群超限额",
    6: "没有该接口的调用权限，请检查应用是否开通对应服务",
    14: "IAM 鉴权失败",
    17: "今日请求量已达上限（免费额度用尽或配额不足）",
    18: "请求过于频繁（QPS 超限），请降低并发或稍后重试",
    100: "参数错误",
    110: "access_token 无效",
    111: "access_token 已过期",
    216200: "图片为空",
    216201: "图片格式错误",
    216202: "图片大小超出限制（base64 后需 ≤4MB）",
    216203: "下载图片失败",
    216630: "识别失败",
    282000: "服务端内部错误，请重试",
}

# token 失效错误码，触发一次重新鉴权 + 重试
_TOKEN_ERRORS = (110, 111)

# 临时性错误码（QPS 超限 / 服务端抖动），退避后重试一次
_TRANSIENT_ERRORS = (18, 282000)


class Api:
    def __init__(self, global_argd):
        self.api_key = global_argd.get("api_key", "").strip()
        self.secret_key = global_argd.get("secret_key", "").strip()
        self.timeout = self._as_number(global_argd.get("timeout"), 10)
        self.ssl_verify = global_argd.get("ssl_verify", True)
        self.debug_log = global_argd.get("debug_log", False)

        self.access_token = None
        self._last_error = ""
        self._img_size = None       # 当前图片的 (宽, 高)，拿不到就是 None
        self.api_type = "general"
        self.language_type = "CHN_ENG"
        self.detect_direction = False
        self.paragraph = False
        self.return_excel = False
        self.cell_contents = False

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
            print(f"[BaiduOCR] {message}")

    def _post(self, url, payload, content_type="application/x-www-form-urlencoded"):
        """发送 POST，返回 (json字典 | None, 错误信息 | None, 错误类型)"""
        data = payload if isinstance(payload, bytes) else parse.urlencode(payload).encode("utf-8")
        try:
            req = request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", content_type)
            with request.urlopen(req, timeout=self.timeout, context=self._ssl_context()) as f:
                body = f.read().decode("utf-8")
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            detail = detail or str(e.reason)
            # 百度鉴权/参数类错误也可能以 4xx 返回，尝试解析出结构化错误
            parsed = self._try_json(detail)
            if isinstance(parsed, dict) and parsed.get("error_code") is not None:
                return parsed, None, ""
            return None, f"HTTP {e.code}: {detail}", "http"
        except URLError as e:
            return None, f"网络异常: {str(e.reason)}", "network"
        except Exception as e:
            return None, f"请求失败: {str(e)}", "request"

        parsed = self._try_json(body)
        if parsed is None:
            return None, f"响应不是合法 JSON: {body[:200]}", "api"
        return parsed, None, ""

    @staticmethod
    def _try_json(text):
        try:
            return json.loads(text)
        except Exception:
            return None

    # ---------- 图片尺寸探测（仅用于伪坐标，不依赖 Pillow） ----------

    @staticmethod
    def _probe_size(image_bytes):
        if not image_bytes or len(image_bytes) < 24:
            return None
        try:
            if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", image_bytes[16:24])
                return (int(w), int(h)) if w and h else None
            if image_bytes[:2] == b"\xff\xd8":  # JPEG，逐段找 SOF
                i = 2
                n = len(image_bytes)
                while i + 9 < n:
                    if image_bytes[i] != 0xFF:
                        i += 1
                        continue
                    marker = image_bytes[i + 1]
                    if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                        i += 2
                        continue
                    seg_len = struct.unpack(">H", image_bytes[i + 2:i + 4])[0]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                  0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        h, w = struct.unpack(">HH", image_bytes[i + 5:i + 9])
                        return (int(w), int(h)) if w and h else None
                    i += 2 + seg_len
                return None
            if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", image_bytes[6:10])
                return (int(w), int(h)) if w and h else None
            if image_bytes[:2] == b"BM" and len(image_bytes) >= 26:
                w, h = struct.unpack("<ii", image_bytes[18:26])
                return (abs(int(w)), abs(int(h)))
        except Exception:
            return None
        return None

    def _remember_size(self, image_bytes):
        size = self._probe_size(image_bytes)
        if size:
            self._img_size = size
        return size

    # ---------- 鉴权 ----------

    def _cache_key(self):
        return f"{self.api_key}|{self.secret_key}"

    def _cached_token(self):
        item = _TOKEN_CACHE.get(self._cache_key())
        if item and item.get("token") and item.get("expire", 0) > time.time():
            return item["token"]
        return None

    def _drop_token(self):
        _TOKEN_CACHE.pop(self._cache_key(), None)
        self.access_token = None

    def _get_access_token(self):
        cached = self._cached_token()
        if cached:
            self._last_error = ""
            return cached

        url = "https://aip.baidubce.com/oauth/2.0/token"
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key,
        }
        result, err, kind = self._post(url, payload)
        if err:
            self._last_error = f"[Error] 获取 access_token 失败: {err}"
            self._debug(self._last_error)
            return None
        token = result.get("access_token") if isinstance(result, dict) else None
        if not token:
            detail = ""
            if isinstance(result, dict):
                code = result.get("error")
                desc = result.get("error_description") or result.get("error_msg") or ""
                detail = f"{code} {desc}".strip()
            self._last_error = f"[Error] 获取 access_token 失败: {detail or result}"
            self._debug(self._last_error)
            return None

        try:
            expires_in = int(result.get("expires_in") or 2592000)
        except (TypeError, ValueError):
            expires_in = 2592000
        # 提前 10 分钟过期，避免临界失效
        _TOKEN_CACHE[self._cache_key()] = {
            "token": token,
            "expire": time.time() + max(60, expires_in - 600),
        }
        self._last_error = ""
        return token

    # ---------- 生命周期 ----------

    def start(self, argd):
        new_api_key = argd.get("api_key", "").strip()
        new_secret_key = argd.get("secret_key", "").strip()
        if new_api_key:
            self.api_key = new_api_key
        if new_secret_key:
            self.secret_key = new_secret_key

        self.timeout = self._as_number(argd.get("timeout", self.timeout), self.timeout)
        self.ssl_verify = argd.get("ssl_verify", self.ssl_verify)
        self.debug_log = argd.get("debug_log", self.debug_log)

        if not self.api_key:
            return "[Error] API Key 不能为空，请在插件设置中填写。"
        if not self.secret_key:
            return "[Error] Secret Key 不能为空，请在插件设置中填写。"

        api_type = argd.get("api_type", "general")
        if api_type not in _API_PARAMS:
            return f"[Error] 不支持的接口: {api_type}"
        self.api_type = api_type

        self.language_type = argd.get("language_type", "CHN_ENG")
        self.detect_direction = bool(argd.get("detect_direction", False))
        self.paragraph = bool(argd.get("paragraph", False))
        self.return_excel = bool(argd.get("return_excel", False))
        self.cell_contents = bool(argd.get("cell_contents", False))

        self.access_token = self._get_access_token()
        if not self.access_token:
            return self._last_error or "[Error] 无法获取 access_token，请检查密钥或网络。"

        return ""

    def stop(self):
        # token 保留在进程缓存里，stop 只清理本次的临时状态
        self._img_size = None

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
        self._remember_size(image_bytes)
        try:
            image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        except Exception as e:
            return self._err(f"图片编码失败: {str(e)}", kind="encode")
        return self._ocr_request(image_base64)

    # ---------- 请求 ----------

    def _build_payload(self, image_base64):
        payload = {"image": image_base64}
        if self.api_type in ("general_basic", "general", "accurate_basic", "accurate"):
            payload["detect_direction"] = "true" if self.detect_direction else "false"
            if "language_type" in _API_PARAMS[self.api_type]:
                payload["language_type"] = self.language_type
            if "paragraph" in _API_PARAMS[self.api_type]:
                payload["paragraph"] = "true" if self.paragraph else "false"
        elif self.api_type == "webimage":
            payload["detect_direction"] = "true" if self.detect_direction else "false"
        elif self.api_type == "table":
            if self.return_excel:
                payload["return_excel"] = "true"
            if self.cell_contents:
                payload["cell_contents"] = "true"
        return payload

    def _ocr_request(self, image_base64, allow_retry=True):
        if not image_base64 or len(image_base64) < 10:
            return self._err("图像数据为空或无效", kind="input")
        if not self.access_token:
            return self._err("access_token 未获取，请重新开始识别", kind="auth", code=102)

        url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/{self.api_type}"
        result, err, kind = self._post(
            f"{url}?access_token={self.access_token}", self._build_payload(image_base64)
        )
        if err:
            return self._err(err, kind=kind)

        if isinstance(result, dict):
            code = result.get("error_code")
            if code is not None:
                # token 失效：刷新一次后重试（仅一次）
                if code in _TOKEN_ERRORS and allow_retry:
                    self._debug(f"token 失效({code})，尝试重新鉴权")
                    self._drop_token()
                    token = self._get_access_token()
                    if token:
                        self.access_token = token
                        return self._ocr_request(image_base64, allow_retry=False)
                # 限流 / 服务端抖动：退避一次
                if code in _TRANSIENT_ERRORS and allow_retry:
                    self._debug(f"临时性错误({code})，0.6s 后重试一次")
                    time.sleep(0.6)
                    return self._ocr_request(image_base64, allow_retry=False)
                return self._api_error(result)

        return self._parse_result(result)

    def _api_error(self, result):
        code = result.get("error_code")
        msg = result.get("error_msg") or "未知错误"
        hint = _ERROR_HINTS.get(code)
        text = f"百度错误 {code}: {msg}"
        if hint and hint not in str(msg):
            text += f"（{hint}）"
        kind = "auth" if code in _TOKEN_ERRORS + (14,) else "api"
        return self._err(text, kind=kind, extra={"log_id": result.get("log_id")})

    # ---------- 结果解析 ----------

    def _parse_result(self, result):
        if not isinstance(result, dict):
            return self._err("响应格式异常", kind="api")
        if "error_code" in result:
            return self._api_error(result)

        extra = {}
        for key in ("log_id", "words_result_num"):
            if key in result:
                extra[key] = result[key]

        if self.api_type == "table":
            return self._parse_table(result, extra)

        items = result.get("words_result") or []
        if not items:
            return {"code": 101, "data": "", "extra": extra}

        data = self._boxes_from_words(items)
        if not data:
            return {"code": 101, "data": "", "extra": extra}
        return {"code": 100, "data": data, "extra": extra}

    def _boxes_from_words(self, items):
        """把 words_result 转成 [{text, box, score}]。
        有 location 的接口用真实坐标；没有的接口（标准版/高精度版/网络图片）
        按图片高度均分生成伪坐标，至少保证排版顺序正确、叠加层落在图片范围内。
        """
        data = []
        located = []
        plain = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("words", "")
            if text is None:
                text = ""
            loc = item.get("location")
            if isinstance(loc, dict) and all(k in loc for k in ("left", "top", "width", "height")):
                located.append((text, loc))
            else:
                plain.append(text)

        for text, loc in located:
            left = loc["left"]
            top = loc["top"]
            width = loc["width"]
            height = loc["height"]
            box = [
                [left, top],
                [left + width, top],
                [left + width, top + height],
                [left, top + height],
            ]
            data.append({"text": text, "box": box, "score": 1.0})

        if plain:
            data.extend(self._pseudo_boxes(plain, start_index=len(data)))

        # 不重新排序：百度返回的 words_result 本身是阅读顺序，
        # 上层（Umi 的排版解析）会依据坐标自行归并，这里保持原序最稳。
        return data

    def _pseudo_boxes(self, texts, start_index=0):
        n = len(texts)
        if not n:
            return []
        width = height = None
        if self._img_size:
            width, height = self._img_size

        result = []
        if width and height:
            # 按行均分图片高度，宽度铺满：足够让版式解析按行归并
            line_h = max(1, int(round(float(height) / n)))
            for i, text in enumerate(texts):
                y = min(height - 1, i * line_h) if height > 1 else 0
                bottom = min(height, y + line_h)
                box = [[0, y], [width, y], [width, bottom], [0, bottom]]
                result.append({"text": text, "box": box, "score": 1.0})
            return result

        # 拿不到图片尺寸时退回等距堆叠（旧行为）
        line_h = 40
        for i, text in enumerate(texts):
            y = (start_index + i) * line_h
            box = [[0, y], [200, y], [200, y + line_h], [0, y + line_h]]
            result.append({"text": text, "box": box, "score": 1.0})
        return result

    def _parse_table(self, result, extra):
        tables = result.get("tables_result")
        if tables is None:
            # return_excel=true 时百度会换成 {"result": {...}} 结构，
            # 里面只有 Excel 数据，没有可渲染的文本单元格
            nested = result.get("result")
            if isinstance(nested, dict):
                extra["result_data"] = nested.get("result_data") or nested
                return {"code": 101, "data": "", "extra": extra}
            return {"code": 101, "data": "", "extra": extra}

        all_texts = []
        boxes = []
        for table in tables:
            if not isinstance(table, dict):
                continue
            rows = {}
            max_row = -1
            max_col = -1
            for cell in table.get("body") or []:
                if not isinstance(cell, dict):
                    continue
                text = str(cell.get("words", "") or cell.get("text", ""))
                text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
                try:
                    row_start = int(cell.get("row_start", 0))
                    row_end = int(cell.get("row_end", row_start))
                    col_start = int(cell.get("col_start", 0))
                    col_end = int(cell.get("col_end", col_start))
                except (TypeError, ValueError):
                    continue
                max_row = max(max_row, row_end)
                max_col = max(max_col, col_end)
                rows.setdefault(row_start, {})[col_start] = text
                loc = cell.get("cell_location") or cell.get("location")
                if isinstance(loc, list) and len(loc) == 4:
                    try:
                        boxes.append([[p["x"], p["y"]] for p in loc])
                    except (KeyError, TypeError):
                        pass

            if max_row < 0 or max_col < 0:
                continue
            grid = []
            for r in range(0, max_row + 1):
                row = []
                for c in range(0, max_col + 1):
                    row.append(rows.get(r, {}).get(c, ""))
                while row and row[-1] == "":
                    row.pop()
                grid.append(row)
            text = "\n".join("\t".join(cell for cell in row) for row in grid if row)
            if text.strip():
                all_texts.append(text)

        if not all_texts:
            return {"code": 101, "data": "", "extra": extra}

        full_text = "\n\n".join(all_texts)
        if boxes:
            xs = [p[0] for box in boxes for p in box]
            ys = [p[1] for box in boxes for p in box]
            box = [[min(xs), min(ys)], [max(xs), min(ys)], [max(xs), max(ys)], [min(xs), max(ys)]]
        else:
            box = self._full_image_box()
        extra["table_count"] = len(all_texts)
        return {"code": 100, "data": [{"text": full_text, "box": box, "score": 1.0}], "extra": extra}

    def _full_image_box(self):
        if self._img_size:
            width, height = self._img_size
            return [[0, 0], [width, 0], [width, height], [0, height]]
        return [[0, 0], [1, 0], [1, 1], [0, 1]]


# 兼容旧调用：部分代码可能直接引用 runPath 之类的模块级函数（无）
