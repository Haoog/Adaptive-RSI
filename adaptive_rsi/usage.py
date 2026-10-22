"""归一化用量；缺失数据保留未知状态，不把缓存重复计费。"""


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid token usage")
    return value


def usage(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, total=None):
    values = {"input_tokens": count(input_tokens), "output_tokens": count(output_tokens),
              "cache_read_tokens": count(cache_read), "cache_write_tokens": count(cache_write)}
    return {**values, "tokens": count(total) if total is not None else sum(values.values()), "usage_known": True}


def native_usage(protocol, response):
    data = response.get("usage")
    if protocol == "ollama":
        return usage(response.get("prompt_eval_count", 0), response.get("eval_count", 0)) if "eval_count" in response else None
    if protocol == "gemini":
        data = response.get("usageMetadata")
        if not data:
            return None
        cached = data.get("cachedContentTokenCount", 0)
        return usage(max(0, data.get("promptTokenCount", 0) - cached),
                     data.get("candidatesTokenCount", 0) + data.get("thoughtsTokenCount", 0),
                     cached, total=data.get("totalTokenCount"))
    if not data:
        return None
    if protocol == "anthropic":
        return usage(data.get("input_tokens", 0), data.get("output_tokens", 0),
                     data.get("cache_read_input_tokens", 0), data.get("cache_creation_input_tokens", 0))
    input_name, output_name = ("prompt_tokens", "completion_tokens") if protocol == "chat_completions" else ("input_tokens", "output_tokens")
    cached = data.get(input_name + "_details", {}).get("cached_tokens", 0)
    return usage(max(0, data.get(input_name, 0) - cached), data.get(output_name, 0), cached, total=data.get("total_tokens"))


def opencode_usage(steps):
    if not steps:
        return None
    result = usage()
    for step in steps:
        cache = step.get("cache", {})
        current = usage(step.get("input", 0), step.get("output", 0), cache.get("read", 0), cache.get("write", 0), step.get("total"))
        for key in ["tokens", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]:
            result[key] += current[key]
    return result
