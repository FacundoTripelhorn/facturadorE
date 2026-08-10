"""TLS helpers for ARCA httpx clients (FAC-81).

AFIP production WSFEX (``servicios1.afip.gov.ar``) still offers DHE with a
1024-bit DH temp key. OpenSSL 3's default SECLEVEL rejects that
(``DH_KEY_TOO_SMALL``). Homologación and WSAA (both envs) negotiate ECDH and
need no change.

The only accommodation is a per-client ``SSLContext`` that keeps Python's
default cipher policy and lowers SECLEVEL to 1 for **prod WSFEX**.
Certificate verification stays on — never ``verify=False``
(design.md §2.1.1 punto 5).
"""

from __future__ import annotations

import logging
import ssl

from ..constants import ArcaEnvironment

logger = logging.getLogger(__name__)

# OpenSSL cipher-list marker: lower security level without swapping the
# cipher suite set. Keep in sync with docs/design.md §2.1.1 punto 5 (FAC-81).
_SECLEVEL_1 = "@SECLEVEL=1"


def _prod_wsfex_ssl_context() -> ssl.SSLContext:
    """Default-context TLS with SECLEVEL=1 for AFIP prod weak DH."""
    ctx = ssl.create_default_context()
    # ``set_ciphers("DEFAULT:@SECLEVEL=1")`` would replace Python/OpenSSL's
    # hardened suite list with the broader DEFAULT set. Instead, reuse the
    # TLS 1.2 names already selected by create_default_context() and only
    # append the SECLEVEL marker. TLS 1.3 suites (``TLS_*``) stay on the
    # context via OpenSSL's separate ciphersuites list.
    tls12 = [
        cipher["name"]
        for cipher in ctx.get_ciphers()
        if not cipher["name"].startswith("TLS_")
    ]
    ctx.set_ciphers(":".join([*tls12, _SECLEVEL_1]))
    return ctx


def wsfex_verify(env: ArcaEnvironment) -> ssl.SSLContext | bool:
    """Return the httpx ``verify`` value for WSFEX.

    Production: ``SSLContext`` with SECLEVEL=1 (AFIP weak DH). Homologación:
    ``True`` (httpx/OpenSSL defaults). Never returns ``False``.
    """
    return arca_servicios1_verify(env, label="WSFEX")


def arca_servicios1_verify(
    env: ArcaEnvironment, *, label: str = "servicios1"
) -> ssl.SSLContext | bool:
    """httpx ``verify`` for ARCA hosts on ``servicios1`` (prod weak DH).

    Used by WSFEX and WSCDC (FAC-84). Homologación keeps defaults.
    Never returns ``False``.
    """
    if env != ArcaEnvironment.PROD:
        return True
    ctx = _prod_wsfex_ssl_context()
    logger.info(
        "ARCA %s TLS: SECLEVEL=1 (AFIP prod weak-DH workaround, FAC-81)",
        label,
    )
    return ctx
