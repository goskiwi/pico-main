def retry(operation, retries):
    if retries < 0:
        raise ValueError("retries must not be negative")
    last_error = None
    for _attempt in range(retries + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
    raise last_error
