"""Redact local fill literals from traces and tool responses."""


def redact_text(text, values):
    if not isinstance(text, str):
        return text
    for value in sorted(set(values or ()), key=len, reverse=True):
        if not isinstance(value, str) or not value:
            continue
        if len(value) < 4:
            if text == value:
                text = "[private]"
        else:
            text = text.replace(value, "[private]")
    return text


def redact(value, values):
    if isinstance(value, str):
        return redact_text(value, values)
    if isinstance(value, dict):
        return {key: redact(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, values) for item in value)
    return value
