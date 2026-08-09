"""Provider 统一接口：加一个模型 = 加一个适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote, urlsplit

import idna

DEFAULT_MAX_OUTPUT_TOKENS = 1024

_ROUTE_ID_RE = re.compile(r"route:v1:[0-9a-f]{64}\Z")
_ENCODED_PATH_SEPARATOR_RE = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_UNRESERVED_PATH_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_SAFE_PATH_CHARACTERS = _UNRESERVED_PATH_CHARACTERS | frozenset("/!$&'()*+,:;=@[]^|")
_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    }
)


class ProviderConfigurationError(ValueError):
    """Provider 配置缺失或不安全，可直接转换为 CLI 参数错误。"""


def validate_route_id(value: object) -> str:
    """Return one well-formed, opaque provider route identity."""

    if not isinstance(value, str) or _ROUTE_ID_RE.fullmatch(value) is None:
        raise ValueError("route_id 必须是 route:v1: 后跟 64 位小写十六进制摘要")
    return value


def _stable_route_id(*, family: str, target: str, model: str) -> str:
    """Hash one canonical route tuple without exposing any of its inputs."""

    if not all(isinstance(value, str) for value in (family, target, model)):
        raise TypeError("route identity fields must be strings")
    payload = json.dumps(
        {
            "family": family,
            "model": model,
            "target": target,
            "version": 1,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(b"llmolympic-route-v1\0" + payload).hexdigest()
    return validate_route_id(f"route:v1:{digest}")


def _has_unsafe_url_character(value: str) -> bool:
    return any(
        character.isspace()
        or unicodedata.category(character) in {"Cc", "Cf"}
        or ord(character) in _BIDI_CONTROL_CODEPOINTS
        for character in value
    )


def _canonical_hostname(hostname: str, *, source: str) -> str:
    """Canonicalize one parsed host without resolving it over DNS."""

    try:
        return ipaddress.ip_address(hostname).compressed
    except ValueError:
        dns_name = hostname.rstrip(".")
        if not dns_name:
            raise ProviderConfigurationError(f"{source} 必须包含有效主机名")
        if dns_name.isascii():
            return dns_name.lower()
        try:
            # Match httpx/OpenAI's IDNA2008 handling so the route identity and
            # the host that is actually requested cannot disagree.
            canonical_name = idna.encode(dns_name.lower()).decode("ascii").rstrip(".")
        except idna.IDNAError as exc:
            raise ProviderConfigurationError(f"{source} 包含无效的国际化主机名") from exc
        if not canonical_name:
            raise ProviderConfigurationError(f"{source} 必须包含有效主机名")
        return canonical_name


def _canonical_route_path(path: str, *, source: str) -> str:
    """Normalize UTF-8 and percent escapes while preserving reserved semantics."""

    normalized: list[str] = []
    index = 0
    while index < len(path):
        character = path[index]
        if character == "%":
            escape = path[index + 1 : index + 3]
            if len(escape) != 2 or any(digit not in "0123456789abcdefABCDEF" for digit in escape):
                raise ProviderConfigurationError(f"{source} 包含无效的百分号编码")
            decoded = chr(int(escape, 16))
            normalized.append(decoded if decoded in _UNRESERVED_PATH_CHARACTERS else f"%{escape.upper()}")
            index += 3
            continue
        if character in _SAFE_PATH_CHARACTERS:
            normalized.append(character)
        else:
            normalized.extend(f"%{byte:02X}" for byte in character.encode("utf-8"))
        index += 1
    return "".join(normalized)


def _parse_base_url(value: str, *, source: str):
    if not isinstance(value, str) or _has_unsafe_url_character(value):
        raise ProviderConfigurationError(f"{source} 不能包含空白、控制字符或双向文本控制符")
    if "\\" in value or _ENCODED_PATH_SEPARATOR_RE.search(value):
        raise ProviderConfigurationError(f"{source} 不能包含反斜杠或编码的路径分隔符")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing ``port`` performs urllib's numeric/range validation.
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ProviderConfigurationError(f"{source} 必须是有效的 HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ProviderConfigurationError(f"{source} 必须是完整的 http:// 或 https:// URL")
    _canonical_hostname(hostname, source=source)
    if any(unquote(segment) in {".", ".."} for segment in parsed.path.split("/")):
        raise ProviderConfigurationError(f"{source} 不能包含相对路径片段 . 或 ..")
    _canonical_route_path(parsed.path, source=source)
    return parsed, hostname, port


def _canonical_route_endpoint(value: str, *, source: str) -> str:
    """Normalize endpoint spelling only; never resolve DNS aliases."""

    parsed, hostname, port = _parse_base_url(value, source=source)
    scheme = parsed.scheme.lower()
    canonical_host = _canonical_hostname(hostname, source=source)
    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        canonical_host = f"{canonical_host}:{port}"
    canonical_path = _canonical_route_path(parsed.path, source=source).rstrip("/")
    return f"{scheme}://{canonical_host}{canonical_path}"


def _endpoint_fingerprint(base_url: str) -> str:
    canonical_endpoint = _canonical_route_endpoint(base_url, source="Provider base_url")
    return hashlib.sha256(
        b"llmolympic-route-endpoint-v1\0" + canonical_endpoint.encode("utf-8")
    ).hexdigest()


def validate_base_url(
    value: str,
    *,
    source: str,
    require_https_for_remote: bool = False,
) -> str:
    """校验 Provider HTTP(S) 端点，禁止把凭据嵌入 URL。"""

    parsed, hostname, _port = _parse_base_url(value, source=source)
    if parsed.username is not None or parsed.password is not None:
        raise ProviderConfigurationError(f"{source} 不能在 URL 中嵌入用户名或密码")
    if "?" in value or "#" in value:
        raise ProviderConfigurationError(f"{source} 不能包含查询参数或 URL 片段")
    if require_https_for_remote and parsed.scheme.lower() == "http":
        canonical_host = _canonical_hostname(hostname, source=source)
        is_loopback = canonical_host == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(canonical_host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise ProviderConfigurationError(
                f"{source} 携带 API Key 时远程端点必须使用 https://；"
                "http:// 仅允许 localhost、127.0.0.0/8 或 ::1"
            )
    return value.rstrip("/")


class ProviderTimeoutError(TimeoutError):
    """Provider 的原生异步请求超过调用方给定的截止时间。"""


class UsageSupport(StrEnum):
    """How reliably one Provider can account for model token usage."""

    NONE = "none"
    REPORTED = "reported"
    EXACT_ZERO = "exact_zero"


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Strict, provider-reported token counts for one completed call."""

    input_tokens: int
    output_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        for field_name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} 必须是非负整数")
            if value < 0:
                raise ValueError(f"{field_name} 必须是非负整数")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens 必须等于 input_tokens + output_tokens")


