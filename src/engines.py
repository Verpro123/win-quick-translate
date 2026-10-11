# -*- coding: utf-8 -*-
"""翻译引擎：百度翻译开放平台 + OpenAI 兼容大模型接口。

两个引擎实现同一个接口，上层（气泡、托盘）不关心具体走哪条路。
"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)


class TranslateError(Exception):
    """翻译失败。message 直接展示给用户，所以要写人话。"""


# ---------------------------------------------------------------- 结果结构


@dataclass
class TranslationResult:
    """一次翻译的产物。

    structured=True 时 phonetic / definitions / examples / note 才有意义；
    否则只有 raw 一段纯文本（百度返回的就是这种，一行译文没有结构）。
    """

    raw: str = ""
    phonetic: str = ""
    definitions: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    note: str = ""
    structured: bool = False

    @property
    def text(self) -> str:
        """重组回纯文本，用于写剪贴板或记日志。"""
        if not self.structured:
            return self.raw
        parts: list[str] = []
        if self.phonetic:
            parts.append(self.phonetic)
        parts.extend(self.definitions)
        if self.examples:
            parts.append("例句：")
            parts.extend(self.examples)
        if self.note:
            parts.append(self.note)
        return "\n".join(parts)


# 模型可能用的分节标记，多给几个别名以提高容错
_SECTION_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("phonetic", ("音标", "读音", "发音")),
    ("definitions", ("释义", "翻译", "解释", "意思")),
    ("examples", ("例句", "例子", "示例")),
    ("note", ("注", "备注", "用法说明", "小贴士")),
)

_SEPARATORS = ("：", ":")


def _match_section(line: str) -> tuple[str, str] | None:
    """判断这一行是不是分节标记。是就返回 (字段名, 标记后面的内容)。"""
    for name, keys in _SECTION_KEYS:
        for key in keys:
            for sep in _SEPARATORS:
                prefix = key + sep
                if line.lower().startswith(prefix.lower()):
                    return name, line[len(prefix):].strip()
    return None


def _append_section(result: TranslationResult, name: str, value: str) -> None:
    if name == "phonetic":
        result.phonetic = f"{result.phonetic} {value}".strip()
    elif name == "definitions":
        result.definitions.append(value)
    elif name == "examples":
        result.examples.append(value)
    elif name == "note":
        result.note = f"{result.note} {value}".strip()


def parse_translation(text: str) -> TranslationResult:
    """把模型输出的分节文本解析成结构化结果。

    认不出任何标记时整体退回纯文本 —— 模型没按格式走也要让人看到内容，
    绝不能因为格式跑偏就显示空白。
    """
    result = TranslationResult(raw=text.strip())
    current: str | None = None
    matched = False
    loose: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 模型偶尔手滑加上的强调符号，顺手剥掉
        line = line.strip("*_`# ").strip()
        if not line:
            continue

        hit = _match_section(line)
        if hit is not None:
            matched = True
            current, rest = hit
            if rest:
                _append_section(result, current, rest)
            continue

        if current is None:
            loose.append(line)
        else:
            _append_section(result, current, line)

    if not matched:
        result.structured = False
        result.raw = text.strip()
        return result

    # 标记之前那些没归属的行，补进释义里
    if loose:
        result.definitions = loose + result.definitions

    result.structured = True
    return result


# ---------------------------------------------------------------- 百度

# 百度常见的错误码，附上人话解释，省得用户对着数字发愁
BAIDU_ERROR_HINTS = {
    "52001": "请求超时，网络不稳定，稍后重试",
    "52002": "百度服务端异常，稍后重试",
    "52003": "APPID 未授权：检查 APPID 是否填错，或在控制台没开通「通用文本翻译」",
    "54000": "必填参数为空",
    "54001": "签名错误：密钥填错了，注意 APPID 和密钥别搞反",
    "54003": "调用频率超限，稍等几秒再试",
    "54004": "账户余额不足",
    "54005": "短时间内重复请求过长文本",
    "58000": "客户端 IP 非法：控制台里若开了 IP 白名单，把本机公网 IP 加进去",
    "58001": "译文语言方向不支持",
    "58002": "服务已关闭，去百度翻译开放平台把服务重新开启",
    "90107": "认证未通过或未生效：确认已通过个人/企业认证",
}


class BaiduTranslator:
    """百度翻译开放平台「通用文本翻译」。

    文档：https://fanyi-api.baidu.com/doc/21
    签名规则：sign = MD5(appid + q + salt + 密钥)
    """

    name = "baidu"
    label = "百度翻译"

    ENDPOINT = "https://fanyi-api.baidu.com/api/trans/vip/translate"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.appid = str(cfg.get("appid") or "").strip()
        self.secret = str(cfg.get("secret") or "").strip()

    def ready(self) -> tuple[bool, str]:
        if not self.appid:
            return False, "还没填百度翻译的 APPID"
        if not self.secret:
            return False, "还没填百度翻译的密钥"
        return True, ""

    def translate(self, text: str, timeout: float = 8.0) -> str:
        # salt 用随机数防止被缓存，百度要求 32 位以内
        salt = str(random.randint(32768, 65536))
        sign = hashlib.md5(
            (self.appid + text + salt + self.secret).encode("utf-8")
        ).hexdigest()

        params = {
            "q": text,
            "from": "en",
            "to": "zh",
            "appid": self.appid,
            "salt": salt,
            "sign": sign,
        }

        try:
            resp = requests.get(self.ENDPOINT, params=params, timeout=timeout)
        except requests.RequestException as exc:
            raise TranslateError(f"网络不通，检查一下网络连接（{exc}）") from exc

        if resp.status_code != 200:
            raise TranslateError(f"百度返回 HTTP {resp.status_code}，稍后重试")

        try:
            data = resp.json()
        except ValueError as exc:
            raise TranslateError("百度返回了非 JSON 内容，可能是网络被劫持") from exc

        if "error_code" in data:
            code = str(data.get("error_code"))
            msg = data.get("error_msg") or "未知错误"
            hint = BAIDU_ERROR_HINTS.get(code, "")
            raise TranslateError(f"百度错误 {code}：{msg}" + (f"（{hint}）" if hint else ""))

        results = data.get("trans_result")
        if not results:
            raise TranslateError("百度没有返回译文，可能选中的不是英文")

        # 多行文本会被拆成多条结果，逐条拼回去
        merged = "\n".join(item.get("dst", "") for item in results).strip()
        # 百度只给一句译文，没有音标、词性和例句，所以不带结构
        return TranslationResult(raw=merged, structured=False)


# ---------------------------------------------------------------- AI 大模型

DEFAULT_SYSTEM_PROMPT = """你是一位专业的英汉词典编辑，服务对象是中文母语的工程师。
用户会给你一段从屏幕上选中的英文，请用简体中文输出讲解。

