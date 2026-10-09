-- Additive maintenance migration for an existing runtime version 2 database.
-- No rows, grants, RLS policies, existing public tables or functions are changed.
begin;
create index if not exists runtime_allocation_receipt on toolbako_runtime.finance_allocations(receipt_id);
create index if not exists runtime_entry_receipt on toolbako_runtime.finance_entries(receipt_id);
create index if not exists runtime_entry_payout on toolbako_runtime.finance_entries(payout_id);
commit;
