"""
Database and business logic layer for Dalsjöfors Hyrservice.

This module is responsible for initialising the SQLite database, defining
the schema and implementing core business rules such as price calculation,
availability checking and booking creation.  The database is stored on
disk (``database.db``) so that bookings persist across restarts.  All
functions in this module are deterministic and free of side‑effects other
than writing to the database when creating or updating bookings.

The following enumerations are used throughout the code:

- ``TrailerType``: ``"GALLER"`` or ``"KAP"`` – the two trailer variants.
- ``RentalType``: ``"TWO_HOURS"`` or ``"FULL_DAY"`` – short or long hire.
- ``Status``: ``"PENDING_PAYMENT"``, ``"CONFIRMED"`` or ``"CANCELLED"`` –
  lifecycle of a booking.

Business rules implemented here:

* **Price calculation** – two hour bookings always cost 200 SEK.  Full
  day bookings cost 250 SEK on Mondays through Thursdays and 300 SEK on
  Fridays through Sundays.  The calculation uses the local date of
  ``start_datetime`` to determine the weekday.
* **Availability** – there are two trailers of each type.  A new booking
  cannot overlap any existing non‑cancelled bookings such that more than
  two trailers would be in use at the same time.  Overlap is defined as
  ``startA < endB`` and ``startB < endA``.
* **Atomic booking creation** – when creating a booking the code first
  rechecks availability inside a transaction.  If availability holds
  exactly one row is inserted with status ``PENDING_PAYMENT``.  The
  booking ID and computed price are returned.

If new business rules are introduced later (for example different
inventory or pricing) this module should be adapted accordingly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple

DB_PATH = Path(__file__).resolve().parent / "database.db"
PENDING_PAYMENT_EXPIRATION_MINUTES = 15


def init_db() -> None:
    """Initialise the SQLite database if it doesn't already exist.

    This function creates the ``bookings`` table with columns for all
    relevant booking attributes.  It also creates a small ``meta`` table
    used to store a schema version in case migrations are needed later.
    The function is idempotent – running it multiple times will leave
    existing data intact.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_reference TEXT UNIQUE,
            trailer_type  TEXT NOT NULL CHECK (trailer_type IN ('GALLER','KAP')),
            rental_type   TEXT NOT NULL CHECK (rental_type IN ('TWO_HOURS','FULL_DAY')),
            start_dt      TEXT NOT NULL,
            end_dt        TEXT NOT NULL,
            price         INTEGER NOT NULL,
            status        TEXT NOT NULL CHECK (status IN ('PENDING_PAYMENT','CONFIRMED','CANCELLED')),
            created_at    TEXT NOT NULL
            -- The following optional columns were added in Milestone C.
            -- swish_id stores the payment reference returned from the Swish
            -- Commerce API, and expires_at stores the expiry timestamp for
            -- pending bookings (ISO format, naive local time).
            , swish_id    TEXT
            , expires_at  TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trailer_blocks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trailer_type TEXT NOT NULL CHECK (trailer_type IN ('GALLER','KAP')),
            start_dt    TEXT NOT NULL,
            end_dt      TEXT NOT NULL,
            reason      TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        """
    )
    # Optional meta table for future schema versioning
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()

    # Add optional columns if they are missing.  SQLite does not support
    # ADD COLUMN IF NOT EXISTS, so we run ALTER TABLE inside a try/except
    # block and ignore the error if the column already exists.  This
    # allows seamless upgrades from the Milestone B schema.
    try:
        conn.execute("ALTER TABLE bookings ADD COLUMN swish_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE bookings ADD COLUMN expires_at TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE bookings ADD COLUMN booking_reference TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_booking_reference
        ON bookings (booking_reference)
        WHERE booking_reference IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trailer_blocks_type_start_end
        ON trailer_blocks (trailer_type, start_dt, end_dt)
        """
    )
    conn.commit()
    conn.close()

VALID_TRAILER_TYPES = {"GALLER", "KAP"}
VALID_RENTAL_TYPES = {"TWO_HOURS", "FULL_DAY"}


class SlotTakenError(ValueError):
    """Raised when a booking slot is already taken."""


class SlotBlockedError(ValueError):
    """Raised when a booking slot is blocked by admin."""

    def __init__(self, block: dict):
        super().__init__("slot blocked")
        self.block = block


