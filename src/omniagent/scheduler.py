from __future__ import annotations

from datetime import datetime, timezone

from omniagent.models import PendingCheckpoint, PendingCheckpointType, utc_now
from omniagent.storage import SQLiteStore


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class TaskScheduler:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def due_timer_checkpoints(self, *, now: str | None = None) -> list[PendingCheckpoint]:
        current = _parse_timestamp(now) or _parse_timestamp(utc_now())
        assert current is not None
        due: list[PendingCheckpoint] = []
        for checkpoint in self.store.list_pending_checkpoints():
            if checkpoint.checkpoint_type != PendingCheckpointType.TIMER:
                continue
            resume_at = _parse_timestamp(
                checkpoint.payload.get("resume_at") or checkpoint.payload.get("wait_until")
            )
            if resume_at is None or resume_at > current:
                continue
            due.append(checkpoint)
        return due

    def expired_checkpoints(self, *, now: str | None = None) -> list[PendingCheckpoint]:
        current = _parse_timestamp(now) or _parse_timestamp(utc_now())
        assert current is not None
        expired: list[PendingCheckpoint] = []
        for checkpoint in self.store.list_pending_checkpoints():
            expires_at = _parse_timestamp(checkpoint.payload.get("expires_at"))
            if expires_at is None or expires_at > current:
                continue
            expired.append(checkpoint)
        return expired

    def checkpoints_for_event(self, event_key: str) -> list[PendingCheckpoint]:
        matched: list[PendingCheckpoint] = []
        for checkpoint in self.store.list_pending_checkpoints():
            if checkpoint.checkpoint_type != PendingCheckpointType.EXTERNAL_EVENT:
                continue
            if str(checkpoint.payload.get("event_key", "")) != event_key:
                continue
            matched.append(checkpoint)
        return matched
