"""Validation at the trusted CLI boundary, separate from model arguments."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json

CST = timezone(timedelta(hours=8))


def timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("time must be an ISO 8601 string with timezone")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid ISO 8601 time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone is required, e.g. +08:00")
    if parsed.second or parsed.microsecond:
        raise ValueError("slot precision is minutes; seconds must be zero")
    return parsed.astimezone(CST).isoformat(timespec="minutes")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Request:
    request_id: str
    material_ids: tuple[str, ...]
    requested_at: str
    channel: str = "main"
    hard_time: bool = True
    confirmed_alternatives: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: dict):
        if not isinstance(raw, dict):
            raise ValueError("input must be an object")
        allowed = {"request_id", "material_ids", "requested_at", "channel",
                   "hard_time", "confirmed_alternatives"}
        if set(raw) - allowed:
            raise ValueError("unknown request fields")
        ids = raw.get("material_ids")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 10
                or any(not isinstance(x, str) or not x.strip() for x in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("material_ids must contain 1..10 distinct nonempty strings")
        for key in ("request_id", "channel"):
            value = raw.get(key, "main" if key == "channel" else None)
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{key} must be a nonempty string, at most 128 chars")
        if type(raw.get("hard_time", True)) is not bool:
            raise ValueError("hard_time must be boolean")
        confirmed = raw.get("confirmed_alternatives", {})
        if not isinstance(confirmed, dict) or set(confirmed) - set(ids):
            raise ValueError("confirmation keys must be requested material IDs")
        return cls(raw["request_id"], tuple(ids), timestamp(raw.get("requested_at")),
                   raw.get("channel", "main"), raw.get("hard_time", True),
                   {key: timestamp(value) for key, value in confirmed.items()})

    def as_dict(self):
        return {"request_id": self.request_id, "material_ids": list(self.material_ids),
                "requested_at": self.requested_at, "channel": self.channel,
                "hard_time": self.hard_time,
                "confirmed_alternatives": self.confirmed_alternatives}

    def fingerprint(self):
        value = self.as_dict()
        value.pop("request_id")
        return digest(value)

    def operation_key(self, material_id):
        # Alternate confirmation updates the same logical draft, not a second draft.
        return digest([material_id, self.channel, self.requested_at])