def calculate_price(start_datetime: datetime, rental_type: str, trailer_type: str) -> int:
    """Calculate the price for a booking.

    Two‑hour rentals always cost 200 SEK.  Full day rentals cost 250 SEK
    on Monday through Thursday and 300 SEK on Friday through Sunday.  The
    determination is based on the weekday of ``start_datetime`` in the
    Europe/Stockholm timezone.  Python's standard datetime library uses
    ISO weekday numbering where Monday is 1 and Sunday is 7.

    Args:
        start_datetime: The start of the hire period.
        rental_type: ``"TWO_HOURS"`` or ``"FULL_DAY"``.
        trailer_type: ``"GALLER"`` or ``"KAP"``.

    Returns:
        The price in Swedish kronor as an integer.
    """
    rental_type = rental_type.upper()
    trailer_type = trailer_type.upper()
    if trailer_type not in VALID_TRAILER_TYPES:
        raise ValueError(f"Unknown trailer type: {trailer_type}")
    if rental_type == "TWO_HOURS":
        return 200
    if rental_type != "FULL_DAY":
        raise ValueError(f"Unknown rental type: {rental_type}")

    # Monday = 1, Sunday = 7
    weekday = start_datetime.isoweekday()
    if 1 <= weekday <= 4:
        return 250
    else:
        return 300


def _parse_iso(dt_str: str) -> datetime:
    """Parse an ISO formatted datetime string to a naive ``datetime``.

    SQLite stores datetimes as text; this helper centralises parsing.  The
    returned datetime is naive and in local time (no timezone handling).
    """
    return datetime.fromisoformat(dt_str)


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Return True if two time ranges overlap.

    Overlap is defined as ``startA < endB`` and ``startB < endA``.
    """
    return a_start < b_end and b_start < a_end


def _active_booking_where_clause() -> str:
    """SQL condition for bookings that should block availability."""
    return (
        "("
        "status = 'CONFIRMED' "
        "OR (status = 'PENDING_PAYMENT' AND (expires_at IS NULL OR expires_at >= ?))"
        ")"
    )


def find_block_overlap(
    trailer_type: str,
    start_datetime: datetime,
    end_datetime: datetime,
    connection: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Return the first overlapping admin block for the requested slot."""
    close_conn = False
    if connection is None:
        connection = sqlite3.connect(DB_PATH)
        close_conn = True
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT id, trailer_type, start_dt, end_dt, reason, created_at
            FROM trailer_blocks
            WHERE trailer_type = ?
              AND start_dt < ?
              AND ? < end_dt
            ORDER BY start_dt
            LIMIT 1
            """,
            (
                trailer_type.upper(),
                end_datetime.isoformat(timespec="minutes"),
                start_datetime.isoformat(timespec="minutes"),
            ),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if close_conn:
            connection.close()


def count_overlapping_active_bookings(
    trailer_type: str,
    start_datetime: datetime,
    end_datetime: datetime,
    connection: Optional[sqlite3.Connection] = None,
    now: Optional[datetime] = None,
) -> int:
    """Count bookings that block availability for the requested slot."""
    if now is None:
        now = datetime.now()
    close_conn = False
    if connection is None:
        connection = sqlite3.connect(DB_PATH)
        close_conn = True
    try:
        row = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM bookings
            WHERE trailer_type = ?
              AND {_active_booking_where_clause()}
              AND (start_dt < ? AND ? < end_dt)
            """,
            (
                trailer_type.upper(),
                now.isoformat(timespec="seconds"),
                end_datetime.isoformat(timespec="minutes"),
                start_datetime.isoformat(timespec="minutes"),
            ),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        if close_conn:
            connection.close()


