# Design decisions

1. **Indexes.** A unique `employees.emp_code` index enforces employee identity. The unique `attendance_logs(emp_code, date)` index enforces one record per day and makes the punch-in race safe. `attendance_logs(date, emp_code)` supports date scans and list ordering; `status, date, emp_code` supports status filters. `employees(department, joined_on)` and `employees(joined_on)` support headcount queries. I did not make email unique: the contract only guarantees `emp_code`, and nothing looks up by email.

2. **Punch-in race.** Both requests may calculate the same attendance date. MongoDB's unique compound index arbitrates the inserts: one succeeds and returns 201; the other raises `DuplicateKeyError`, which becomes 409. Nothing depends on check-then-insert.

3. **Ties.** The leaderboard assigns `$rank` by total late minutes before applying `rank <= limit`. Everyone tied at the cutoff rank is returned, and the next rank is skipped.

4. **Headcount.** The department summary starts from eligible employee documents, then looks up each employee's monthly logs. Employees with no logs stay in the grouped set, so each still adds one.

5. **At 100x scale.** I would measure query cost first, then consider materialized daily department summaries. That trades write complexity for cheaper reads and needs a freshness policy.
