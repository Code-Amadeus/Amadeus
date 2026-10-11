"""Explicit platform environment for third-party child processes.

Application credentials and launch registries never cross this boundary by
inheritance. Callers may add the configuration owned by that particular child.
This isolates accidental credential forwarding, not OS access by local code.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


_PLATFORM_KEYS = frozenset({
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "PATH",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
})


def isolated_child_environment(
    *, source: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build from an allowlist, then add only this child's explicit bindings.

    PATH is retained for declared bare executable names; Runner interpreters
    use their absolute installed path. Python import-path overrides, Provider
    credentials, desktop tokens and registry snapshots are not inherited.
    """
    inherited = os.environ if source is None else source
    result = {key: value for key, value in inherited.items()
              if key.upper() in _PLATFORM_KEYS}
    for key, value in (extra or {}).items():
        if not isinstance(key, str) or not isinstance(value, str) or "\0" in key + value:
            raise ValueError("Invalid child environment binding")
        result[key] = value
    return result