def get_availability_conflict(
    trailer_type: str,
    start_datetime: datetime,
    end_datetime: datetime,
    connection: Optional[sqlite3.Connection] = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Return details about what blocks availability, if anything."""
    block = find_block_overlap(trailer_type, start_datetime, end_datetime, connection=connection)
    if block:
        return {"type": "BLOCK", "block": block}
    overlaps = count_overlapping_active_bookings(
        trailer_type, start_datetime, end_datetime, connection=connection, now=now
    )
    if overlaps > 0:
        return {"type": "BOOKING", "overlaps": overlaps}
    return None


def check_availability(
    trailer_type: str,
    start_datetime: datetime,
    end_datetime: datetime,
    connection: Optional[sqlite3.Connection] = None,
) -> bool:
    """Check whether a trailer of the given type is available for a time span.

    The system maintains exactly two units of each trailer type.  This
    function queries all existing bookings of the given type with status
    other than ``CANCELLED``, counts how many overlap with the requested
    period and returns ``True`` if no overlap exists.

    Args:
        trailer_type: ``"GALLER"`` or ``"KAP"``.
        start_datetime: Inclusive start of the requested hire.
        end_datetime: Exclusive end of the requested hire.
        connection: Optional existing SQLite connection.  If provided the
            caller is responsible for closing it.

    Returns:
        ``True`` if the booking can be accommodated, ``False`` otherwise.
    """
    conflict = get_availability_conflict(
        trailer_type,
        start_datetime,
        end_datetime,
        connection=connection,
    )
    return conflict is None


def create_booking(
    trailer_type: str,
    rental_type: str,
    start_datetime: datetime,
    end_datetime: datetime,
) -> Tuple[int, int]:
    """Attempt to create a new booking and return its ID and price.

    The function runs in a single database transaction.  It first
    recalculates the price and checks availability.  If
    no trailer is free the function raises ``ValueError``.  Otherwise it
    inserts a new booking with status ``PENDING_PAYMENT`` and returns
    ``(booking_id, price)``.

    Args:
        trailer_type: ``"GALLER"`` or ``"KAP"``.
        rental_type: ``"TWO_HOURS"`` or ``"FULL_DAY"``.
        start_datetime: Inclusive start of the hire.
        end_datetime: Exclusive end of the hire.
    Returns:
        A tuple ``(booking_id, price)`` for the newly created booking.

    Raises:
        SlotTakenError: If there is no availability for the requested slot.
    """
    trailer_type = trailer_type.upper()
    rental_type = rental_type.upper()
    price = calculate_price(start_datetime, rental_type, trailer_type)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.isolation_level = None  # Use manual transaction management
        # Begin a transaction.  IMMEDIATE obtains a reserved lock up front to
        # prevent other writers from starting until this transaction commits.
        conn.execute("BEGIN IMMEDIATE")
        # Recheck availability within the transaction.  Use the same
        # connection so the SELECT sees any uncommitted inserts (none yet).
        conflict = get_availability_conflict(
            trailer_type,
            start_datetime,
            end_datetime,
            connection=conn,
            now=datetime.now(),
        )
        if conflict:
            # Roll back and raise a clean exception if unavailable
            conn.execute("ROLLBACK")
            if conflict["type"] == "BLOCK":
                raise SlotBlockedError(conflict["block"])
            raise SlotTakenError("slot taken")
        # Insert booking
        # Compute expiry timestamp for new holds.  The
        # expiry is stored as ISO 8601 (naive) string.  It will be used
        # later to cancel expired reservations.
        created_at = datetime.now()
        expires_at = (
            created_at + timedelta(minutes=PENDING_PAYMENT_EXPIRATION_MINUTES)
        ).isoformat(timespec="seconds")

        cur = conn.execute(
            """
            INSERT INTO bookings (booking_reference, trailer_type, rental_type, start_dt, end_dt, price, status, created_at, swish_id, expires_at)
            VALUES (NULL, ?, ?, ?, ?, ?, 'PENDING_PAYMENT', ?, NULL, ?)
            """,
            (
                trailer_type,
                rental_type,
                start_datetime.isoformat(timespec="minutes"),
                end_datetime.isoformat(timespec="minutes"),
                price,
                created_at.isoformat(timespec="seconds"),
                expires_at,
            ),
        )
        booking_id = cur.lastrowid
        booking_reference = _generate_booking_reference(created_at, booking_id)
        conn.execute(
            """
            UPDATE bookings
            SET booking_reference = ?
            WHERE id = ?
            """,
            (booking_reference, booking_id),
        )
        conn.execute("COMMIT")
        return booking_id, price
    except Exception:
        # If an error occurs before commit/rollback we attempt to roll back.
        # Catch any OperationalError in case there is no active transaction.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


def create_block(
    trailer_type: str,
    start_datetime: datetime,
    end_datetime: datetime,
    reason: str,
) -> dict:
    """Create an admin block row and return it."""
    trailer_type_u = trailer_type.upper()
    if trailer_type_u not in VALID_TRAILER_TYPES:
        raise ValueError("Invalid trailerType")
    if end_datetime <= start_datetime:
        raise ValueError("endDatetime must be after startDatetime")
    created_at = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            INSERT INTO trailer_blocks (trailer_type, start_dt, end_dt, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                trailer_type_u,
                start_datetime.isoformat(timespec="minutes"),
                end_datetime.isoformat(timespec="minutes"),
                reason or "",
                created_at,
            ),
        )
        block_id = cur.lastrowid
        conn.commit()
        row = conn.execute(
            """
            SELECT id, trailer_type, start_dt, end_dt, reason, created_at
            FROM trailer_blocks
            WHERE id = ?
            """,
            (block_id,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_blocks(start_datetime: Optional[datetime] = None, end_datetime: Optional[datetime] = None) -> list[dict]:
    """List admin blocks, optionally filtering by overlap with a range."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        query = """
            SELECT id, trailer_type, start_dt, end_dt, reason, created_at
            FROM trailer_blocks
        """
        params: list[str] = []
        if start_datetime and end_datetime:
            query += " WHERE start_dt < ? AND ? < end_dt"
            params.extend(
                [
                    end_datetime.isoformat(timespec="minutes"),
                    start_datetime.isoformat(timespec="minutes"),
                ]
            )
        elif start_datetime:
            query += " WHERE end_dt > ?"
            params.append(start_datetime.isoformat(timespec="minutes"))
        elif end_datetime:
            query += " WHERE start_dt < ?"
            params.append(end_datetime.isoformat(timespec="minutes"))
        query += " ORDER BY start_dt, id"
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def delete_block(block_id: int) -> bool:
    """Delete an admin block by ID."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("DELETE FROM trailer_blocks WHERE id = ?", (block_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_confirmed(booking_id: int) -> None:
    """Mark a booking as confirmed (paid).

    This simply updates the status to ``CONFIRMED``.  It is safe to call on
    a booking that is already confirmed or cancelled; in such cases the
    state remains unchanged.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            UPDATE bookings
            SET status = 'CONFIRMED'
            WHERE id = ? AND status = 'PENDING_PAYMENT'
            """,
            (booking_id,),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_booking(booking_id: int) -> None:
    """Mark a booking as cancelled.

    This update is idempotent and safe to call multiple times.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            UPDATE bookings
            SET status = 'CANCELLED'
            WHERE id = ? AND status != 'CANCELLED'
            """,
            (booking_id,),
        )
        conn.commit()
    finally:
        conn.close()


