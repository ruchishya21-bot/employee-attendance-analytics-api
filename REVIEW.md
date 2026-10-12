# REVIEW.md

## Defect Review

| # | Where (function / area)           | What is wrong                                                                                                                                | How to notice it                                                                                       | How it was fixed                                                                                                                                                              |
| - | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 | Department trend aggregation      | MongoDB `$round` uses ties-to-even rounding, which can disagree with the required half-up rounding for reported rates.                       | Test a calculated rate or moving average whose value falls exactly on a rounding boundary.             | Use a half-up rounding expression for non-negative values and retain four-decimal precision for reported trend metrics.                                                       |
| 2 | Late leaderboard                  | Sorting and ranking do not, by themselves, guarantee that all employees tied at the cutoff are returned in the required employee-code order. | Request a limited leaderboard where multiple employees have the same total late minutes at the cutoff. | Retrieve cutoff ties, avoid duplicate employees, assign the shared competition rank, and sort the final results by total late minutes descending and employee code ascending. |
| 3 | Late leaderboard explain endpoint | A hardcoded limit can make the explained query differ from the limit requested for the leaderboard.                                          | Request execution statistics with a limit other than the default and compare the explained pipeline.   | Accept a configurable limit and apply it to the explained pipeline.                                                                                                           |

## Additional Checks

* **Punch-in concurrency:** A unique compound index on `emp_code` and `date` prevents duplicate attendance records when concurrent requests target the same employee and attendance date.
* **Punch-out concurrency:** The conditional update requires `punch_out` to remain `null`, allowing only one concurrent request to close the record successfully.
* **Regularization concurrency:** Compare-and-swap conditions prevent an update from silently overwriting a concurrently modified attendance record.
* **Employees without attendance logs:** Department headcount is calculated from employee records rather than inferred solely from attendance logs.
* **Attendance trend gaps:** Database-side date densification includes dates without attendance records, including weekends.
* **Timestamp validation:** Strict integer validation and range checks reject values that are not valid epoch-millisecond timestamps.
* **Derived fields:** Work hours, late minutes, overtime, and half-day status are recalculated by the application rather than trusted from client input.

## Potential Improvements

The explain endpoint should be kept synchronized with every significant change to its corresponding production query. In particular, multi-query endpoints should account for all material queries when explaining their execution plans.

The aggregation pipelines and indexes should also be evaluated against the full grading dataset, rather than relying only on a small local dataset.
