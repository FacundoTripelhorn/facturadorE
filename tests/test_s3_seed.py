"""S3 seed storage adapter (FAC-45) against an in-memory stub — no real bucket."""

from __future__ import annotations

import pytest

from facturador.constants import ArcaEnvironment
from facturador.s3_seed import (
    SEED_OBJECT_NAME,
    Boto3ObjectStore,
    MemoryObjectStore,
    S3SeedAdapter,
    S3SeedConfigError,
    S3SeedNotFoundError,
    S3SeedPermanentError,
    S3SeedTransientError,
    SeedLocation,
    _map_boto_error,
    format_recipients,
    parse_recipients,
)

# Synthetic age1 keys (shape only; not real identities).
AGE_KEY_A = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
AGE_KEY_B = "age1ygutdp4klvvdvxqzk68e37ndy2n4ctsjvmdngm359e9qmjsjtqcspwltdk"


@pytest.fixture
def location() -> SeedLocation:
    return SeedLocation.from_config(
        bucket="user-private-bucket",
        prefix="facturador",
        cuit="20123456789",
        environment="homo",
    )


@pytest.fixture
def adapter(location: SeedLocation) -> S3SeedAdapter:
    return S3SeedAdapter(
        location, MemoryObjectStore(), max_attempts=3, backoff_base_s=0
    )


def test_fixed_key_layout(location: SeedLocation) -> None:
    assert location.seed_key == "facturador/20123456789/homo/seed.age"
    assert location.recipients_key == "facturador/20123456789/homo/recipients.txt"
    assert location.uri_for(SEED_OBJECT_NAME) == (
        "s3://user-private-bucket/facturador/20123456789/homo/seed.age"
    )


def test_prefix_normalized_and_bucket_required() -> None:
    loc = SeedLocation.from_config(
        bucket="  b  ",
        prefix="/pfx/",
        cuit="20123456789",
        environment=ArcaEnvironment.PROD,
    )
    assert loc.bucket == "b"
    assert loc.prefix == "pfx"
    assert loc.seed_key == "pfx/20123456789/prod/seed.age"

    empty = SeedLocation.from_config(
        bucket="ok",
        prefix="",
        cuit="20123456789",
        environment="homo",
    )
    assert empty.seed_key == "20123456789/homo/seed.age"

    with pytest.raises(S3SeedConfigError, match="backup_s3_bucket"):
        SeedLocation.from_config(
            bucket="",
            prefix="facturador",
            cuit="20123456789",
            environment="homo",
        )

    with pytest.raises(S3SeedConfigError, match="CUIT"):
        SeedLocation.from_config(
            bucket="b",
            prefix="p",
            cuit="20-123",
            environment="homo",
        )


def test_put_get_seed_overwrite(adapter: S3SeedAdapter) -> None:
    uri = adapter.put_seed(b"AGE-ENCRYPTED-v1")
    assert uri.endswith("/seed.age")
    assert adapter.get_seed() == b"AGE-ENCRYPTED-v1"

    adapter.put_seed(b"AGE-ENCRYPTED-v2")
    assert adapter.get_seed() == b"AGE-ENCRYPTED-v2"


def test_get_seed_missing_raises(adapter: S3SeedAdapter) -> None:
    with pytest.raises(S3SeedNotFoundError):
        adapter.get_seed()


def test_put_seed_rejects_empty(adapter: S3SeedAdapter) -> None:
    with pytest.raises(S3SeedConfigError, match="non-empty"):
        adapter.put_seed(b"")


def test_recipients_put_get_and_append_bootstrap(adapter: S3SeedAdapter) -> None:
    # New machine: recipients.txt may not exist yet.
    assert adapter.get_recipients(missing_ok=True) == ""

    uri = adapter.append_recipient(AGE_KEY_A, comment="machine-a")
    assert uri.endswith("/recipients.txt")
    text = adapter.get_recipients()
    assert AGE_KEY_A in parse_recipients(text)
    assert "machine-a" in text

    # Second machine appends without decrypting the seed.
    adapter.append_recipient(AGE_KEY_B, comment="machine-b")
    keys = parse_recipients(adapter.get_recipients())
    assert keys == [AGE_KEY_A, AGE_KEY_B]

    # Idempotent append.
    adapter.append_recipient(AGE_KEY_A)
    assert parse_recipients(adapter.get_recipients()) == [AGE_KEY_A, AGE_KEY_B]