def expire_outdated_bookings(now: Optional[datetime] = None) -> int:
    """Cancel all pending bookings whose expiry timestamp has passed.

    This helper checks for bookings in status ``PENDING_PAYMENT`` with an
    ``expires_at`` column that is not NULL and is strictly less than the
    current time.  Any such booking is marked as ``CANCELLED``.  The
    function is idempotent and can be called at the start of any API
    request to ensure stale holds do not block availability.

    Args:
        now: A naive datetime representing the current time.  If omitted
            ``datetime.now()`` is used.
    """
    if now is None:
        now = datetime.now()
    current_iso = now.isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            """
            UPDATE bookings
            SET status = 'CANCELLED'
            WHERE status = 'PENDING_PAYMENT'
              AND expires_at IS NOT NULL
              AND expires_at < ?
            """,
            (current_iso,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_booking_by_id(booking_id: int) -> Optional[dict]:
    """Return a single booking by its ID or ``None`` if not found.

    Args:
        booking_id: The integer ID of the booking.

    Returns:
        A dictionary of the booking columns or ``None``.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM bookings WHERE id = ?",
            (booking_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_swish_id(booking_id: int, swish_id: str) -> None:
    """Persist the Swish payment identifier on a booking.

    Args:
        booking_id: The booking to update.
        swish_id: The payment request identifier returned from the Swish API.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            UPDATE bookings
            SET swish_id = ?
            WHERE id = ?
            """,
            (swish_id, booking_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_bookings(status: Optional[str] = None) -> list[dict]:
    """Return a list of bookings as dictionaries.

    Args:
        status: Optionally filter by status (PENDING_PAYMENT, CONFIRMED, CANCELLED).

    Returns:
        A list of dictionaries representing bookings.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE status = ? ORDER BY start_dt",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bookings ORDER BY start_dt"
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _generate_booking_reference(created_at: datetime, booking_id: int) -> str:
    """Build a deterministic reference ID for customer-facing flows."""
    return f"DHS-{created_at.strftime('%Y%m%d')}-{booking_id:06d}"
