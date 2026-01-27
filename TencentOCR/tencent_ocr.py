import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from urllib import request
from urllib.error import URLError, HTTPError
import ssl


class Api:
    def __init__(self, global_argd):
        self.secret_id = global_argd.get("secret_id", "").strip()
        self.secret_key = global_argd.get("secret_key", "").strip()
        self.timeout = global_argd.get("timeout", 10)
        self.ssl_verify = global_argd.get("ssl_verify", True)
        self.host = None
        self.endpoint = None
        self.region = "ap-guangzhou"
        self.version = None

    def start(self, argd):
        self.pdf_page_number = 1  # 安全默认值，即使不用
        supported_apis = [
            "GeneralBasicOCR",
            "GeneralAccurateOCR",
            "GeneralHandwritingOCR",
            "GeneralEfficientOCR",
            "GeneralFastOCR",
            "FormulaOCR",
            "RecognizeTableOCR" ,
            "RecognizeTableAccurateOCR",
            "TableOCR",
            "EnglishOCR" ,
            "AdvertiseOCR"
        ]

        self.api_type = argd.get("api_type", "GeneralBasicOCR")
        if self.api_type not in supported_apis:
            return f"不支持的接口: {self.api_type}"

        if not self.secret_id:
            return "[Error] SecretId 不能为空，请在插件设置中填写。"
        if not self.secret_key:
            return "[Error] SecretKey 不能为空，请在插件设置中填写。"

        self.region = argd.get("region", self.region)
        self.ssl_verify = argd.get("ssl_verify", self.ssl_verify)

        self.is_pdf = argd.get("is_pdf", False)
        pdf_page_number = argd.get("pdf_page_number", 1)
        try:
            self.pdf_page_number = max(1, int(pdf_page_number))
        except Exception:
            self.pdf_page_number = 1
        self.table_language = argd.get("table_language", "zh").strip().lower()

        # GeneralBasicOCR 特有参数
        if self.api_type == "GeneralBasicOCR":
            user_lang = argd.get("language_type", "auto").strip().lower()
            valid_language_types = {
                'auto': 'auto', 'zh': 'zh', 'chinese': 'zh', 'zh_rare': 'zh_rare',
                'mix': 'mix', 'jap': 'jap', 'ja': 'jap', 'jp': 'jap', 'japanese': 'jap',
                'kor': 'kor', 'ko': 'kor', 'korean': 'kor', 'spa': 'spa', 'spanish': 'spa',
                'fre': 'fre', 'fr': 'fre', 'french': 'fre', 'ger': 'ger', 'de': 'ger',
                'german': 'ger', 'por': 'por', 'pt': 'por', 'portuguese': 'por',
                'vie': 'vie', 'vietnamese': 'vie', 'may': 'may', 'malay': 'may',
                'rus': 'rus', 'ru': 'rus', 'russian': 'rus', 'ita': 'ita',
                'it': 'ita', 'italian': 'ita', 'hol': 'hol', 'dutch': 'hol', 'swe': 'swe',
                'swedish': 'swe', 'fin': 'fin', 'finnish': 'fin', 'dan': 'dan',
                'danish': 'dan', 'nor': 'nor', 'norwegian': 'nor', 'hun': 'hun',
                'hungarian': 'hun', 'tha': 'tha', 'thai': 'tha', 'hi': 'hi',
                'hindi': 'hi', 'ara': 'ara', 'arabic': 'ara', 'en': 'auto', 'english': 'auto'
            }
            self.language_type = valid_language_types.get(user_lang, 'auto')
        else:
            self.language_type = "auto"

        # RecognizeTableOCR 特有参数
        if self.api_type == "RecognizeTableOCR":
            self.table_language = "jap" if self.table_language in ("jap", "japanese") else "zh"

        # RecognizeTableAccurateOCR 特有参数
        if self.api_type == "RecognizeTableAccurateOCR":
            # PDF 时传页码，图片则不传
            pass
            
        # 在 RecognizeTableAccurateOCR 之后添加：
        if self.api_type == "TableOCR":
            # TableOCR 无特殊参数，仅需图片
            pass
        timeout = argd.get("timeout")
        if timeout is not None:
            self.timeout = timeout
        elif self.api_type == "TableOCR":
            self.timeout = max(self.timeout, 30)

        config = self._get_api_config()
        if not config:
            return f"不支持的接口: {self.api_type}"

        self.host = config["host"]
        self.endpoint = config["endpoint"]
        self.version = config["version"]

        return ""

    def stop(self):
        pass

    def _err(self, message, kind="request", code=102):
        if not message:
            message = "未知错误"
        if message.startswith("[Error]"):
            data = message
            clean = message[len("[Error]"):].strip()
        else:
            data = f"[Error] {message}"
            clean = message
        return {"code": code, "data": data, "error": {"message": clean, "type": kind}}

    def _get_ssl_context(self):
        if self.ssl_verify:
            return None
        return ssl._create_unverified_context()

    def _grid_from_cells(self, cells):
        if not cells:
            return []
        max_row = max(cell.get("RowBr", 0) for cell in cells)
        max_col = max(cell.get("ColBr", 0) for cell in cells)
        if max_row <= 0 or max_col <= 0:
            return []
        grid = [["" for _ in range(max_col)] for _ in range(max_row)]
        for cell in cells:
            row_tl = cell.get("RowTl", -1)
            col_tl = cell.get("ColTl", -1)
            text = cell.get("Text", "")
            if 0 <= row_tl < max_row and 0 <= col_tl < max_col:
                if grid[row_tl][col_tl] == "":
                    grid[row_tl][col_tl] = text
        return grid

    def _grid_to_text(self, grid):
        lines = []
        for row in grid:
            line = "\t".join(str(cell) for cell in row if cell)
            if line.strip():
                lines.append(line)
        return "\n".join(lines)

    def _safe_table_box(self, cells):
        min_x = None
        min_y = None
        max_x = None
        max_y = None

        def add_point(x, y):
            nonlocal min_x, min_y, max_x, max_y
            if x is None or y is None:
                return
            if min_x is None or x < min_x:
                min_x = x
            if min_y is None or y < min_y:
                min_y = y
            if max_x is None or x > max_x:
                max_x = x
            if max_y is None or y > max_y:
                max_y = y

        for cell in cells or []:
            polygon = cell.get("Polygon") or cell.get("polygon")
            if isinstance(polygon, list):
                for pt in polygon:
                    if isinstance(pt, dict):
                        add_point(pt.get("X", pt.get("x")), pt.get("Y", pt.get("y")))
            rect = cell.get("Rect") or cell.get("rect") or cell.get("Location") or cell.get("location")
            if isinstance(rect, dict):
                x = rect.get("X", rect.get("x"))
                y = rect.get("Y", rect.get("y"))
                w = rect.get("Width", rect.get("width"))
                h = rect.get("Height", rect.get("height"))
                if x is not None and y is not None and w is not None and h is not None:
                    add_point(x, y)
                    add_point(x + w, y + h)

        if min_x is None or min_y is None or max_x is None or max_y is None:
            return [[0, 0], [1, 0], [1, 1], [0, 1]]
        return [[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]]

    def _get_api_config(self):
        common_config = {
            "version": "2018-11-19",
            "endpoint": "https://ocr.tencentcloudapi.com",
            "host": "ocr.tencentcloudapi.com"
        }
        config_map = {
            "GeneralBasicOCR": common_config,
            "GeneralAccurateOCR": common_config,
            "GeneralHandwritingOCR": common_config,
            "GeneralEfficientOCR": common_config,
            "GeneralFastOCR": common_config,
            "FormulaOCR": common_config,
            "RecognizeTableOCR": common_config,
            "RecognizeTableAccurateOCR": common_config,
            "TableOCR": common_config,
            "EnglishOCR": common_config,
            "AdvertiseOCR":common_config,
        }
        return config_map.get(self.api_type)

    def _build_sign(self, params):
        version = self.version
        algorithm = "TC3-HMAC-SHA256"
        service = "ocr"
        timestamp = int(time.time())
        date = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")

        payload = {}
        if params.get("image_base64"):
            payload["ImageBase64"] = params["image_base64"]

        # GeneralBasicOCR 参数
        if self.api_type == "GeneralBasicOCR":
            payload["LanguageType"] = self.language_type
        # RecognizeTableOCR 参数
        elif self.api_type == "RecognizeTableOCR":
            payload["IsPdf"] = self.is_pdf
            if self.is_pdf:
                payload["PdfPageNumber"] = self.pdf_page_number
            payload["TableLanguage"] = self.table_language
        # RecognizeTableAccurateOCR 参数
        elif self.api_type == "RecognizeTableAccurateOCR":
            if self.is_pdf:
                payload["PdfPageNumber"] = self.pdf_page_number
        # TableOCR：仅需 ImageBase64，无其他参数
        elif self.api_type == "TableOCR":
            # payload 已包含 ImageBase64，无需额外操作
            pass
        elif self.api_type == "EnglishOCR":
            payload["EnableCoordPoint"] = params.get("enable_coord_point", False)
            payload["EnableCandWord"] = params.get("enable_cand_word", False)
            payload["Preprocess"] = params.get("preprocess", True)

        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        hashed_payload = hashlib.sha256(body.encode("utf-8")).hexdigest()

        canonical_uri = "/"
        canonical_querystring = ""
        canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{self.host}\n"
        signed_headers = "content-type;host"
        canonical_request = (
            f"POST\n"
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

        return auth_header, body, timestamp, version

    def _ocr_request(self, image_base64):
        if not image_base64 or len(image_base64) < 10:
            return self._err("图像数据为空或无效", kind="input")

        try:
            params = {"image_base64": image_base64}
            auth_header, body, timestamp, version = self._build_sign(params)

            req = request.Request(self.endpoint, data=body.encode("utf-8"), method="POST")
            req.add_header("Authorization", auth_header)
            req.add_header("Content-Type", "application/json; charset=utf-8")
            req.add_header("Host", self.host)
            req.add_header("X-TC-Action", self.api_type)
            req.add_header("X-TC-Version", version)
            req.add_header("X-TC-Region", self.region)
            req.add_header("X-TC-Timestamp", str(timestamp))

            with request.urlopen(req, timeout=self.timeout, context=self._get_ssl_context()) as f:
                response = json.loads(f.read().decode("utf-8"))

            resp = response.get("Response", {})
            if "Error" in resp:
                err = resp["Error"]
                return self._err(f"{err.get('Code')}: {err.get('Message')}", kind="api")

            return self._parse_result(resp)

        except HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")
            except Exception:
                pass
            detail = body or str(e.reason)
            return self._err(f"HTTP {e.code}: {detail}", kind="http")
        except URLError as e:
            return self._err(f"网络异常: {str(e.reason)}", kind="network")
        except Exception as e:
            return self._err(f"请求失败: {str(e)}", kind="request")

    def runPath(self, img_path: str):
        try:
            with open(img_path, "rb") as f:
                image_bytes = f.read()
            image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        except Exception as e:
            return self._err(f"读取图片失败: {str(e)}", kind="io")
        return self._ocr_request(image_base64)

    def runBytes(self, image_bytes):
        try:
            image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        except Exception as e:
            return self._err(f"图片编码失败: {str(e)}", kind="encode")
        return self._ocr_request(image_base64)

    def runBase64(self, image_base64: str):
        return self._ocr_request(image_base64)

    def _parse_result(self, result):
        # 通用 OCR 接口（除 FormulaOCR 和 RecognizeTableOCR）
        if self.api_type in [
            "GeneralBasicOCR",
            "GeneralAccurateOCR",
            "GeneralHandwritingOCR",
            "GeneralEfficientOCR",
            "GeneralFastOCR",
            "EnglishOCR",
            "AdvertiseOCR"
        ]:
            data = []
            for item in result.get("TextDetections", []):
                text = item.get("DetectedText", "")
                box = [[pt.get("X", 0), pt.get("Y", 0)] for pt in item.get("Polygon", [])]
                score = item.get("Confidence", 1.0)
                data.append({"text": text, "box": box, "score": score})
            return {"code": 100, "data": data} if data else {"code": 101, "data": ""}

        elif self.api_type == "FormulaOCR":
            formula_list = result.get("FormulaInfos", [])
            request_id = result.get("RequestId", "")
            data = []

            for item in formula_list:
                text = item.get("DetectedText", "").strip()
                if not text:
                    continue
                confidence = item.get("Confidence", 1.0)
                x = item.get("X", 0)
                y = item.get("Y", 0)
                w = item.get("Width", 0)
                h = item.get("Height", 0)

                x1, y1 = x, y
                x2, y2 = x + max(w, 1), y + max(h, 1)
                box = [
                    [x1, y1],
                    [x2, y1],
                    [x2, y2],
                    [x1, y2]
                ]

                data.append({
                    "text": text,
                    "box": box,
                    "score": confidence,
                    "angle": result.get("Angle", 0)
                })

            if data:
                return {"code": 100, "data": data, "extra": {"request_id": request_id}}
            else:
                return {"code": 101, "data": "", "extra": {"request_id": request_id}}

        elif self.api_type in ["RecognizeTableOCR", "RecognizeTableAccurateOCR"]:
            tables = []
            raw_cells_list = []
            for table in result.get("TableDetections", []):
                cells = table.get("Cells", [])
                raw_cells_list.append(cells)
                tables.append(self._grid_from_cells(cells))

            # 将表格转为纯文本（用于兼容上层 text/score 结构）
            table_texts = []
            for grid in tables:
                table_texts.append(self._grid_to_text(grid))

            # 构造兼容格式：每个表格作为一个“文本块”
            data = []
            for i, text in enumerate(table_texts):
                if text.strip():
                    box = self._safe_table_box(raw_cells_list[i] if i < len(raw_cells_list) else [])
                    data.append({
                        "text": text,
                        "box": box,
                        "score": 1.0,       # 表格无置信度，设为 1.0
                        "type": "table",    # 新增类型标记！
                        "table_data": tables[i]  # 保留结构化数据
                    })

            excel_b64 = result.get("Data", "")
            angle = result.get("Angle", 0)
            request_id = result.get("RequestId", "")

            return {
                "code": 100,
                "data": data,
                "extra": {
                    "excel_base64": excel_b64,
                    "angle": angle,
                    "request_id": request_id,
                    "raw_cells": raw_cells_list
                }
            }

        elif self.api_type == "TableOCR":
            # 旧版 TableOCR 解析
            cells = result.get("TextDetections", [])
            raw_cells_list = [cells]  # 为了和新版结构对齐，包装成列表

            if not cells:
                return {
                    "code": 101,
                    "data": "",
                    "extra": {
                        "excel_base64": "",
                        "request_id": result.get("RequestId", "")
                    }
                }

            # 过滤结构化单元格（RowTl/ColTl >= 0）
            structured_cells = [c for c in cells if c.get("RowTl", -1) >= 0 and c.get("ColTl", -1) >= 0]

            if not structured_cells:
                # 全是非结构化文本，按顺序拼接
                text = "\n".join(c.get("Text", "") for c in cells if c.get("Text", "").strip())
                box = self._safe_table_box(cells)
                return {
                    "code": 100,
                    "data": [{"text": text, "box": box, "score": 1.0, "type": "table"}],
                    "extra": {
                        "excel_base64": result.get("Data", ""),
                        "request_id": result.get("RequestId", "")
                    }
                }

            grid = self._grid_from_cells(structured_cells)
            table_text = self._grid_to_text(grid)

            data = []
            if table_text.strip():
                box = self._safe_table_box(structured_cells)
                data.append({
                    "text": table_text,
                    "box": box,
                    "score": 1.0,
                    "type": "table",
                    "table_data": grid
                })

            return {
                "code": 100,
                "data": data,
                "extra": {
                    "excel_base64": result.get("Data", ""),
                    "request_id": result.get("RequestId", ""),
                    "raw_cells": raw_cells_list
                }
            }

        else:
            return self._err(f"不支持的接口: {self.api_type}", kind="api")