@dataclass(frozen=True, slots=True)
class ProviderChatResult:
    """Text plus optional accounting metadata from one Provider call.

    ``text`` intentionally remains validated by ``LLMPlayer`` so legacy
    adapters that accidentally return a non-string keep the existing stable
    player error instead of failing at a new compatibility boundary.
    """

    text: str
    usage: ProviderUsage | None = None
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if self.usage is not None and not isinstance(self.usage, ProviderUsage):
            raise TypeError("usage 必须是 ProviderUsage 或 None")
        if self.finish_reason is not None and not isinstance(self.finish_reason, str):
            raise TypeError("finish_reason 必须是字符串或 None")


class Provider(ABC):
    """把各家模型 API 统一成同步与异步 chat 调用。

    内置 Provider 实现原生 ``achat``，使比赛能真正取消超时请求。同步 ``chat``
    继续保留，兼容脚本和第三方适配器；默认异步实现仅用于未启用硬超时的旧适配器。
    """

    name: str = "abstract"
    # 命名 Profile 实例会设置这个安全标识；永远不在 Provider
    # 对象上暴露 Profile 解析出的 API Key。
    profile_id: str | None = None

    def usage_support_for(self, model: str) -> UsageSupport:
        """Return the adapter's accounting capability without probing it."""

        del model
        return UsageSupport.NONE

    def resolve_output_token_cap(
        self,
        model: str,
        *,
        requested_cap: int | None,
        params: dict[str, object],
    ) -> int | None:
        """Return the output bound this adapter can provably send, if any."""

        del model, requested_cap, params
        return None

    def route_id_for(self, model: str) -> str:
        """Return a stable route identity without inspecting instance secrets.

        Endpoint-aware adapters should override this method.  The compatibility
        fallback deliberately uses only adapter type metadata and the exact
        requested model; it never serializes instance attributes such as API
        keys, names, profile IDs or base URLs.
        """

        provider_type = type(self)
        adapter = f"{provider_type.__module__}.{provider_type.__qualname__}"
        return _stable_route_id(
            family="provider-fallback-v1",
            target=adapter,
            model=model,
        )

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        """同步发送消息；``request_timeout`` 是网络请求限时。"""

    def chat_with_usage(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        output_token_cap: int | None = None,
        **params,
    ) -> ProviderChatResult:
        """Compatibility wrapper for synchronous adapters without usage metadata."""

        if output_token_cap is not None:
            raise ProviderConfigurationError(
                f"Provider {self.name!r} 不支持可验证的输出 Token 上限"
            )
        call_params = dict(params)
        if request_timeout is not None:
            call_params["request_timeout"] = request_timeout
        return ProviderChatResult(
            text=self.chat(
                messages,
                model=model,
                **call_params,
            )
        )

    async def achat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        """异步发送消息；旧适配器在未启用硬超时时回退到工作线程。"""
        call_params = dict(params)
        if request_timeout is not None:
            call_params["request_timeout"] = request_timeout
        return await asyncio.to_thread(
            self.chat,
            messages,
            model=model,
            **call_params,
        )

    async def achat_with_usage(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        output_token_cap: int | None = None,
        **params,
    ) -> ProviderChatResult:
        """Compatibility wrapper for asynchronous adapters without usage metadata."""

        if output_token_cap is not None:
            raise ProviderConfigurationError(
                f"Provider {self.name!r} 不支持可验证的输出 Token 上限"
            )
        call_params = dict(params)
        if request_timeout is not None:
            call_params["request_timeout"] = request_timeout
        return ProviderChatResult(
            text=await self.achat(
                messages,
                model=model,
                **call_params,
            )
        )
