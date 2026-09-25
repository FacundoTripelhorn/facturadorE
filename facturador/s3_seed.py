"""S3 adapter for the encrypted profile seed.

Stores and retrieves the **already-encrypted** seed blob plus the cleartext
recipients list. Seed creation / age encryption lives in seed_backup; this module only
talks to object storage.

Layout (exactly one logical bundle per profile + environment)::

    s3://{bucket}/{prefix}/{cuit}/{env}/seed.age
    s3://{bucket}/{prefix}/{cuit}/{env}/recipients.txt

``seed.age`` is overwritten on each backup (single ``PutObject`` of the
complete ciphertext — no multipart, no partial object left behind).
``recipients.txt`` holds ``age`` public keys (one per machine, optional
``# comment`` lines). Public keys are not secret; the file is readable from
a machine that cannot yet decrypt the seed (new-machine bootstrap).

Bucket and prefix come from profile configuration (``Settings.backup_s3_*``),
never hardcoded. Object keys expose only the semi-public CUIT and environment
— never passphrases, private keys, or ciphertext metadata.

**Bucket versioning:** enable S3 versioning on the user-owned private bucket.
Overwrite safety is the bucket's version history; this adapter has no
in-app version / listing / recency logic.

**Credentials (minimal scope):** the boto3 client uses the standard AWS
credential chain (environment variables, shared credentials file, or
instance/role credentials). Grant the IAM principal only::

    s3:GetObject, s3:PutObject

on ``arn:aws:s3:::<bucket>/<prefix>/*`` (and ``s3:ListBucket`` only if an
operator tool needs it — this adapter does not list). Block Public Access
on the bucket. Do not put secrets in object keys or user-defined metadata.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from .constants import ArcaEnvironment
from .profile import parse_environment

logger = logging.getLogger(__name__)

SEED_OBJECT_NAME = "seed.age"
RECIPIENTS_OBJECT_NAME = "recipients.txt"

_CUIT_RE = re.compile(r"^\d{11}$")
# Bech32 age recipient keys are typically `age1` + ~58 chars; keep a
# loose lower bound so we reject empty/garbage without pinning an exact length.
_AGE_PUBKEY_RE = re.compile(r"^age1[a-z0-9]{50,100}$")

# Transient S3 / network conditions worth retrying with backoff.
_TRANSIENT_ERROR_CODES = frozenset(
    {
        "SlowDown",
        "ServiceUnavailable",
        "InternalError",
        "RequestTimeout",
        "RequestTimeTooSkewed",
        "Throttling",
        "ThrottlingException",
        "PriorRequestNotComplete",
        "Timeout",
    }
)
_TRANSIENT_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE_S = 0.25


class S3SeedError(RuntimeError):
    """Base error for seed object-storage operations."""


class S3SeedConfigError(S3SeedError):
    """Invalid location / configuration (not retried)."""


class S3SeedNotFoundError(S3SeedError):
    """Requested object does not exist."""


class S3SeedPermanentError(S3SeedError):
    """Non-retryable S3 / credential / access failure."""


class S3SeedTransientError(S3SeedError):
    """Retryable failure that exhausted the backoff budget."""


@runtime_checkable
class ObjectStore(Protocol):
    """Minimal put/get surface; tests inject an in-memory implementation."""

    def put(self, bucket: str, key: str, body: bytes) -> None:
        """Upload ``body`` as a single complete object (overwrite)."""

    def get(self, bucket: str, key: str) -> bytes:
        """Return object bytes; raise ``S3SeedNotFoundError`` if missing."""


@dataclass(frozen=True)
class SeedLocation:
    """Where the single seed bundle lives for one profile + environment.

    ``bucket`` / ``prefix`` are profile settings (user-owned private bucket).
    ``cuit`` is the sealed fiscal identity (11 digits). ``environment`` is
    the immutable ARCA environment of the profile.
    """

    bucket: str
    prefix: str
    cuit: str
    environment: ArcaEnvironment

    def __post_init__(self) -> None:
        bucket = self.bucket.strip()
        if not bucket:
            raise S3SeedConfigError(
                "backup_s3_bucket is empty: configure the user-owned bucket "
                "in Configuración before using seed storage."
            )
        cuit = self.cuit.strip()
        if not _CUIT_RE.fullmatch(cuit):
            raise S3SeedConfigError(
                f"CUIT inválido para la clave S3 del seed: {self.cuit!r} "
                "(se esperan 11 dígitos)."
            )
        prefix = self.prefix.strip().strip("/")
        object.__setattr__(self, "bucket", bucket)
        object.__setattr__(self, "cuit", cuit)
        object.__setattr__(self, "prefix", prefix)

    @classmethod
    def from_config(
        cls,
        *,
        bucket: str,
        prefix: str,
        cuit: str,
        environment: ArcaEnvironment | str,
    ) -> SeedLocation:
        env = (
            environment
            if isinstance(environment, ArcaEnvironment)
            else parse_environment(str(environment))
        )
        return cls(bucket=bucket, prefix=prefix, cuit=cuit, environment=env)

    def key_for(self, object_name: str) -> str:
        """Build ``{prefix}/{cuit}/{env}/{object_name}`` (prefix optional)."""
        name = object_name.strip().lstrip("/")
        if name not in (SEED_OBJECT_NAME, RECIPIENTS_OBJECT_NAME):
            raise S3SeedConfigError(
                f"Nombre de objeto no permitido: {object_name!r} "
                f"(solo {SEED_OBJECT_NAME!r} y {RECIPIENTS_OBJECT_NAME!r})."
            )
        parts = [p for p in (self.prefix, self.cuit, self.environment.value, name) if p]
        return "/".join(parts)

    @property
    def seed_key(self) -> str:
        return self.key_for(SEED_OBJECT_NAME)

    @property
    def recipients_key(self) -> str:
        return self.key_for(RECIPIENTS_OBJECT_NAME)

    def uri_for(self, object_name: str) -> str:
        return f"s3://{self.bucket}/{self.key_for(object_name)}"


def parse_recipients(text: str) -> list[str]:
    """Extract age public keys from a recipients.txt body (comments allowed)."""
    keys: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Inline comment: "age1...  # laptop"
        key = line.split("#", 1)[0].strip()
        if key:
            keys.append(key)
    return keys


def format_recipients(
    keys: list[str],
    *,
    existing_text: str = "",
    new_comment: str | None = None,
) -> str:
    """Render recipients.txt preserving prior comment lines when possible."""
    # Preserve existing file body and only append keys that are not present.
    body = existing_text if existing_text.endswith("\n") or not existing_text else (
        existing_text + "\n"
    )
    present = set(parse_recipients(existing_text))
    for key in keys:
        if key in present:
            continue
        if new_comment:
            body += f"# {new_comment}\n"
        body += f"{key}\n"
        present.add(key)
    return body


def validate_age_public_key(key: str) -> str:
    cleaned = key.strip()
    if not _AGE_PUBKEY_RE.fullmatch(cleaned):
        raise S3SeedConfigError(
            f"Clave pública age inválida: {key!r} "
            "(formato esperado: age1…)."
        )
    return cleaned


class MemoryObjectStore:
    """In-process stub for tests (no network, no real bucket)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put(self, bucket: str, key: str, body: bytes) -> None:
        self.objects[(bucket, key)] = body

    def get(self, bucket: str, key: str) -> bytes:
        try:
            return self.objects[(bucket, key)]
        except KeyError as exc:
            raise S3SeedNotFoundError(
                f"Object not found: s3://{bucket}/{key}"
            ) from exc


