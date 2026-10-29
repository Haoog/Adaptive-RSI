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
    if prices and sum(counts.values()) == tokens and all(
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


def native_usage(protocol, response):
    if not isinstance(response, dict):
        return None
    data = response.get("usage")
    if protocol == "ollama":
        if "eval_count" not in response:
            return None
        values = [_tolerated(response.get("prompt_eval_count", 0)), _tolerated(response.get("eval_count"))]
        return usage(*values) if None not in values else None
    if protocol == "gemini":
        data = response.get("usageMetadata")
        if not isinstance(data, dict) or not data:
            return None
        fields = {key: _tolerated(data.get(key, 0)) for key in
                  ["promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "cachedContentTokenCount"]}
        total = _tolerated(data.get("totalTokenCount")) if data.get("totalTokenCount") is not None else None
        if None in fields.values() or total is None and data.get("totalTokenCount") is not None:
            return None
        return usage(max(0, fields["promptTokenCount"] - fields["cachedContentTokenCount"]),
                     fields["candidatesTokenCount"] + fields["thoughtsTokenCount"],
                     fields["cachedContentTokenCount"], total=total)
    if not isinstance(data, dict) or not data:
        return None
    if protocol == "anthropic":
        fields = {key: _tolerated(data.get(key, 0)) for key in
                  ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]}
        return usage(fields["input_tokens"], fields["output_tokens"],
                     fields["cache_read_input_tokens"], fields["cache_creation_input_tokens"]) \
            if None not in fields.values() else None
    input_name, output_name = ("prompt_tokens", "completion_tokens") if protocol == "chat_completions" else ("input_tokens", "output_tokens")
    cached = _tolerated(data.get(input_name + "_details", {}).get("cached_tokens", 0)) if isinstance(data.get(input_name + "_details"), dict) else 0
    input_tokens, output_tokens = _tolerated(data.get(input_name, 0)), _tolerated(data.get(output_name, 0))
    total = _tolerated(data.get("total_tokens")) if data.get("total_tokens") is not None else None
    if None in (input_tokens, output_tokens, cached) or total is None and data.get("total_tokens") is not None:
        return None
    return usage(max(0, input_tokens - cached), output_tokens, cached, total=total)


def opencode_usage(steps):
    if not steps:
        return None
    result = usage()
    for step in steps:
        cache = step.get("cache", {})
        if not isinstance(cache, dict):
            cache = {}
        fields = [_tolerated(step.get("input", 0)), _tolerated(step.get("output", 0)),
                  _tolerated(cache.get("read", 0)), _tolerated(cache.get("write", 0))]
        total = _tolerated(step.get("total")) if step.get("total") is not None else None
        if None in fields or total is None and step.get("total") is not None:
            return None
        current = usage(fields[0], fields[1], fields[2], fields[3], total)
        for key in ["tokens", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]:
            result[key] += current[key]
    return result
