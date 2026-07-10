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

    async def list_warning_due(self, now: datetime, warning_until: datetime) -> list[UserRecord]:
        rows = await self.db.execute_fetchall(
            """
            SELECT * FROM users
            WHERE access_until IS NOT NULL
              AND access_until > ?
              AND access_until <= ?
              AND (warned_access_until IS NULL OR warned_access_until != access_until)
            """,
            (datetime_to_iso(now), datetime_to_iso(warning_until)),
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
    ) -> InviteLinkRecord:
        now = datetime_to_iso(utc_now())
        await self.db.execute(
            """
            INSERT INTO invite_links
                (user_id, payment_id, invite_link, telegram_invite_link_id, status, expires_at, created_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (user_id, payment_id, invite_link, telegram_invite_link_id, datetime_to_iso(expires_at), now),
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