def test_put_recipients_overwrite(adapter: S3SeedAdapter) -> None:
    adapter.put_recipients(f"# only a\n{AGE_KEY_A}\n")
    adapter.put_recipients(f"{AGE_KEY_B}\n")
    assert parse_recipients(adapter.get_recipients()) == [AGE_KEY_B]


def test_get_recipients_missing_strict(adapter: S3SeedAdapter) -> None:
    with pytest.raises(S3SeedNotFoundError):
        adapter.get_recipients(missing_ok=False)


def test_append_rejects_garbage_key(adapter: S3SeedAdapter) -> None:
    with pytest.raises(S3SeedConfigError, match="age"):
        adapter.append_recipient("not-an-age-key")


def test_parse_and_format_recipients() -> None:
    body = f"# comment\n{AGE_KEY_A}  # inline\n\n{AGE_KEY_B}\n"
    assert parse_recipients(body) == [AGE_KEY_A, AGE_KEY_B]
    extra = "age1" + ("a" * 58)
    updated = format_recipients(
        [AGE_KEY_A, extra],
        existing_text=body,
        new_comment="new",
    )
    assert AGE_KEY_A in updated
    assert "new" in updated
    # Existing key is not duplicated.
    assert updated.count(AGE_KEY_A) == 1


class _FlakyStore(MemoryObjectStore):
    def __init__(self, fail_times: int, exc: Exception) -> None:
        super().__init__()
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0

    def put(self, bucket: str, key: str, body: bytes) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        super().put(bucket, key, body)


def test_retries_transient_then_succeeds(location: SeedLocation) -> None:
    store = _FlakyStore(2, S3SeedTransientError("SlowDown"))
    sleeps: list[float] = []
    adapter = S3SeedAdapter(
        location,
        store,
        max_attempts=4,
        backoff_base_s=0.5,
        sleep=sleeps.append,
    )
    adapter.put_seed(b"ok")
    assert adapter.get_seed() == b"ok"
    assert store.calls == 3
    assert sleeps == [0.5, 1.0]


def test_retries_exhausted_surfaces_transient(location: SeedLocation) -> None:
    store = _FlakyStore(10, S3SeedTransientError("503"))
    adapter = S3SeedAdapter(
        location, store, max_attempts=3, backoff_base_s=0, sleep=lambda _: None
    )
    with pytest.raises(S3SeedTransientError, match="3 attempts"):
        adapter.put_seed(b"x")


def test_permanent_error_not_retried(location: SeedLocation) -> None:
    store = _FlakyStore(10, S3SeedPermanentError("AccessDenied"))
    adapter = S3SeedAdapter(
        location, store, max_attempts=5, backoff_base_s=0, sleep=lambda _: None
    )
    with pytest.raises(S3SeedPermanentError, match="AccessDenied"):
        adapter.put_seed(b"x")
    assert store.calls == 1


def test_map_boto_error_classification() -> None:
    class FakeClientError(Exception):
        def __init__(self, code: str, status: int) -> None:
            super().__init__(code)
            self.response = {
                "Error": {"Code": code},
                "ResponseMetadata": {"HTTPStatusCode": status},
            }

    assert isinstance(
        _map_boto_error(
            FakeClientError("NoSuchKey", 404),
            action="get",
            bucket="b",
            key="k",
        ),
        S3SeedNotFoundError,
    )
    assert isinstance(
        _map_boto_error(
            FakeClientError("SlowDown", 503),
            action="put",
            bucket="b",
            key="k",
        ),
        S3SeedTransientError,
    )
    assert isinstance(
        _map_boto_error(
            FakeClientError("AccessDenied", 403),
            action="put",
            bucket="b",
            key="k",
        ),
        S3SeedPermanentError,
    )


def test_boto3_store_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeBody:
        def read(self) -> bytes:
            return b"seed"

    class FakeClient:
        def __init__(self) -> None:
            self.puts: list[tuple[str, str, bytes]] = []

        def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict:
            self.puts.append((Bucket, Key, Body))
            return {}

        def get_object(self, *, Bucket: str, Key: str) -> dict:
            if Key.endswith("missing"):
                err = type("NoSuchKey", (Exception,), {})("missing")
                err.response = {  # type: ignore[attr-defined]
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                }
                raise err
            return {"Body": FakeBody()}

    client = FakeClient()
    store = Boto3ObjectStore(client=client)
    store.put("b", "k", b"data")
    assert client.puts == [("b", "k", b"data")]
    assert store.get("b", "k") == b"seed"
    with pytest.raises(S3SeedNotFoundError):
        store.get("b", "missing")
