from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiosqlite

from app.utils.datetime import datetime_to_iso, iso_to_datetime, utc_now
from app.utils.json import dumps_compact


@dataclass(slots=True)
class UserRecord:
    id: int
    telegram_user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    access_until: datetime | None
    access_kind: str | None
    access_source_id: int | None
    is_in_group: bool
    warned_access_until: str | None


@dataclass(slots=True)
class TariffRecord:
    id: int
    code: str
    title: str
    description: str
    price_amount: int
    currency: str
    duration_days: int
    is_active: bool
    sort_order: int


@dataclass(slots=True)
class PaymentRecord:
    id: int
    user_id: int
    tariff_id: int
    provider: str
    invoice_id: str | None
    order_id: str
    status: str
    amount: int
    currency: str
    payment_url: str | None
    created_at: datetime
    paid_at: datetime | None
    expires_at: datetime | None
    raw_payload: str
    applied_at: datetime | None
    admin_notification_attempted_at: datetime | None


@dataclass(slots=True)
class InviteLinkRecord:
    id: int
    user_id: int
    payment_id: int | None
    invite_link: str
    telegram_invite_link_id: str | None
    status: str
    expires_at: datetime
    created_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    access_kind: str | None
    access_source_id: int | None


@dataclass(slots=True)
class PromoCodeRecord:
    id: int
    code: str
    duration_days: int
    status: str
    created_by_telegram_user_id: int
    created_at: datetime
    redeemed_by_user_id: int | None
    redeemed_at: datetime | None
    cancelled_at: datetime | None


@dataclass(slots=True)
class TrialSettingsRecord:
    enabled: bool
    duration_days: int
    updated_at: datetime
    updated_by_telegram_user_id: int | None


@dataclass(slots=True)
class TrialAccessRecord:
    id: int
    user_id: int
    telegram_user_id: int
    started_at: datetime
    expires_at: datetime
    status: str
    issued_by: str
    warned_at: datetime | None


