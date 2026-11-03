"""Shared secret classification and bounded, structured diagnostic redaction."""
import json
import re

# These are documented boolean switches, not credentials. Values do not decide identity.
NON_SECRET_FIELDS = frozenset({'CODEBUDDY_API_KEY_HELPER_DISABLED'})


def is_secret_field(name):
    upper = str(name).upper()
    return upper not in NON_SECRET_FIELDS and any(word in upper for word in
        ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'AUTHORIZATION'))


def redact(text, env):
    values = {str(v) for k, v in env.items() if v and is_secret_field(k)}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    return re.sub(r"(?i)(bearer\s+)[^\s\"',}]+", r"\1[REDACTED]", text)


def redact_value(value, env):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if is_secret_field(k) else redact_value(v, env)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, env) for v in value]
    if isinstance(value, str):
        # Tool arguments may be JSON encoded in a string. Preserve the original
        # bytes when unchanged; sanitize credential fields even if not in env.
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            decoded = None
        if isinstance(decoded, (dict, list)):
            safe = redact_value(decoded, env)
            if safe != decoded:
                value = json.dumps(safe, ensure_ascii=False)
        return redact(value, env)
    return value


def audit_call(call, env, limit=4096):
    result = {}
    for key in ('type', 'name', 'namespace', 'call_id', 'arguments', 'input'):
        if key not in call:
            continue
        value = call[key]
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        safe = redact_value(value, env)
        encoded = json.dumps(safe, ensure_ascii=False).encode('utf-8')
        if len(encoded) > limit:
            result[key] = encoded[:limit].decode('utf-8', errors='ignore')
            result[key + '_metadata'] = {'truncated': True, 'original_bytes': len(raw),
                'redacted_bytes': len(encoded), 'preview_limit_bytes': limit}
        else:
            result[key] = safe
    return result
