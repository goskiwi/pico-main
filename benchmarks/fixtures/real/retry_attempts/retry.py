def retry(operation, max_attempts):
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    last_error = None
    for _attempt in range(max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
    raise last_error
