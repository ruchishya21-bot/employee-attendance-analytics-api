# DECISIONS.md

## 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employee codes, plus a compound department/employee-code index for sorted employee listings. An employee join-date index supports headcount calculations. Attendance has a unique `(emp_code, date)` index to enforce one record per employee per day, a date/employee-code index for attendance queries, an employee/date/status index for employee-specific filtering, a status/date index, and a late-minutes/date/employee-code index. I would evaluate a department/join-date index if trend queries become a bottleneck.

## 2. Punch-in Race

Two identical punch-in requests can both check that the employee exists. However, both attempt to insert the same employee/date combination. MongoDB's unique compound index allows only one insert. The other raises `DuplicateKeyError`, which the API converts to HTTP 409.

## 3. Ties

The leaderboard uses competition ranking: 1, 2, 2, 4. If employees tie at the requested limit, all employees sharing the cutoff total are returned. Results are ordered by late minutes descending and employee code ascending.

## 4. Headcount

Department headcount is aggregated from employee records whose joining date is on or before the month's end. It does not depend on attendance logs, so employees with no logs are still counted.

## 5. Scaling

For 100 times more data, I would benchmark query plans and index selectivity using production-like data, then optimize expensive aggregation stages and introduce caching only for reports that tolerate it.
