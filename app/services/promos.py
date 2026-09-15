from dataclasses import dataclass
from datetime import datetime
import secrets

import aiosqlite

from app.db.repositories import AccessEventsRepository, PromoCodeRecord, PromoCodesRepository, UserRecord, UsersRepository
from app.services.access import calculate_access_extension
from app.utils.datetime import datetime_to_iso, utc_now


PROMO_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass(frozen=True, slots=True)
class PromoRedemptionResult:
    status: str
    promo: PromoCodeRecord | None
    user: UserRecord | None
    access_until: datetime | None


class PromoService:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        self.codes = PromoCodesRepository(db)
        self.users = UsersRepository(db)
        self.events = AccessEventsRepository(db)

    async def create(self, duration_days: int, admin_telegram_user_id: int) -> PromoCodeRecord:
        if not 1 <= duration_days <= 1000:
            raise ValueError("promo duration must be between 1 and 1000")
        for _ in range(10):
            code = "".join(secrets.choice(PROMO_ALPHABET) for _ in range(8))
            try:
                promo = await self.codes.create(code, duration_days, admin_telegram_user_id)
            except aiosqlite.IntegrityError:
                continue
            await self.events.add(
                telegram_user_id=admin_telegram_user_id,
                event_type="promo_created",
                details={"promo_id": promo.id, "code": promo.code, "duration_days": duration_days},
            )
            await self.db.commit()
            return promo
        raise RuntimeError("unable to create unique promo code")

    async def redeem(
        self,
        promo_id: int,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> PromoRedemptionResult:
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            promo = await self.codes.get_by_id(promo_id)
            if promo is None:
                await self.db.commit()
                return PromoRedemptionResult("not_found", None, None, None)
            if promo.status != "created":
                await self.db.commit()
                status = "already_redeemed" if promo.status == "redeemed" else promo.status
                return PromoRedemptionResult(status, promo, None, None)
            user = await self.users.upsert_telegram_user(telegram_user_id, username, first_name, last_name)
            if not await self.codes.redeem(promo.id, user.id):
                await self.db.commit()
                current = await self.codes.get_by_id(promo.id)
                return PromoRedemptionResult("already_redeemed" if current else "not_found", current, None, None)
            extension = calculate_access_extension(user.access_until, promo.duration_days, utc_now())
            await self.users.set_access(user.id, extension.new_access_until, "promo", promo.id)
            await self.events.add(
                telegram_user_id=user.telegram_user_id,
                user_id=user.id,
                event_type="promo_redeemed",
                details={
                    "promo_id": promo.id, "code": promo.code, "duration_days": promo.duration_days,
                    "previous_access_until": datetime_to_iso(extension.previous_access_until),
                    "new_access_until": datetime_to_iso(extension.new_access_until),
                },
            )
            updated = await self.users.get_by_id(user.id)
            await self.db.commit()
            return PromoRedemptionResult("redeemed", promo, updated, extension.new_access_until)
        except Exception:
            await self.db.rollback()
            raise
