from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite

from app.db.repositories import (
    AccessEventsRepository,
    TrialAccessesRepository,
    TrialSettingsRepository,
    UserRecord,
    UsersRepository,
)
from app.utils.datetime import datetime_to_iso, utc_now


@dataclass(frozen=True, slots=True)
class TrialGrantResult:
    status: str
    user: UserRecord
    expires_at: datetime | None = None
    trial_id: int | None = None


def user_has_active_access(user: UserRecord) -> bool:
    return user.access_until is not None and user.access_until > utc_now()


class TrialService:
    """Issue one database-backed trial per Telegram account."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        self.users = UsersRepository(db)
        self.settings = TrialSettingsRepository(db)
        self.trials = TrialAccessesRepository(db)
        self.events = AccessEventsRepository(db)

    async def is_eligible(self, user: UserRecord) -> bool:
        settings = await self.settings.get()
        if not settings.enabled or user_has_active_access(user):
            return False
        return await self.trials.get_by_user_id(user.id) is None

    async def grant(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> TrialGrantResult:
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            user = await self.users.get_by_telegram_id(telegram_user_id)
            user = await self.users.upsert_telegram_user(telegram_user_id, username, first_name, last_name)

            settings = await self.settings.get()
            if not settings.enabled:
                await self.db.commit()
                return TrialGrantResult(status="disabled", user=user)
            if user_has_active_access(user):
                await self.db.commit()
                return TrialGrantResult(status="active_access", user=user, expires_at=user.access_until)
            if await self.trials.get_by_user_id(user.id) is not None:
                await self.db.commit()
                return TrialGrantResult(status="already_used", user=user)

            started_at = utc_now()
            expires_at = started_at + timedelta(days=settings.duration_days)
            trial = await self.trials.create(user.id, user.telegram_user_id, started_at, expires_at)
            await self.users.set_access(user.id, expires_at, "trial", trial.id)
            await self.events.add(
                telegram_user_id=user.telegram_user_id,
                user_id=user.id,
                event_type="trial_issued",
                details={
                    "trial_id": trial.id,
                    "duration_days": settings.duration_days,
                    "started_at": datetime_to_iso(started_at),
                    "expires_at": datetime_to_iso(expires_at),
                },
            )
            updated = await self.users.get_by_id(user.id)
            if updated is None:
                raise RuntimeError("failed to reload trial user")
            await self.db.commit()
            return TrialGrantResult(status="issued", user=updated, expires_at=expires_at, trial_id=trial.id)
        except Exception:
            await self.db.rollback()
            raise
