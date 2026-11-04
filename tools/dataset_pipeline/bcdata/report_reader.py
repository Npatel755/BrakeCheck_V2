"""Optional progress reports must never prevent access to saved reviews."""
from copy import deepcopy
import json
from pathlib import Path


def optional_report(path, fallback=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if isinstance(value, dict):
            return value
    except (OSError, UnicodeError, ValueError):
        pass
    return deepcopy(fallback)
