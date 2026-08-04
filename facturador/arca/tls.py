"""TLS helpers for ARCA httpx clients (FAC-81).

AFIP production WSFEX (``servicios1.afip.gov.ar``) still offers DHE with a
1024-bit DH temp key. OpenSSL 3's default SECLEVEL rejects that
(``DH_KEY_TOO_SMALL``). Homologación and WSAA (both envs) negotiate ECDH and
need no change.

The only accommodation is a per-client ``SSLContext`` with
``DEFAULT:@SECLEVEL=1`` for **prod WSFEX**. Certificate verification stays
on — never ``verify=False`` (design.md §2.1.1 punto 5).
"""

from __future__ import annotations

import logging
import ssl

from ..constants import ArcaEnvironment

logger = logging.getLogger(__name__)

# Cipher string for the AFIP prod WSFEX weak-DH exception. Keep in sync with
# docs/design.md §2.1.1 punto 5 (FAC-81).
_PROD_WSFEX_CIPHERS = "DEFAULT:@SECLEVEL=1"


def wsfex_verify(env: ArcaEnvironment) -> ssl.SSLContext | bool:
    """Return the httpx ``verify`` value for WSFEX.

    Production: ``SSLContext`` with SECLEVEL=1 (AFIP weak DH). Homologación:
    ``True`` (httpx/OpenSSL defaults). Never returns ``False``.
    """
    if env != ArcaEnvironment.PROD:
        return True
    ctx = ssl.create_default_context()
    ctx.set_ciphers(_PROD_WSFEX_CIPHERS)
    logger.info(
        "ARCA WSFEX TLS: SECLEVEL=1 (AFIP prod weak-DH workaround, FAC-81)"
    )
    return ctx