必须严格按下面这个固定格式输出，不要使用任何 markdown 符号（星号、井号、反引号、表格一概不要），不要寒暄，不要解释你的格式：

音标: /音标/
释义:
词性. 中文释义
例句:
英文例句 —— 中文翻译
注: 一句话补充说明

填写规则：
1. 输入是单个单词时才写「音标:」这一行；是短语或句子就把整行删掉。
2. 「释义:」每行写一个义项，行首给词性缩写（n. v. adj. adv. prep. 等）。
   短语和句子直接给中文翻译，不用写词性，可以只有一行。
3. 「例句:」最多两条，每条都必须用「 —— 」把英文和中文连起来。
   没有合适的例句，就把「例句:」这一段整段删掉。
4. 「注:」用来说明语境、常见搭配或易混词，只写一句；没有就把整行删掉。
5. 每个标记必须独占一行，标记后面紧跟冒号。
6. 全文不要出现空行。"""


class AITranslator:
    """OpenAI 兼容的 Chat Completions 接口。

    只要服务商兼容 /chat/completions，就不用改这里的代码：
    DeepSeek、通义、智谱、Kimi、SiliconFlow、本地 Ollama 都可以。
    """

    name = "ai"
    label = "AI 大模型"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.model = str(cfg.get("model") or "").strip()
        self.timeout = float(cfg.get("timeout") or 25)
        custom = str(cfg.get("prompt") or "").strip()
        self.system_prompt = custom or DEFAULT_SYSTEM_PROMPT

    def ready(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "还没填接口地址 base_url"
        if not self.api_key:
            return False, "还没填 API Key"
        if not self.model:
            return False, "还没填模型名 model"
        return True, ""

    @property
    def _endpoint(self) -> str:
        # 用户可能填 https://api.deepseek.com，也可能填 .../v1，两种都得能用
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def translate(self, text: str, timeout: float | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(
                self._endpoint,
                headers=headers,
                json=payload,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise TranslateError(f"网络不通或接口地址有误（{exc}）") from exc

        if resp.status_code == 401:
            raise TranslateError("AI 接口拒绝认证（401）：API Key 填错了或已失效")
        if resp.status_code == 402:
            raise TranslateError("AI 账户余额不足（402），去服务商后台充值")
        if resp.status_code == 404:
            raise TranslateError(
                f"接口地址不存在（404）：检查 base_url 是否为 {self._endpoint}"
            )
        if resp.status_code == 429:
            raise TranslateError("请求太频繁或额度用尽（429），稍后再试")
        if resp.status_code >= 400:
            detail = resp.text[:180].replace("\n", " ")
            raise TranslateError(f"AI 接口返回 {resp.status_code}：{detail}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise TranslateError("AI 返回了非 JSON 内容") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslateError("AI 返回结构异常，没找到译文") from exc

        if not content or not content.strip():
            raise TranslateError("AI 返回了空译文，换个模型或重试")

        # 模型理论上按分节格式输出；格式跑偏时 parse 会退回纯文本
        return parse_translation(content)


# ---------------------------------------------------------------- 工厂


def build_engine(cfg: dict[str, Any]):
    """按配置造出对应的引擎实例。"""
    if cfg.get("engine") == "ai":
        return AITranslator(cfg.get("ai") or {})
    return BaiduTranslator(cfg.get("baidu") or {})


def looks_like_english(text: str) -> bool:
    """粗筛：一个英文字母都没有的话，多半是选错内容了。"""
    return any(ch.isascii() and ch.isalpha() for ch in text)
