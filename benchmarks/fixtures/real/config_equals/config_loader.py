def parse_config(lines):
    config = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=")
        config[key.strip()] = value.strip()
    return config
