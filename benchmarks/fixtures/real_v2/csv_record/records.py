def parse_record(line):
    """Parse a single CSV record."""
    if not isinstance(line, str):
        raise TypeError("line must be a string")
    return tuple(part.strip() for part in line.strip().split(","))
