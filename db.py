"""Supabase-backed tools for the Booking agent (commit 3).

Tools read the Supabase `AsyncClient` off `params.app_resources`, wired up in
bot.py via `PipelineWorker(..., app_resources=supabase_client)` so every tool
call in the session shares one client/connection instead of opening a new one
per call.
"""

from pipecat.services.llm_service import FunctionCallParams

# Single fixed capacity per date+time slot -- no per-table layout modeling,
# intentionally simple for a demo.
MAX_COVERS_PER_SLOT = 20


async def _booked_covers(params: FunctionCallParams, date: str, time: str) -> int:
    response = (
        await params.app_resources.table("reservations")
        .select("party_size")
        .eq("date", date)
        .eq("time", time)
        .execute()
    )
    return sum(row["party_size"] for row in response.data)


async def check_availability(
    params: FunctionCallParams, date: str, time: str, party_size: int
) -> None:
    """Check whether a table is available for a given date, time, and party size.

    Args:
        date: Reservation date, formatted as YYYY-MM-DD.
        time: Reservation time, formatted as HH:MM (24-hour).
        party_size: Number of guests.
    """
    booked = await _booked_covers(params, date, time)
    available = booked + party_size <= MAX_COVERS_PER_SLOT
    reason = (
        "Table available."
        if available
        else f"Fully booked for that slot ({booked}/{MAX_COVERS_PER_SLOT} covers already reserved)."
    )
    await params.result_callback({"available": available, "reason": reason})


async def book_table(
    params: FunctionCallParams, name: str, date: str, time: str, party_size: int
) -> None:
    """Book a table for a caller, if the slot still has capacity.

    Args:
        name: Name to book the reservation under.
        date: Reservation date, formatted as YYYY-MM-DD.
        time: Reservation time, formatted as HH:MM (24-hour).
        party_size: Number of guests.
    """
    booked = await _booked_covers(params, date, time)
    if booked + party_size > MAX_COVERS_PER_SLOT:
        await params.result_callback(
            {
                "booked": False,
                "reason": f"Fully booked for that slot ({booked}/{MAX_COVERS_PER_SLOT} covers already reserved).",
            }
        )
        return

    response = (
        await params.app_resources.table("reservations")
        .insert({"name": name, "date": date, "time": time, "party_size": party_size})
        .execute()
    )
    await params.result_callback({"booked": True, "reservation": response.data[0]})
