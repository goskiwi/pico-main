def parse_byte_range(header, size):
    """Return inclusive start and end offsets for one HTTP byte range."""
    if size <= 0:
        raise ValueError("size must be positive")
    if not header.startswith("bytes="):
        raise ValueError("unsupported range unit")

    spec = header[6:]
    if "," in spec:
        raise ValueError("multiple ranges are not supported")
    start_text, end_text = spec.split("-", 1)
    if not start_text or not end_text:
        raise ValueError("open ranges are not supported")

    try:
        start = int(start_text)
        end = int(end_text)
    except ValueError as exc:
        raise ValueError("invalid byte range") from exc
    if start < 0 or end < start or start >= size:
        raise ValueError("unsatisfiable byte range")
    return start, min(end, size - 1)
