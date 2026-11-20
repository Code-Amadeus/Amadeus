"""Development renderer origin for the backend's exact-origin checks."""

import logging
import re

from config.environment import EnvironmentReader


def development_server_origin(environment: EnvironmentReader) -> str | None:
    # Electron passes its already-resolved port; headless development uses the
    # same process-over-root-dotenv precedence through EnvironmentReader.
    value = environment.string("AMADEUS_DEV_PORT", "5173").strip()
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
        # Bad development input must not prevent native/file-origin clients
        # from using the backend. Disable only the unresolved browser origin.
        logging.getLogger(__name__).warning(
            "Invalid AMADEUS_DEV_PORT (expected an integer from 1 to 65535); "
            "development browser origin disabled."
        )
        return None
    port = int(value)
    return "http://localhost" if port == 80 else f"http://localhost:{port}"
