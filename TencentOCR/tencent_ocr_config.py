# tencent_ocr_config.py
from plugin_i18n import Translator

tr = Translator(__file__, "i18n.csv")

# 全局配置
globalOptions = {
    "title": tr("腾讯云 OCR"),
    "type": "group",
    "secret_id": {
        "title": tr("SecretId"),
        "default": "",
        "toolTip": tr("腾讯云 OCR 的 API Key"),
    },
    "secret_key": {
        "title": tr("SecretKey"),
        "default": "",
        "toolTip": tr("腾讯云 OCR 的 Secret Key"),
    },
    "timeout": {
        "title": tr("超时时间"),
        "type": "number",
        "default": 10,
        "min": 5,
        "max": 60,
        "unit": tr("秒"),
    },
    "ssl_verify": {
        "title": tr("SSL 验证"),
        "type": "boolean",
        "default": True,
        "toolTip": tr("是否启用 SSL 证书验证。关闭可绕过证书问题，但不安全。"),
    },
    "debug_log": {
        "title": tr("调试日志"),
        "type": "boolean",
        "default": False,
        "toolTip": tr("输出插件调试信息（仅排查问题时开启）。"),
    },
}

# 局部配置（仅保留文本 + 表格识别）
localOptions = {
    "title": tr("文字识别（腾讯云 OCR）"),
    "type": "group",
    "api_type": {
        "title": tr("OCR 接口"),
        "type": "enum",
        "optionsList": [
            ["GeneralBasicOCR", tr("通用印刷体识别")],
            ["GeneralAccurateOCR", tr("通用文字识别（高精度版）")],
            ["GeneralEfficientOCR",tr("通用印刷体识别（精简版）")],
            ["GeneralFastOCR",tr("通用印刷体识别（高速版）")],
            ["GeneralHandwritingOCR",tr("通用手写体识别")],
            ["RecognizeTableOCR",tr("表格识别 V2")],
            ["RecognizeTableAccurateOCR",tr("表格识别 V2（高精度版）")],
            ["TableOCR",tr("表格识别 V1（仅老客户可调用）")],
            ["EnglishOCR",tr("英文识别")],
            ["AdvertiseOCR",tr("广告识别")],
        ],
        "default": "GeneralAccurateOCR",
        "toolTip": tr("选择要调用的腾讯云 OCR 接口。腾讯云「公式识别」已下线，公式请改用百度插件。"),
    },
    "language_type": {
        "title": tr("语言类型"),
        "type": "enum",
        "optionsList": [
            ["auto", tr("自动检测")],
            ["zh", tr("中文")],
            ["en", tr("英文")],
            ["ja", tr("日文")],
            ["ko", tr("韩文")],
            ["ru", tr("俄文")],
            ["fr", tr("法文")],
            ["de", tr("德文")],
            ["es", tr("西班牙文")],
            ["pt", tr("葡萄牙文")],
            ["it", tr("意大利文")],
            ["hi", tr("印地文")],
        ],
        "default": "auto",
        "toolTip": tr("指定识别语言类型（部分接口支持）"),
    },
    "region": {
        "title": tr("区域"),
        "type": "enum",
        "optionsList": [
            ["ap-guangzhou", tr("广州")],
            ["ap-beijing", tr("北京")],
            ["ap-shanghai", tr("上海")],
            ["ap-hongkong", tr("中国香港")],
            ["na-siliconvalley", tr("硅谷")],
        ],
        "default": "ap-guangzhou",
        "toolTip": tr("选择最近的区域以降低延迟"),
    },
    "timeout": {
        "title": tr("超时时间"),
        "type": "number",
        "default": 10,
        "min": 5,
        "max": 60,
        "unit": tr("秒"),
        "toolTip": tr("仅对本次识别生效，优先于全局超时设置。"),
    },
    "ssl_verify": {
        "title": tr("SSL 验证"),
        "type": "boolean",
        "default": True,
        "toolTip": tr("仅对本次识别生效。关闭可绕过证书问题，但不安全。"),
    },

}