@runtime_checkable
class _S3Client(Protocol):
    """Subset of the boto3 S3 client used by this adapter."""

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> object: ...

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]: ...


class Boto3ObjectStore:
    """Thin boto3 wrapper: one complete PutObject / GetObject per call."""

    def __init__(self, client: _S3Client | None = None) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - env without dep
                raise S3SeedPermanentError(
                    "boto3 is required for S3 seed storage. "
                    "Install project dependencies with `uv sync`."
                ) from exc
            client = boto3.client("s3")
        self._client: _S3Client = client

    def put(self, bucket: str, key: str, body: bytes) -> None:
        try:
            self._client.put_object(Bucket=bucket, Key=key, Body=body)
        except Exception as exc:
            raise _map_boto_error(exc, action="put", bucket=bucket, key=key) from exc

    def get(self, bucket: str, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
            raw = cast(Any, response["Body"]).read()
        except Exception as exc:
            raise _map_boto_error(exc, action="get", bucket=bucket, key=key) from exc
        if isinstance(raw, memoryview):
            return raw.tobytes()
        if isinstance(raw, bytes):
            return raw
        return bytes(raw)


def _map_boto_error(
    exc: BaseException, *, action: str, bucket: str, key: str
) -> S3SeedError:
    """Classify botocore errors into not-found / transient / permanent."""
    name = type(exc).__name__
    code = getattr(exc, "response", None)
    error_code = ""
    status = None
    if isinstance(code, dict):
        err = code.get("Error") or {}
        error_code = str(err.get("Code") or "")
        meta = code.get("ResponseMetadata") or {}
        status = meta.get("HTTPStatusCode")

    target = f"s3://{bucket}/{key}"
    if error_code in {"NoSuchKey", "NotFound", "404"} or name in {
        "NoSuchKey",
        "404",
    }:
        return S3SeedNotFoundError(f"Object not found: {target}")
    if status == 404:
        return S3SeedNotFoundError(f"Object not found: {target}")

    message = f"S3 {action} failed for {target}: {exc}"
    transient = (
        error_code in _TRANSIENT_ERROR_CODES
        or status in _TRANSIENT_HTTP_STATUS
        or name
        in {
            "EndpointConnectionError",
            "ConnectionClosedError",
            "ConnectTimeoutError",
            "ReadTimeoutError",
            "ResponseStreamingError",
        }
    )
    if transient:
        return S3SeedTransientError(message)
    return S3SeedPermanentError(message)


class S3SeedAdapter:
    """Put/get ``seed.age`` and get/put/append ``recipients.txt`` at fixed keys.

    Retries transient failures with exponential backoff. Permanent failures
    surface as ``S3SeedPermanentError``. Uploads are a single put of the
    complete blob so a failed attempt never leaves a truncated object under
    the fixed key (S3 replaces the object only on a successful PutObject).
    """

    def __init__(
        self,
        location: SeedLocation,
        store: ObjectStore | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise S3SeedConfigError("max_attempts must be >= 1")
        self.location = location
        self._store = store if store is not None else Boto3ObjectStore()
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        self._sleep = sleep

    def put_seed(self, ciphertext: bytes) -> str:
        """Overwrite ``seed.age`` with the complete encrypted blob."""
        if not ciphertext:
            raise S3SeedConfigError("seed ciphertext must be non-empty")
        key = self.location.seed_key
        self._with_retry("put_seed", lambda: self._store.put(
            self.location.bucket, key, ciphertext
        ))
        return self.location.uri_for(SEED_OBJECT_NAME)

    def get_seed(self) -> bytes:
        """Download the encrypted seed; raises if the object is missing."""
        key = self.location.seed_key
        return self._with_retry(
            "get_seed",
            lambda: self._store.get(self.location.bucket, key),
        )

    def put_recipients(self, text: str) -> str:
        """Overwrite ``recipients.txt`` (cleartext age public keys)."""
        body = text if text.endswith("\n") or text == "" else text + "\n"
        key = self.location.recipients_key
        self._with_retry(
            "put_recipients",
            lambda: self._store.put(
                self.location.bucket, key, body.encode("utf-8")
            ),
        )
        return self.location.uri_for(RECIPIENTS_OBJECT_NAME)

    def get_recipients(self, *, missing_ok: bool = False) -> str:
        """Read ``recipients.txt``.

        When ``missing_ok`` is True (new-machine bootstrap), a missing object
        returns an empty string instead of raising.
        """
        key = self.location.recipients_key

        def _read() -> str:
            try:
                raw = self._store.get(self.location.bucket, key)
            except S3SeedNotFoundError:
                if missing_ok:
                    return ""
                raise
            return raw.decode("utf-8")

        return self._with_retry("get_recipients", _read)

    def append_recipient(
        self,
        age_public_key: str,
        *,
        comment: str | None = None,
    ) -> str:
        """Append a public key if absent; works before the seed is decryptable."""
        key = validate_age_public_key(age_public_key)
        existing = self.get_recipients(missing_ok=True)
        if key in parse_recipients(existing):
            return self.location.uri_for(RECIPIENTS_OBJECT_NAME)
        updated = format_recipients(
            [key], existing_text=existing, new_comment=comment
        )
        return self.put_recipients(updated)

    def _with_retry[T](self, action: str, fn: Callable[[], T]) -> T:
        delay = self._backoff_base_s
        last_transient: S3SeedTransientError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return fn()
            except S3SeedNotFoundError:
                raise
            except S3SeedConfigError:
                raise
            except S3SeedPermanentError:
                raise
            except S3SeedTransientError as exc:
                last_transient = exc
                if attempt >= self._max_attempts:
                    break
                logger.warning(
                    "Transient S3 error on %s (attempt %s/%s): %s; retrying in %.2fs",
                    action,
                    attempt,
                    self._max_attempts,
                    exc,
                    delay,
                )
                self._sleep(delay)
                delay *= 2
            except Exception as exc:
                # Unexpected store bugs: do not retry forever.
                raise S3SeedPermanentError(
                    f"Unexpected error during {action}: {exc}"
                ) from exc
        assert last_transient is not None
        raise S3SeedTransientError(
            f"{action} failed after {self._max_attempts} attempts: "
            f"{last_transient}"
        ) from last_transient
