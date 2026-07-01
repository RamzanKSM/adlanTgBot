from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from app.db.repositories import AccessEventsRepository, UserRecord, UsersRepository
from app.utils.datetime import add_days_from_base, datetime_to_iso, utc_now


@dataclass(frozen=True, slots=True)
class AccessExtension:
    previous_access_until: datetime | None
    paid_at: datetime
    duration_days: int
    new_access_until: datetime


@dataclass(frozen=True, slots=True)
class ManualAccessGrant:
    user: UserRecord
    access_extension: AccessExtension


def calculate_access_extension(
    current_access_until: datetime | None,
    duration_days: int,
    paid_at: datetime | None = None,
) -> AccessExtension:
    effective_paid_at = paid_at or utc_now()
    new_access_until = add_days_from_base(current_access_until, effective_paid_at, duration_days)
    return AccessExtension(
        previous_access_until=current_access_until,
        paid_at=effective_paid_at,
        duration_days=duration_days,
        new_access_until=new_access_until,
    )


async def grant_manual_access(
    db: aiosqlite.Connection,
    telegram_user_id: int,
    duration_days: int,
    granted_by_telegram_user_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    granted_at: datetime | None = None,
) -> ManualAccessGrant:
    users = UsersRepository(db)
    user = await users.get_by_telegram_id(telegram_user_id)
    if user is None or username is not None or first_name is not None or last_name is not None:
        user = await users.upsert_telegram_user(
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )

    extension = calculate_access_extension(user.access_until, duration_days, granted_at)
    await users.set_access_until(user.id, extension.new_access_until)
    await AccessEventsRepository(db).add(
        telegram_user_id=user.telegram_user_id,
        user_id=user.id,
        event_type="manual_grant",
        details={
            "granted_by_telegram_user_id": granted_by_telegram_user_id,
            "duration_days": duration_days,
            "previous_access_until": datetime_to_iso(extension.previous_access_until),
            "new_access_until": datetime_to_iso(extension.new_access_until),
        },
    )
    updated_user = await users.get_by_id(user.id)
    if updated_user is None:
        raise RuntimeError("failed to reload user after manual access grant")
    return ManualAccessGrant(user=updated_user, access_extension=extension)
