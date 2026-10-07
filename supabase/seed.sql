-- Local dev data only (applied by `supabase start` / `supabase db reset`).
insert into public.users (name) values
  ('Alex Rivera'),
  ('Sam Patel'),
  ('Jordan Lee');

insert into public.pushups (name, day, count) values
  ('Alex Rivera', current_date,      40),
  ('Alex Rivera', current_date - 1,  30),
  ('Sam Patel',   current_date,      25),
  ('Sam Patel',   current_date,      25),
  ('Jordan Lee',  current_date - 2,  60);
