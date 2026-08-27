-- Reservations schema for the Booking agent's check_availability/book_table tools.
-- Capacity is a single fixed cap per date+time slot (MAX_COVERS_PER_SLOT in
-- db.py) -- no per-table layout modeling, intentionally simple for a demo.

create table if not exists reservations (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  date date not null,
  time time not null,
  party_size int not null check (party_size > 0),
  created_at timestamptz not null default now()
);

create index if not exists reservations_date_time_idx on reservations (date, time);

-- RLS is on by default for new tables on this project; the service_role key
-- (used by db.py) bypasses RLS but still needs explicit table grants.
alter table reservations enable row level security;
grant usage on schema public to service_role;
grant select, insert on public.reservations to service_role;

