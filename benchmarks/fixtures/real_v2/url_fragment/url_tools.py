from urllib.parse import urlencode


def append_query(url, params):
    """Append mapping items as URL query parameters."""
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"
