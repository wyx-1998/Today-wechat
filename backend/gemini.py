"""Gemini native HTTPS adapter. No redirects, proxy inheritance or body logs."""

import json
import http.client
import math
import re
import ssl
import socket
import urllib.error
import urllib.parse
import urllib.request

from backend.contracts import ServiceError
from backend.model_references import compact_prompt


def normalize_endpoint(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ServiceError("invalid_model_address", "请输入 HTTPS 服务根地址或 /v1beta 地址。")
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and parsed.path.rstrip("/") in {"", "/v1beta"} and parsed.port in {None, 443})
    except ValueError:
        valid = False
    if not valid:
        raise ServiceError("invalid_model_address", "需要 HTTPS 根地址或 /v1beta，不包含密钥、查询参数、账号或其他路径。")
    return urllib.parse.urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def model_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", value):
        raise ServiceError("invalid_model_name", "模型名只使用字母、数字、点、短横线或下划线。")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_failure(status, media_type):
    """Retain only a bounded numeric status, never provider text or headers."""
    valid = type(status) is int and 300 <= status <= 599
    if not valid:
        return ServiceError("model_http_error", "服务未完成请求，无法确认 HTTP 状态；未保存响应正文，实际用量可能未知。", retryable=True)
    if status < 400:
        code, detail = "model_redirect", "服务要求重定向，未转发密钥；请填写最终 HTTPS 服务地址。"
    elif status in {401, 403}:
        if media_type in {"text/html", "application/xhtml+xml"}:
            code, detail = "model_gateway_denied", "服务返回网页拒绝，尚不能判定为 API 密钥错误；请联系服务方核对网关限制。"
        else:
            code, detail = "model_auth", "请核对密钥启用、Gemini 第三方调用分组、模型权限及额度。"
    elif status == 429:
        code, detail = "model_rate_limit", "服务限流或额度不足；请查看服务账户后手动重试。"
    elif status == 404:
        code, detail = "model_endpoint", "服务端点或模型不存在；请核对 Gemini 原生协议及模型名。"
    else:
        # Fits the existing persistent error_code field. No schema migration or
        # retroactive rewrite; old generic errors remain genuinely unknown.
        code = f"model_http_{status}"
        detail = "服务请求未完成；该状态不足以确定具体原因，请核对服务状态或联系服务方。"
    return ServiceError(code, f"服务返回 HTTP {status}。{detail}未保存响应正文；实际用量可能未知。", retryable=True)


def transport_failure(error):
    """Classify exception types only. Never store errno text or partial bodies.

    A closed connection cannot establish remote processing or billing. Older
    generic errors remain unchanged; classification applies to new attempts.
    """
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    if isinstance(error, TimeoutError):
        return GeminiClient.timeout_error()
    if isinstance(error, (http.client.RemoteDisconnected, ssl.SSLEOFError, EOFError)):
        code, detail = "model_connection_closed_unknown", "连接在完整响应返回前被关闭。"
    elif isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        code, detail = "model_connection_reset_unknown", "连接被重置或中断。"
    elif isinstance(error, http.client.IncompleteRead):
        code, detail = "model_response_incomplete_unknown", "HTTP 响应未接收完整，未使用部分正文。"
    elif isinstance(error, socket.gaierror):
        code, detail = "model_dns_unknown", "无法完成服务地址解析。"
    elif isinstance(error, ssl.SSLError):
        code, detail = "model_tls_unknown", "TLS 安全连接未完成，请核对服务证书与网络；不要关闭证书验证。"
    elif isinstance(error, http.client.HTTPException):
        code, detail = "model_http_protocol_unknown", "未取得有效的完整 HTTP 响应。"
    else:
        code, detail = "model_result_unknown", "暂未取得模型结果。"
    return ServiceError(code, detail + "请求结果未知，服务端可能已处理或计费；未保存异常正文，请核对用量后决定是否重试。", retryable=True)


class GeminiClient:
    def __init__(self, *, timeout=40, reconciliation_timeout=120, opener=None):
        for value in (timeout, reconciliation_timeout):
            if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 600:
                raise ValueError("Model wait timeout must be finite and within (0, 600].")
        self.timeout = timeout
        self.reconciliation_timeout = reconciliation_timeout
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def generate(self, config, key, prompt, *, schema=None, json_response=True):
        base = normalize_endpoint(config["base_url"])
        endpoint = base + ("" if base.endswith("/v1beta") else "/v1beta")
        endpoint += "/models/" + model_name(config["model"]) + ":generateContent"
        prompt, references = compact_prompt(prompt, schema) if json_response else (prompt, None)
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if json_response:
            generation = {"responseMimeType": "application/json"}
            if schema:
                generation["responseSchema"] = schema
            payload["generationConfig"] = generation
        request = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": key,
                     "User-Agent": "wechat-today/0.1.0", "Accept": "application/json"}, method="POST")
        try:
            # Blocking socket-operation wait, not a whole-batch deadline. A
            # candidate ledger can take longer than bounded discovery windows.
            wait_timeout = (self.reconciliation_timeout if json_response and isinstance(schema, dict)
                and "candidate_decisions" in schema.get("properties", {}) else self.timeout)
            with self.opener.open(request, timeout=wait_timeout) as response:
                # Resource safety only. Oversized replies fail, never truncate.
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ServiceError("model_response_large", "模型响应超过本地安全解析范围；未发布结果。")
        except urllib.error.HTTPError as error:
            code = error.code
            # Only inspect a fixed media-type classification, never read or echo
            # provider error bodies or arbitrary headers (they may reflect keys).
            media_type = (error.headers.get("Content-Type", "") if error.headers else "").split(";", 1)[0].strip().lower()
            error.close()
            raise http_failure(code, media_type) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, EOFError) as error:
            raise transport_failure(error) from None
        try:
            data = json.loads(raw)
            candidates = data["candidates"]
            if not isinstance(candidates, list) or len(candidates) != 1:
                raise ValueError()
            candidate = candidates[0]
            if candidate.get("finishReason") != "STOP":
                raise ValueError()
            parts = candidate["content"]["parts"]
            # Do not treat thinking parts as the answer.
            text = "".join(p["text"] for p in parts if isinstance(p, dict) and "text" in p and not p.get("thought"))
            safe_text = text.replace(key, "[访问密钥]")
            answer = json.loads(safe_text) if json_response else safe_text
            if references is not None:
                answer = references.restore(answer)
            usage = data.get("usageMetadata", {})
            counts = {name: usage.get(field) for name, field in (("input_tokens", "promptTokenCount"),
                ("output_tokens", "candidatesTokenCount"), ("total_tokens", "totalTokenCount"))}
            counts = {name: number if type(number) is int and number >= 0 else None for name, number in counts.items()}
            return answer, counts
        except (KeyError, ValueError, TypeError, AttributeError, UnicodeError):
            raise ServiceError("model_response_invalid", "模型没有返回完整、可解析的 JSON 答复；未发布结果，请重试。", retryable=True) from None

    @staticmethod
    def timeout_error():
        return ServiceError("model_timeout_unknown", "模型连接或读取等待超时，结果未知；服务端可能仍在处理或已计费。本轮不自动重发，请核对用量后再决定。", retryable=True)
