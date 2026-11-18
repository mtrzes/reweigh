import re


def safe_name(raw):
    """Keep the final path component, replacing anything outside [A-Za-z0-9._-]."""
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if cleaned in ("", ".", ".."):
        raise ValueError("bad file name")
    return cleaned
