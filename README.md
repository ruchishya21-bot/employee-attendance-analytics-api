# Employee Attendance & Analytics API

A REST API built with **Python, FastAPI, and MongoDB** for employee management, attendance tracking, attendance regularization, and workforce analytics.

The implementation uses MongoDB indexes and aggregation pipelines to support efficient querying and reporting.

## Features

* Employee creation and paginated employee listing.
* Attendance punch-in and punch-out with duplicate protection.
* Attendance filtering by employee, date range, and status.
* Attendance regularization with change history and concurrent-update protection.
* Monthly employee attendance analytics.
* Department-level attendance summaries, including employees without attendance records.
* Late-arrival leaderboard with competition ranking and cutoff ties.
* Department attendance trends with missing dates, daily headcount, and seven-day moving averages.
* Health check and query execution-plan inspection endpoints.

## Technology Stack

* Python 3.11+
* FastAPI
* PyMongo
* MongoDB 6.0+
* Pydantic

## Prerequisites

Install Python 3.11 or newer and MongoDB 6.0 or newer.

MongoDB must be accessible to the application. The default connection is:

`mongodb://127.0.0.1:27017`

## Setup

Run these commands from the project root.

### 1. Create and activate a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 3. Configure MongoDB

The application supports the following environment variables:

* `MONGO_URI`: MongoDB connection URI.
* `MONGO_DB`: Database name; defaults to `attendance_db`.

For a local MongoDB instance, the defaults work without a `.env` file.

If you use environment variables in a local `.env` file, keep that file out of version control.

### 4. Start the API

```powershell
python -m uvicorn app.main:app --port 8000
```

The API will be available at:

`http://127.0.0.1:8000`

Interactive API documentation:

`http://127.0.0.1:8000/docs`

## API Overview

| Method | Endpoint                                    | Purpose                                                          |
| ------ | ------------------------------------------- | ---------------------------------------------------------------- |
| GET    | `/health`                                   | Check application and database availability                      |
| POST   | `/employees`                                | Create an employee                                               |
| GET    | `/employees`                                | List employees with optional department filtering and pagination |
| POST   | `/attendance/punch-in`                      | Record an attendance punch-in                                    |
| POST   | `/attendance/punch-out`                     | Record an attendance punch-out                                   |
| GET    | `/attendance`                               | Query attendance records with filters and pagination             |
| PATCH  | `/attendance/{emp_code}/{date}`             | Correct an attendance record                                     |
| GET    | `/analytics/employees/{emp_code}/monthly`   | Retrieve monthly employee attendance analytics                   |
| GET    | `/analytics/departments/summary`            | Retrieve department-level monthly statistics                     |
| GET    | `/analytics/leaderboard/late`               | Retrieve the monthly late-arrival leaderboard                    |
| GET    | `/analytics/departments/{department}/trend` | Retrieve daily department attendance trends                      |
| GET    | `/admin/explain/{endpoint}`                 | Inspect query execution statistics                               |

Refer to `/docs` for request schemas, query parameters, and response formats.

## Implementation Notes

* API timestamps use epoch milliseconds.
* Attendance dates use the employee's local attendance date in Indian Standard Time (IST).
* MongoDB stores timestamps as UTC BSON datetimes.
* Unique indexes protect employee codes and employee/date attendance records.
* Attendance regularization records changes in an audit history.
* Analytics use MongoDB aggregation pipelines for attendance calculations.
* Attendance trend generation uses database-side date densification.
* Derived attendance values are calculated by the application rather than accepted from clients.

## Submission Contents

The repository includes:

* `app/main.py`
* `requirements.txt`
* `README.md`
* `REVIEW.md`
* `DECISIONS.md`

No environment files, credentials, or secrets should be committed.
