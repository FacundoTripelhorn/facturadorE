"""Helpers compartidos por los módulos del repo."""

from __future__ import annotations

import datetime as dt
import uuid


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex
