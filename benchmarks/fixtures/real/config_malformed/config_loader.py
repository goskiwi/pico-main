def parse_config(lines):
    config = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid config on line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"empty key on line {line_number}")
        config[key] = value.strip()
    return config
