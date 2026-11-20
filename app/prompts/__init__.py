import re


def render_prompt(template: str, **values: str) -> str:
    """Replace named fields while keeping literal JSON examples intact."""
    return re.sub(r"\{([a-z_]+)\}", lambda match: values[match[1]], template)
