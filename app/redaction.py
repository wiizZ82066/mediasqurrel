"""Redaction shared by stored logs, public errors and diagnostic exports."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
_SECRET = re.compile(r"cookie|authorization|token|password|passwd|secret|session(?:id)?|api[-_]?key|access[-_]?key|auth[-_]?key|credential|signature", re.I)
_COOKIE_NAMES = re.compile(r"^(?:SUB|SUBP|SESSDATA|ttwid|sid_guard|uid_tt|uid_tt_ss|sessionid_ss)$", re.I)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
# Scan a bounded key only at its boundary, then classify it in Python. Searching
# for a secret substring after an unbounded key prefix causes quadratic work on
# long plain log lines (and can stop the event loop before it reaches the cap).
_PAIR = re.compile(r"(?<![\w.-])(?P<key>[\"']?(?P<name>[\w.-]{1,128})[\"']?\s*[:=]\s*)(?P<val>\[REDACTED\]|\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)", re.I)
_HEADER = re.compile(r"(?im)(\b(?:set-cookie|cookie|authorization|proxy-authorization)\s*:\s*)[^\r\n]+")
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\"'<>|]+")


def _url(match):
    value = match.group(0)
    try:
        parts = urlsplit(value)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        query = urlencode([(k, REDACTED if _secret_key(k) else v)
                           for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        fragment = _PAIR.sub(_redact_pair, parts.fragment)
        return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))
    except ValueError:
        return "[INVALID URL]"


def redact_text(value: str, *, diagnostic: bool = False) -> str:
    text = _URL.sub(_url, str(value))
    text = _HEADER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _PAIR.sub(_redact_pair, text)
    text = _BEARER.sub(lambda m: m.group(1) + " " + REDACTED, text)
    if diagnostic:
        text = _PATH.sub("[LOCAL PATH]", text)
        text = re.sub(r"(?<![:/])/(?:home|Users|tmp|mnt|media)/[^\s\"']+", "[LOCAL PATH]", text)
    return text


def _secret_key(key):
    return _SECRET.search(key) or _COOKIE_NAMES.fullmatch(key)


def _redact_pair(match):
    return match.group('key') + REDACTED if _secret_key(match.group('name')) else match.group(0)


def redact_value(value, *, diagnostic: bool = False):
    if isinstance(value, dict):
        return {str(k): REDACTED if _secret_key(str(k)) else redact_value(v, diagnostic=diagnostic)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, diagnostic=diagnostic) for v in value]
    if isinstance(value, str):
        return redact_text(value, diagnostic=diagnostic)
    return value


def redact_task_metadata(metadata):
    """Keep the generated subscription claim ID internally; it is not a credential."""
    result = redact_value(metadata or {})
    subscription = (metadata or {}).get("subscription", {})
    claim = subscription.get("claim_token") if isinstance(subscription, dict) else None
    if isinstance(claim, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", claim):
        result["subscription"]["claim_token"] = claim
    return result