async def _fetchone(
    db: aiosqlite.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> aiosqlite.Row | None:
    cursor = await db.execute(sql, params)
    return await cursor.fetchone()


def _user(row: aiosqlite.Row | None) -> UserRecord | None:
    if row is None:
        return None
    return UserRecord(
        id=row["id"],
        telegram_user_id=row["telegram_user_id"],
        username=row["username"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        access_until=iso_to_datetime(row["access_until"]),
        access_kind=row["access_kind"],
        access_source_id=row["access_source_id"],
        is_in_group=bool(row["is_in_group"]),
        warned_access_until=row["warned_access_until"],
    )


def _tariff(row: aiosqlite.Row | None) -> TariffRecord | None:
    if row is None:
        return None
    return TariffRecord(
        id=row["id"],
        code=row["code"],
        title=row["title"],
        description=row["description"],
        price_amount=row["price_amount"],
        currency=row["currency"],
        duration_days=row["duration_days"],
        is_active=bool(row["is_active"]),
        sort_order=row["sort_order"],
    )


def _payment(row: aiosqlite.Row | None) -> PaymentRecord | None:
    if row is None:
        return None
    return PaymentRecord(
        id=row["id"],
        user_id=row["user_id"],
        tariff_id=row["tariff_id"],
        provider=row["provider"],
        invoice_id=row["invoice_id"],
        order_id=row["order_id"],
        status=row["status"],
        amount=row["amount"],
        currency=row["currency"],
        payment_url=row["payment_url"],
        created_at=iso_to_datetime(row["created_at"]) or utc_now(),
        paid_at=iso_to_datetime(row["paid_at"]),
        expires_at=iso_to_datetime(row["expires_at"]),
        raw_payload=row["raw_payload"],
        applied_at=iso_to_datetime(row["applied_at"]),
        admin_notification_attempted_at=iso_to_datetime(row["admin_notification_attempted_at"]),
    )


def _invite(row: aiosqlite.Row | None) -> InviteLinkRecord | None:
    if row is None:
        return None
    return InviteLinkRecord(
        id=row["id"],
        user_id=row["user_id"],
        payment_id=row["payment_id"],
        invite_link=row["invite_link"],
        telegram_invite_link_id=row["telegram_invite_link_id"],
        status=row["status"],
        expires_at=iso_to_datetime(row["expires_at"]) or utc_now(),
        created_at=iso_to_datetime(row["created_at"]) or utc_now(),
        used_at=iso_to_datetime(row["used_at"]),
        revoked_at=iso_to_datetime(row["revoked_at"]),
        access_kind=row["access_kind"],
        access_source_id=row["access_source_id"],
    )


def _promo(row: aiosqlite.Row | None) -> PromoCodeRecord | None:
    if row is None:
        return None
    return PromoCodeRecord(
        id=row["id"], code=row["code"], duration_days=row["duration_days"], status=row["status"],
        created_by_telegram_user_id=row["created_by_telegram_user_id"],
        created_at=iso_to_datetime(row["created_at"]) or utc_now(),
        redeemed_by_user_id=row["redeemed_by_user_id"], redeemed_at=iso_to_datetime(row["redeemed_at"]),
        cancelled_at=iso_to_datetime(row["cancelled_at"]),
    )


def _trial_settings(row: aiosqlite.Row | None) -> TrialSettingsRecord | None:
    if row is None:
        return None
    return TrialSettingsRecord(
        enabled=bool(row["enabled"]),
        duration_days=row["duration_days"],
        updated_at=iso_to_datetime(row["updated_at"]) or utc_now(),
        updated_by_telegram_user_id=row["updated_by_telegram_user_id"],
    )


def _trial_access(row: aiosqlite.Row | None) -> TrialAccessRecord | None:
    if row is None:
        return None
    return TrialAccessRecord(
        id=row["id"],
        user_id=row["user_id"],
        telegram_user_id=row["telegram_user_id"],
        started_at=iso_to_datetime(row["started_at"]) or utc_now(),
        expires_at=iso_to_datetime(row["expires_at"]) or utc_now(),
        status=row["status"],
        issued_by=row["issued_by"],
        warned_at=iso_to_datetime(row["warned_at"]),
    )


class UsersRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def upsert_telegram_user(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> UserRecord:
        now = datetime_to_iso(utc_now())
        await self.db.execute(
            """
            INSERT INTO users (telegram_user_id, username, first_name, last_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                updated_at = excluded.updated_at
            """,
            (telegram_user_id, username, first_name, last_name, now, now),
        )
        row = await _fetchone(
            self.db,
            "SELECT * FROM users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        user = _user(row)
        if user is None:
            raise RuntimeError("failed to upsert user")
        return user

    async def get_by_id(self, user_id: int) -> UserRecord | None:
        return _user(await _fetchone(self.db, "SELECT * FROM users WHERE id = ?", (user_id,)))

    async def get_by_telegram_id(self, telegram_user_id: int) -> UserRecord | None:
        return _user(
            await _fetchone(
                self.db,
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
        )

    async def set_access_until(self, user_id: int, access_until: datetime) -> None:
        await self.db.execute(
            "UPDATE users SET access_until = ?, updated_at = ? WHERE id = ?",
            (datetime_to_iso(access_until), datetime_to_iso(utc_now()), user_id),
        )

    async def set_access(
        self,
        user_id: int,
        access_until: datetime,
        access_kind: str,
        access_source_id: int | None,
    ) -> None:
        await self.db.execute(
            """
            UPDATE users
            SET access_until = ?, access_kind = ?, access_source_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (datetime_to_iso(access_until), access_kind, access_source_id, datetime_to_iso(utc_now()), user_id),
        )

    async def set_is_in_group(self, telegram_user_id: int, is_in_group: bool) -> None:
        await self.db.execute(
            "UPDATE users SET is_in_group = ?, updated_at = ? WHERE telegram_user_id = ?",
            (1 if is_in_group else 0, datetime_to_iso(utc_now()), telegram_user_id),
        )

    async def mark_warned(self, user_id: int, access_until: datetime) -> None:
        await self.db.execute(
            "UPDATE users SET warned_access_until = ?, updated_at = ? WHERE id = ?",
            (datetime_to_iso(access_until), datetime_to_iso(utc_now()), user_id),
        )

    async def list_expired_in_group(self, now: datetime) -> list[UserRecord]:
        rows = await self.db.execute_fetchall(
            """
            SELECT * FROM users
            WHERE is_in_group = 1 AND access_until IS NOT NULL AND access_until <= ?
            """,
            (datetime_to_iso(now),),
        )
        return [user for row in rows if (user := _user(row))]

    async def list_warning_due(
        self,
        now: datetime,
        trial_warning_until: datetime,
        standard_warning_until: datetime,
    ) -> list[UserRecord]:
        rows = await self.db.execute_fetchall(
            """
            SELECT * FROM users
            WHERE access_until IS NOT NULL
              AND access_until > ?
              AND (warned_access_until IS NULL OR warned_access_until != access_until)
              AND (
                  (access_kind = 'trial' AND access_until <= ?)
                  OR (COALESCE(access_kind, 'manual') != 'trial' AND access_until <= ?)
              )
            """,
            (datetime_to_iso(now), datetime_to_iso(trial_warning_until), datetime_to_iso(standard_warning_until)),
        )
        return [user for row in rows if (user := _user(row))]


class TariffsRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def upsert(
        self,
        code: str,
        title: str,
        description: str,
        price_amount: int,
        currency: str,
        duration_days: int,
        is_active: bool = True,
        sort_order: int = 100,
    ) -> TariffRecord:
        now = datetime_to_iso(utc_now())
        await self.db.execute(
            """
            INSERT INTO tariffs
                (code, title, description, price_amount, currency, duration_days, is_active, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                price_amount = excluded.price_amount,
                currency = excluded.currency,
                duration_days = excluded.duration_days,
                is_active = excluded.is_active,
                sort_order = excluded.sort_order,
                updated_at = excluded.updated_at
            """,
            (
                code,
                title,
                description,
                price_amount,
                currency.upper(),
                duration_days,
                1 if is_active else 0,
                sort_order,
                now,
                now,
            ),
        )
        row = await _fetchone(self.db, "SELECT * FROM tariffs WHERE code = ?", (code,))
        tariff = _tariff(row)
        if tariff is None:
            raise RuntimeError("failed to upsert tariff")
        return tariff

    async def get_by_code(self, code: str, active_only: bool = False) -> TariffRecord | None:
        sql = "SELECT * FROM tariffs WHERE code = ?"
        params: tuple[Any, ...] = (code,)
        if active_only:
            sql += " AND is_active = 1"
        return _tariff(await _fetchone(self.db, sql, params))

    async def get_by_id(self, tariff_id: int) -> TariffRecord | None:
        return _tariff(await _fetchone(self.db, "SELECT * FROM tariffs WHERE id = ?", (tariff_id,)))

    async def list_active(self) -> list[TariffRecord]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM tariffs WHERE is_active = 1 ORDER BY sort_order, price_amount, id"
        )
        return [tariff for row in rows if (tariff := _tariff(row))]

    async def list_all(self) -> list[TariffRecord]:
        rows = await self.db.execute_fetchall("SELECT * FROM tariffs ORDER BY sort_order, id")
        return [tariff for row in rows if (tariff := _tariff(row))]

    async def set_active(self, code: str, is_active: bool) -> bool:
        cursor = await self.db.execute(
            "UPDATE tariffs SET is_active = ?, updated_at = ? WHERE code = ?",
            (1 if is_active else 0, datetime_to_iso(utc_now()), code),
        )
        return cursor.rowcount > 0


class PaymentsRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def create(
        self,
        user_id: int,
        tariff_id: int,
        order_id: str,
        amount: int,
        currency: str,
        payment_url: str | None,
        invoice_id: str | None,
        expires_at: datetime | None,
        provider: str = "lava",
        raw_payload: dict[str, Any] | None = None,
    ) -> PaymentRecord:
        now = datetime_to_iso(utc_now())
        await self.db.execute(
            """
            INSERT INTO payments (
                user_id, tariff_id, provider, invoice_id, order_id, status,
                amount, currency, payment_url, created_at, expires_at, raw_payload
            )
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                tariff_id,
                provider,
                invoice_id,
                order_id,
                amount,
                currency,
                payment_url,
                now,
                datetime_to_iso(expires_at),
                dumps_compact(raw_payload or {}),
            ),
        )
        row = await _fetchone(
            self.db,
            "SELECT * FROM payments WHERE order_id = ?",
            (order_id,),
        )
        payment = _payment(row)
        if payment is None:
            raise RuntimeError("failed to create payment")
        return payment

    async def get_by_order_id(self, order_id: str) -> PaymentRecord | None:
        return _payment(
            await _fetchone(
                self.db,
                "SELECT * FROM payments WHERE order_id = ?",
                (order_id,),
            )
        )

    async def get_by_id(self, payment_id: int) -> PaymentRecord | None:
        return _payment(await _fetchone(self.db, "SELECT * FROM payments WHERE id = ?", (payment_id,)))

    async def get_by_invoice_id(self, invoice_id: str) -> PaymentRecord | None:
        return _payment(
            await _fetchone(
                self.db,
                "SELECT * FROM payments WHERE invoice_id = ?",
                (invoice_id,),
            )
        )

    async def mark_paid(
        self,
        payment_id: int,
        invoice_id: str | None,
        paid_at: datetime,
        raw_payload: dict[str, Any],
    ) -> None:
        await self.db.execute(
            """
            UPDATE payments
            SET status = 'paid',
                invoice_id = COALESCE(?, invoice_id),
                paid_at = COALESCE(paid_at, ?),
                raw_payload = ?
            WHERE id = ?
            """,
            (invoice_id, datetime_to_iso(paid_at), dumps_compact(raw_payload), payment_id),
        )

    async def mark_applied(self, payment_id: int, applied_at: datetime) -> None:
        await self.db.execute(
            "UPDATE payments SET applied_at = ?, status = 'applied' WHERE id = ? AND applied_at IS NULL",
            (datetime_to_iso(applied_at), payment_id),
        )

    async def claim_admin_notification(self, payment_id: int) -> bool:
        cursor = await self.db.execute(
            "UPDATE payments SET admin_notification_attempted_at = ? "
            "WHERE id = ? AND admin_notification_attempted_at IS NULL",
            (datetime_to_iso(utc_now()), payment_id),
        )
        return cursor.rowcount == 1

    async def mark_status(
        self,
        payment_id: int,
        status: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        await self.db.execute(
            "UPDATE payments SET status = ?, raw_payload = COALESCE(?, raw_payload) WHERE id = ?",
            (status, dumps_compact(raw_payload) if raw_payload is not None else None, payment_id),
        )

    async def list_pending_for_check(self, limit: int = 50, provider: str = "lava") -> list[PaymentRecord]:
        rows = await self.db.execute_fetchall(
            """
            SELECT * FROM payments
            WHERE status = 'pending' AND provider = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (provider, limit),
        )
        return [payment for row in rows if (payment := _payment(row))]


class InviteLinksRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def create(
        self,
        user_id: int,
        payment_id: int | None,
        invite_link: str,
        telegram_invite_link_id: str | None,
        expires_at: datetime,
        access_kind: str | None = None,
        access_source_id: int | None = None,
    ) -> InviteLinkRecord:
        now = datetime_to_iso(utc_now())
        await self.db.execute(
            """
            INSERT INTO invite_links
                (user_id, payment_id, invite_link, telegram_invite_link_id, status, expires_at, created_at, access_kind, access_source_id)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (user_id, payment_id, invite_link, telegram_invite_link_id, datetime_to_iso(expires_at), now, access_kind, access_source_id),
        )
        row = await _fetchone(self.db, "SELECT * FROM invite_links WHERE invite_link = ?", (invite_link,))
        invite = _invite(row)
        if invite is None:
            raise RuntimeError("failed to create invite link")
        return invite

    async def get_active_by_user(self, user_id: int, now: datetime) -> InviteLinkRecord | None:
        return _invite(
            await _fetchone(
                self.db,
                """
                SELECT * FROM invite_links
                WHERE user_id = ? AND status = 'active' AND expires_at > ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id, datetime_to_iso(now)),
            )
        )

    async def get_active_by_link(self, invite_link: str) -> InviteLinkRecord | None:
        return _invite(
            await _fetchone(
                self.db,
                "SELECT * FROM invite_links WHERE invite_link = ? AND status = 'active'",
                (invite_link,),
            )
        )

    async def mark_used(self, invite_id: int, used_at: datetime) -> None:
        await self.db.execute(
            "UPDATE invite_links SET status = 'used', used_at = ? WHERE id = ? AND status = 'active'",
            (datetime_to_iso(used_at), invite_id),
        )

    async def mark_revoked(self, invite_id: int, revoked_at: datetime) -> None:
        await self.db.execute(
            "UPDATE invite_links SET status = 'revoked', revoked_at = ? WHERE id = ?",
            (datetime_to_iso(revoked_at), invite_id),
        )


class PromoCodesRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def create(self, code: str, duration_days: int, created_by_telegram_user_id: int) -> PromoCodeRecord:
        await self.db.execute(
            "INSERT INTO promo_codes (code, duration_days, status, created_by_telegram_user_id, created_at) "
            "VALUES (?, ?, 'created', ?, ?)",
            (code, duration_days, created_by_telegram_user_id, datetime_to_iso(utc_now())),
        )
        promo = await self.get_by_code(code)
        if promo is None:
            raise RuntimeError("failed to create promo code")
        return promo

    async def get_by_code(self, code: str) -> PromoCodeRecord | None:
        return _promo(await _fetchone(self.db, "SELECT * FROM promo_codes WHERE code = ?", (code,)))

    async def get_by_id(self, promo_id: int) -> PromoCodeRecord | None:
        return _promo(await _fetchone(self.db, "SELECT * FROM promo_codes WHERE id = ?", (promo_id,)))

    async def list_recent(self, created_by_telegram_user_id: int, limit: int = 10) -> list[PromoCodeRecord]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM promo_codes WHERE created_by_telegram_user_id = ? ORDER BY id DESC LIMIT ?",
            (created_by_telegram_user_id, limit),
        )
        return [promo for row in rows if (promo := _promo(row))]

    async def cancel(self, promo_id: int) -> bool:
        cursor = await self.db.execute(
            "UPDATE promo_codes SET status = 'cancelled', cancelled_at = ? WHERE id = ? AND status = 'created'",
            (datetime_to_iso(utc_now()), promo_id),
        )
        return cursor.rowcount == 1

    async def redeem(self, promo_id: int, user_id: int) -> bool:
        cursor = await self.db.execute(
            "UPDATE promo_codes SET status = 'redeemed', redeemed_by_user_id = ?, redeemed_at = ? "
            "WHERE id = ? AND status = 'created'",
            (user_id, datetime_to_iso(utc_now()), promo_id),
        )
        return cursor.rowcount == 1

    async def has_redeemed_by_user(self, user_id: int) -> bool:
        return (await _fetchone(self.db, "SELECT 1 FROM promo_codes WHERE redeemed_by_user_id = ? LIMIT 1", (user_id,))) is not None


class TrialSettingsRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get(self) -> TrialSettingsRecord:
        row = await _fetchone(self.db, "SELECT * FROM trial_settings WHERE id = 1")
        settings = _trial_settings(row)
        if settings is None:
            raise RuntimeError("trial settings are not initialized")
        return settings

    async def set(self, enabled: bool, duration_days: int, updated_by_telegram_user_id: int) -> TrialSettingsRecord:
        now = datetime_to_iso(utc_now())
        await self.db.execute(
            """
            UPDATE trial_settings
            SET enabled = ?, duration_days = ?, updated_at = ?, updated_by_telegram_user_id = ?
            WHERE id = 1
            """,
            (1 if enabled else 0, duration_days, now, updated_by_telegram_user_id),
        )
        return await self.get()


class TrialAccessesRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get_by_user_id(self, user_id: int) -> TrialAccessRecord | None:
        return _trial_access(await _fetchone(self.db, "SELECT * FROM trial_accesses WHERE user_id = ?", (user_id,)))

    async def create(
        self,
        user_id: int,
        telegram_user_id: int,
        started_at: datetime,
        expires_at: datetime,
        issued_by: str = "bot",
    ) -> TrialAccessRecord:
        now = datetime_to_iso(utc_now())
        cursor = await self.db.execute(
            """
            INSERT INTO trial_accesses (
                user_id, telegram_user_id, started_at, expires_at, status, issued_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                user_id,
                telegram_user_id,
                datetime_to_iso(started_at),
                datetime_to_iso(expires_at),
                issued_by,
                now,
                now,
            ),
        )
        record = await self.get_by_user_id(user_id)
        if record is None or cursor.lastrowid != record.id:
            raise RuntimeError("failed to create trial access")
        return record

    async def set_status(self, user_id: int, status: str) -> None:
        await self.db.execute(
            "UPDATE trial_accesses SET status = ?, updated_at = ? WHERE user_id = ? AND status = 'active'",
            (status, datetime_to_iso(utc_now()), user_id),
        )

    async def mark_warned(self, user_id: int, warned_at: datetime) -> None:
        await self.db.execute(
            "UPDATE trial_accesses SET warned_at = ?, updated_at = ? WHERE user_id = ?",
            (datetime_to_iso(warned_at), datetime_to_iso(utc_now()), user_id),
        )

    async def mark_expired_due(self, now: datetime) -> None:
        await self.db.execute(
            """
            UPDATE trial_accesses
            SET status = 'expired', updated_at = ?
            WHERE status = 'active' AND expires_at <= ?
            """,
            (datetime_to_iso(now), datetime_to_iso(now)),
        )


class AccessEventsRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def add(
        self,
        telegram_user_id: int,
        event_type: str,
        details: dict[str, Any] | None = None,
        user_id: int | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO access_events (user_id, telegram_user_id, event_type, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, telegram_user_id, event_type, dumps_compact(details or {}), datetime_to_iso(utc_now())),
        )
