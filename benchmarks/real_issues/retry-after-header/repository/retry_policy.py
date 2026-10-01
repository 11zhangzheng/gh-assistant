def retry_delay(headers: dict[str, str], default: int = 1) -> int:
    value = headers.get("Retry-After")
    if value is None:
        return default
    try:
        return max(0, int(value))
    except ValueError:
        return default
