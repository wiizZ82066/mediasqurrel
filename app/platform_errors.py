"""Shared platform-access failures, independent of transport and scan coverage.

Only an explicitly unavailable/unsupported adapter permits trying another
transport. Unknown failures, authentication, throttling and platform challenges
must stop; a retryable network error is not permission to switch transports.
"""
from .redaction import redact_text


class PlatformAccessError(ValueError):
    def __init__(self, message, reason='platform_error', *, platform=''):
        super().__init__(redact_text(str(message)))
        self.reason = reason
        self.platform = platform

    @property
    def retryable(self):
        """May retry later under the caller's existing bounded retry policy."""
        return self.reason in ('network_error', 'timeout')

    @property
    def fallback_allowed(self):
        """Unavailable means local adapter absence, never an upstream outage."""
        return self.reason in ('unavailable', 'unsupported')
