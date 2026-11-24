"""归一化用量；缺失数据保留未知状态，不把缓存重复计费。"""
import math


PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
MAX_UNIT_PRICE = 1000000


def unit_prices(value, strict=False):
    """Normalize optional per-1M prices; strict callers reject unknown or invalid input."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        if strict:
            raise ValueError("Invalid model prices")
        return {}
    result = {}
    for key, price in value.items():
        if key not in PRICE_FIELDS:
            if strict:
                raise ValueError("Invalid model prices")
            continue
        if price is None:
            continue
        if (not isinstance(price, (int, float)) or isinstance(price, bool)
                or not math.isfinite(price) or price < 0 or price > MAX_UNIT_PRICE):
            if strict:
                raise ValueError("Invalid model prices")
            continue
        result[key] = float(price)
    return result


def estimate(details, prices=None, unit_price=None):
    """Return (USD, basis) from frozen prices; missing components never count as zero."""
    if not details or not details.get("usage_known"):
        return None, "unknown"
    tokens = details.get("tokens")
    counts = {name: details.get(name, 0) for name in
              ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}
    if type(tokens) is not int or tokens < 0 or any(type(count) is not int or count < 0
                                                    for count in counts.values()):
        return None, "unknown"
    prices = prices or {}
    # 分量口径只在归一化后的四类计数与 tokens 对账时使用，避免缓存重复或漏计。
    if details.get("usage_components_known") != 0 and prices and sum(counts.values()) == tokens and all(
            counts[field + "_tokens"] == 0 or field in prices for field in PRICE_FIELDS):
        return sum(counts[field + "_tokens"] * prices.get(field, 0)
                   for field in PRICE_FIELDS) / 1000000.0, "components"
    if isinstance(unit_price, (int, float)) and not isinstance(unit_price, bool):
        price = float(unit_price)
        if math.isfinite(price) and price >= 0:
            return tokens * price / 1000000.0, "mixed"
    return None, "unknown"


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid token usage")
    return value


def _tolerated(value):
    """容忍网关返回的 null/字符串计数；无法确认时返回 None（按未知处理）。"""
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            number = int(value)
        except ValueError:
            return None
        return number if number >= 0 else None
    return None


def usage(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, total=None):
    values = {"input_tokens": count(input_tokens), "output_tokens": count(output_tokens),
              "cache_read_tokens": count(cache_read), "cache_write_tokens": count(cache_write)}
    return {**values, "tokens": count(total) if total is not None else sum(values.values()), "usage_known": True}


def _reported_usage(data, input_name, output_name, *, cache_read=0, cache_write=0,
                    extra_output=0, total_name=None, input_includes_cache=False):
    raw_total = data.get(total_name) if total_name else None
    total = _tolerated(raw_total)
    if raw_total is not None and total is None:
        return None
    # 有明确总量时兼容只报总量的供应商，缺少主计数时只保留总量。
    default = 0 if total is not None else None
    input_tokens = _tolerated(data.get(input_name, default))
    output_tokens = _tolerated(data.get(output_name, default))
    if None in (input_tokens, output_tokens, cache_read, cache_write, extra_output):
        return None
    if input_includes_cache and input_name in data:
        if cache_read > input_tokens:
            return None
        input_tokens -= cache_read
    output_tokens += extra_output
    if total is not None:
        if total < input_tokens + output_tokens + cache_read + cache_write:
            return None
        if input_name not in data or output_name not in data:
            return {**usage(total=total), "usage_components_known": False}
    return usage(input_tokens, output_tokens, cache_read, cache_write, total)


def native_usage(protocol, response):
    if not isinstance(response, dict):
        return None
    data = response.get("usage")
    if protocol == "ollama":
        return _reported_usage(response, "prompt_eval_count", "eval_count")
    if protocol == "gemini":
        data = response.get("usageMetadata")
        if not isinstance(data, dict):
            return None
        return _reported_usage(data, "promptTokenCount", "candidatesTokenCount", total_name="totalTokenCount",
                               extra_output=_tolerated(data.get("thoughtsTokenCount", 0)),
                               cache_read=_tolerated(data.get("cachedContentTokenCount", 0)),
                               input_includes_cache=True)
    if not isinstance(data, dict):
        return None
    if protocol == "anthropic":
        return _reported_usage(data, "input_tokens", "output_tokens",
                               cache_read=_tolerated(data.get("cache_read_input_tokens", 0)),
                               cache_write=_tolerated(data.get("cache_creation_input_tokens", 0)))
    input_name, output_name = ("prompt_tokens", "completion_tokens") if protocol == "chat_completions" else ("input_tokens", "output_tokens")
    details = data.get(input_name + "_details")
    if details is not None and not isinstance(details, dict):
        return None
    cached = _tolerated(details.get("cached_tokens", 0)) if details else 0
    return _reported_usage(data, input_name, output_name, cache_read=cached, total_name="total_tokens",
                           input_includes_cache=True)


def codex_usage(data):
    if not isinstance(data, dict):
        return None
    return _reported_usage(data, "input_tokens", "output_tokens",
                           cache_read=_tolerated(data.get("cached_input_tokens", 0)), input_includes_cache=True)


def opencode_usage(steps):
    if not steps:
        return None
    result = usage()
    for step in steps:
        if not isinstance(step, dict):
            return None
        cache = step.get("cache", {})
        if not isinstance(cache, dict):
            return None
        current = _reported_usage(step, "input", "output", total_name="total",
                                  cache_read=_tolerated(cache.get("read", 0)),
                                  cache_write=_tolerated(cache.get("write", 0)))
        if current is None:
            return None
        if current.get("usage_components_known") is False:
            result["usage_components_known"] = False
        for key in ["tokens", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]:
            result[key] += current[key]
    return result
