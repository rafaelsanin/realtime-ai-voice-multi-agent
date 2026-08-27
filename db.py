"""Booking business logic (commit 3).

Commit 6: depends on the `ReservationsRepository` abstraction (Dependency
Inversion) instead of a raw Supabase client, and returns typed pydantic
results instead of raw dicts.
"""

from __future__ import annotations

from loguru import logger
from pydantic import BaseModel

from reservations import ReservationsRepository

# Single fixed capacity per date+time slot -- no per-table layout modeling,
# intentionally simple for a demo.
MAX_COVERS_PER_SLOT = 20


class AvailabilityResult(BaseModel):
    """Result of a check_availability tool call."""

    available: bool
    reason: str


class BookingResult(BaseModel):
    """Result of a book_table tool call."""

    booked: bool
    reservation_id: int | None = None
    reason: str | None = None


async def check_availability(
    repository: ReservationsRepository, *, date: str, time: str, party_size: int
) -> AvailabilityResult:
    """Check whether a table is available for a given date, time, and party size.

    Args:
        repository: Reservations backend to check against.
        date: Reservation date, formatted as YYYY-MM-DD.
        time: Reservation time, formatted as HH:MM (24-hour).
        party_size: Number of guests.
    """
    booked = await repository.booked_covers(date, time)
    available = booked + party_size <= MAX_COVERS_PER_SLOT
    reason = (
        "Table available."
        if available
        else f"Fully booked for that slot ({booked}/{MAX_COVERS_PER_SLOT} covers already reserved)."
    )
    logger.bind(
        event="availability_checked",
        date=date,
        time=time,
        party_size=party_size,
        available=available,
    ).info("availability checked")
    return AvailabilityResult(available=available, reason=reason)


async def book_table(
    repository: ReservationsRepository, *, name: str, date: str, time: str, party_size: int
) -> BookingResult:
    """Book a table for a caller, if the slot still has capacity.

    Args:
        repository: Reservations backend to book against.
        name: Name to book the reservation under.
        date: Reservation date, formatted as YYYY-MM-DD.
        time: Reservation time, formatted as HH:MM (24-hour).
        party_size: Number of guests.
    """
    booked = await repository.booked_covers(date, time)
    if booked + party_size > MAX_COVERS_PER_SLOT:
        logger.bind(event="booking_rejected", date=date, time=time, party_size=party_size).info(
            "booking rejected: slot full"
        )
        return BookingResult(
            booked=False,
            reason=f"Fully booked for that slot ({booked}/{MAX_COVERS_PER_SLOT} covers already reserved).",
        )

    reservation = await repository.create(name=name, date=date, time=time, party_size=party_size)
    logger.bind(
        event="booking_created",
        reservation_id=reservation.id,
        date=date,
        time=time,
        party_size=party_size,
    ).info("booking created")
    return BookingResult(booked=True, reservation_id=reservation.id)
