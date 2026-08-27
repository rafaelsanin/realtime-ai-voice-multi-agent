"""Reservations persistence (commit 6): a typed repository over Supabase.

`ReservationsRepository` is the abstraction `db.py`'s booking logic depends on
(Dependency Inversion / Interface Segregation) -- `SupabaseReservationsRepository`
is the only concrete implementation for now, but a fake/in-memory one (for
tests) could implement the same Protocol without touching `db.py` or
`workers.py`.
"""

from __future__ import annotations

from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import BaseModel
from supabase import AsyncClient


class Reservation(BaseModel):
    """A single booked table."""

    id: UUID
    name: str
    date: str
    time: str
    party_size: int


class ReservationsRepository(Protocol):
    """Everything the booking tools need from a reservations backend."""

    async def booked_covers(self, date: str, time: str) -> int:
        """Total covers already booked for a given date/time slot."""
        ...

    async def create(self, *, name: str, date: str, time: str, party_size: int) -> Reservation:
        """Insert a new reservation and return it."""
        ...


class SupabaseReservationsRepository:
    """`ReservationsRepository` backed by the Supabase `reservations` table."""

    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def booked_covers(self, date: str, time: str) -> int:
        response = (
            await self._client.table("reservations")
            .select("party_size")
            .eq("date", date)
            .eq("time", time)
            .execute()
        )
        rows = cast(list[dict[str, Any]], response.data)
        return sum(row["party_size"] for row in rows)

    async def create(self, *, name: str, date: str, time: str, party_size: int) -> Reservation:
        response = (
            await self._client.table("reservations")
            .insert({"name": name, "date": date, "time": time, "party_size": party_size})
            .execute()
        )
        rows = cast(list[dict[str, Any]], response.data)
        return Reservation.model_validate(rows[0])
