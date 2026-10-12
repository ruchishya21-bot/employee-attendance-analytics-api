"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000

Environment:
  MONGO_URI (default: mongodb://127.0.0.1:27017)
  MONGO_DB  (default: attendance_db)

MongoDB stores instants as UTC BSON datetimes. API instants are epoch milliseconds.
"""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, ConfigDict, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId

# Real environment variables take precedence over values in a local .env file.
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=False)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
NON_PRESENT_STATUSES = {"ABSENT", "LEAVE"}
ALL_STATUSES = PRESENT_STATUSES | NON_PRESENT_STATUSES
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------- Request models ----------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def valid_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must match EMP followed by 4 to 6 digits")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("shift times must use HH:MM in 24-hour format")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if len(value) != 10:
            raise ValueError("joined_on must use YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        for field in ("name", "email", "department"):
            if not getattr(self, field).strip():
                raise ValueError(f"{field} cannot be empty")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class AttendanceCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    by: str = "admin"
    reason: str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_epoch(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punch times must be epoch milliseconds")
        return value

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value):
        if not value.strip():
            raise ValueError("reason cannot be empty")
        return value


# ---------------------------- Common helpers ----------------------------

def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)


def to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(UTC).replace(tzinfo=None, microsecond=0)


def epoch_ms_to_utc(value: int) -> datetime:
    # Reject values that look like epoch seconds, strings, floats, and booleans.
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(status_code=422, detail="timestamp must be epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0, tzinfo=None)


def utc_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(aware.timestamp()) * 1000


def as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="date must use YYYY-MM-DD")
    if len(value) != 10:
        raise HTTPException(status_code=422, detail="date must use YYYY-MM-DD")
    return parsed


def parse_shift(value: str) -> tuple[int, int]:
    h, m = map(int, value.split(":"))
    return h, m


def shift_bounds(punch_in: datetime, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    local_in = as_ist(punch_in)
    start_h, start_m = parse_shift(shift_start)
    end_h, end_m = parse_shift(shift_end)
    start = local_in.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = local_in.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    # A punch-in before the end time of an overnight shift belongs to the previous shift.
    if end.date() > start.date() and local_in.time().replace(tzinfo=None) < time(end_h, end_m):
        start -= timedelta(days=1)
        end -= timedelta(days=1)
    return start.astimezone(UTC).replace(tzinfo=None), end.astimezone(UTC).replace(tzinfo=None)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: Optional[str] = None) -> int:
    if shift_end is None:
        local = as_ist(punch_in)
        h, m = parse_shift(shift_start)
        start_local = local.replace(hour=h, minute=m, second=0, microsecond=0)
    else:
        start_utc, _ = shift_bounds(punch_in, shift_start, shift_end)
        start_local = start_utc.replace(tzinfo=UTC).astimezone(IST)
    seconds = int((as_ist(punch_in) - start_local).total_seconds())
    # Strictly more than 10 minutes late; store floor minutes since shift start.
    return max(0, seconds // 60) if seconds > 600 else 0


def decimal_round(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def mongo_round_half_up(expression: Any, places: int = 4) -> dict:
    """Mongo aggregation expression for half-up rounding of non-negative values.

    MongoDB's $round uses ties-to-even. All rates and moving averages used here
    are non-negative, so floor(value * scale + 0.5) / scale implements half-up.
    """
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}}, scale]}


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return decimal_round(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, punch_in: datetime, shift_start: str) -> int:
    _, end_utc = shift_bounds(punch_in, shift_start, shift_end)
    seconds = (punch_out - end_utc).total_seconds()
    minutes = int(seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_derived(emp: dict, status: str, punch_in: Optional[datetime],
                    punch_out: Optional[datetime]) -> dict:
    if status in NON_PRESENT_STATUSES:
        return {
            "punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
        }
    if punch_in is None:
        raise HTTPException(status_code=422, detail="presence statuses require punch_in")
    late = compute_late_minutes(punch_in, emp["shift_start"], emp["shift_end"])
    work_hours = None
    overtime = 0
    half_day = False
    if punch_out is not None:
        if punch_out <= punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if punch_out - punch_in > timedelta(hours=24):
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
        work_hours = compute_work_hours(punch_in, punch_out)
        overtime = compute_overtime(punch_out, emp["shift_end"], punch_in, emp["shift_start"])
        half_day = work_hours < 4.50
    return {
        "punch_in": punch_in, "punch_out": punch_out, "work_hours": work_hours,
        "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day,
    }


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = as_ist(punch_in)
    start_h, start_m = parse_shift(shift_start)
    end_h, end_m = parse_shift(shift_end)
    # For an overnight shift, early-morning punches belong to the prior shift date.
    if (end_h, end_m) <= (start_h, start_m) and local.time().replace(tzinfo=None) < time(end_h, end_m):
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def public_employee(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = utc_to_epoch_ms(doc["created_at"])
    return doc


def public_attendance(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    # All API instants, including audit instants, are epoch milliseconds.
    for key in ("punch_in", "punch_out"):
        if isinstance(doc.get(key), datetime):
            doc[key] = utc_to_epoch_ms(doc[key])
    history = []
    for item in doc.get("history", []) or []:
        entry = dict(item)
        if isinstance(entry.get("at"), datetime):
            entry["at"] = utc_to_epoch_ms(entry["at"])
        changes = {}
        for key, change in (entry.get("changes") or {}).items():
            change = dict(change)
            for side in ("before", "after"):
                if isinstance(change.get(side), datetime):
                    change[side] = utc_to_epoch_ms(change[side])
            changes[key] = change
        if "changes" in entry:
            entry["changes"] = changes
        history.append(entry)
    if "history" in doc:
        doc["history"] = history
    return doc


def validate_page(page: int, page_size: int) -> None:
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")


def month_range(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")
    try:
        first = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")
    if first.month == 12:
        next_first = date(first.year + 1, 1, 1)
    else:
        next_first = date(first.year, first.month + 1, 1)
    return first, next_first - timedelta(days=1)


def weekdays_inclusive(start: date, end: date) -> int:
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def date_utc_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def record_to_api(doc: dict) -> dict:
    out = public_attendance(doc)
    return out


def db_ping() -> None:
    db.command("ping")


# ---------------------------- Indexes / startup ----------------------------

@app.on_event("startup")
def create_indexes() -> None:
    # Idempotent indexes: unique constraints provide race-safe writes.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                    unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)],
                                    name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING), ("status", ASCENDING)],
                                    name="ix_attendance_employee_date_status")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)],
                                    name="ix_attendance_status_date")
    db.attendance_logs.create_index([("late_minutes", DESCENDING), ("date", ASCENDING), ("emp_code", ASCENDING)],
                                    name="ix_attendance_late_date_employee")


# ---------------------------- Basic endpoints ----------------------------

@app.get("/health")
def health():
    try:
        db_ping()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return public_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = [public_employee(doc) for doc in db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else now_utc()
    local_day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": local_day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return record_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else now_utc()
    # The most recent eligible record determines whether this is a first or duplicate punch-out.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(status_code=404, detail="no matching punch-in")
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    if ts <= rec["punch_in"]:
        raise HTTPException(status_code=422, detail="punch-out must be after punch-in")
    if ts - rec["punch_in"] > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch-out must be within 24 hours of punch-in")
    derived = compute_derived(emp, rec["status"], rec["punch_in"], ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {k: derived[k] for k in ("punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return record_to_api(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(status_code=422, detail="invalid status")
        q["status"] = status
    if date_from is not None or date_to is not None:
        date_filter = {}
        if date_from is not None:
            iso_date(date_from)
            date_filter["$gte"] = date_from
        if date_to is not None:
            iso_date(date_to)
            date_filter["$lte"] = date_to
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        q["date"] = date_filter
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    items = [record_to_api(doc) for doc in db.attendance_logs.find(q).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    ).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: AttendanceCorrection,
):
    iso_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(status_code=404, detail="attendance record not found")

    supplied = body.model_fields_set - {"by", "reason"}
    if not supplied:
        raise HTTPException(status_code=422, detail="no fields to correct")
    status = body.status if "status" in supplied else old.get("status", "PRESENT")
    pin = epoch_ms_to_utc(body.punch_in) if "punch_in" in supplied and body.punch_in is not None else (
        None if "punch_in" in supplied else old.get("punch_in")
    )
    pout = epoch_ms_to_utc(body.punch_out) if "punch_out" in supplied and body.punch_out is not None else (
        None if "punch_out" in supplied else old.get("punch_out")
    )
    if status in NON_PRESENT_STATUSES:
        if ("punch_in" in supplied and body.punch_in is not None) or ("punch_out" in supplied and body.punch_out is not None):
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot have punch times")
        pin, pout = None, None
    else:
        if pin is None:
            raise HTTPException(status_code=422, detail="presence statuses require punch_in")
        if attendance_date_for(pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(status_code=422, detail="punch_in must fall on the attendance date")
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    derived = compute_derived(emp, status, pin, pout)
    proposed = {"status": status, **derived}
    changed = {}
    for key, after in proposed.items():
        before = old.get(key)
        if key == "half_day":
            before = old.get(key, False)
        if before != after:
            changed[key] = {"before": before, "after": after}
    if not changed:
        raise HTTPException(status_code=422, detail="correction makes no changes")

    history_entry = {
        "at": now_utc(),
        "by": body.by.strip() or "admin",
        "reason": body.reason,
        "changes": changed,
    }
    # Compare-and-swap protects against lost updates, including legacy records
    # that do not yet contain history or half_day fields.
    query = {"_id": old["_id"]}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"):
        if key in old:
            query[key] = old[key]
        else:
            query[key] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        query,
        {"$set": proposed, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently; retry")
    return record_to_api(updated)


# ---------------------------- Analytics pipelines ----------------------------

def present_match(start: date, end: date, emp_code: Optional[str] = None) -> dict:
    q: dict[str, Any] = {
        "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        "status": {"$in": list(PRESENT_STATUSES)},
    }
    if emp_code:
        q["emp_code"] = emp_code
    return q


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., description="YYYY-MM")):
    first, last = month_range(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    joined = iso_date(emp["joined_on"])
    work_start = max(first, joined)
    working_days = weekdays_inclusive(work_start, last) if work_start <= last else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    agg = rows[0] if rows else {}
    present_days = float(agg.get("present_days", 0))
    pct = decimal_round(present_days / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": decimal_round(present_days, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., description="YYYY-MM")):
    first, last = month_range(month)
    # Attendance facts are aggregated in MongoDB; headcount is joined against all
    # employees so employees with zero attendance logs are included.
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]},
                "$work_hours", 0
            ]}},
            "hours_count": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]},
                1, 0
            ]}},
        }},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.joined_on": {"$lte": last.isoformat()}}},
        {"$group": {
            "_id": "$employee.department",
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "hours_sum": {"$sum": "$hours_sum"},
            "hours_count": {"$sum": "$hours_count"},
        }},
    ]
    facts = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}
    # Include departments with headcount but no logs by obtaining distinct department
    # values from employees; all numerical attendance metrics remain DB-aggregated.
    departments = list(db.employees.aggregate([
        {"$match": {"joined_on": {"$lte": last.isoformat()}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]))
    output = []
    for dept in departments:
        f = facts.get(dept["_id"], {})
        hours_count = f.get("hours_count", 0)
        output.append({
            "department": dept["_id"],
            "headcount": dept["headcount"],
            "present_days": decimal_round(float(f.get("present_days", 0)), 2),
            "avg_work_hours": decimal_round(f["hours_sum"] / hours_count, 2) if hours_count else None,
            "late_count": int(f.get("late_count", 0)),
            "total_late_minutes": int(f.get("late_minutes", 0)),
            "leave_count": int(f.get("leave_count", 0)),
            "on_duty_count": int(f.get("on_duty_count", 0)),
        })
    return {"month": month, "items": output}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., description="YYYY-MM"),
    limit: int = Query(10, ge=1, le=100),
    department: Optional[str] = None,
):
    first, last = month_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}}
        }},
        {"$limit": limit},
        {"$project": {
            "_id": 0, "emp_code": "$_id", "name": "$employee.name",
            "department": "$employee.department", "total_late_minutes": 1, "rank": 1
        }},
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    # $setWindowFields does not guarantee the final output order. Restore the
    # required total-late-descending, emp_code-ascending order before finding
    # the limit boundary and again after adding any tied employees.
    items.sort(key=lambda row: (-row["total_late_minutes"], row["emp_code"]))
    # Include ties at the limit boundary; rank is competition rank (1,2,2,4).
    if items and len(items) >= limit:
        boundary = items[limit - 1]["total_late_minutes"]
        boundary_rank = items[limit - 1]["rank"]
        extra = list(db.attendance_logs.aggregate([
            {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$match": {"total_late_minutes": boundary}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            *([{"$match": {"employee.department": department}}] if department is not None else []),
            {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1}},
            {"$sort": {"emp_code": 1}},
        ]))
        seen = {x["emp_code"] for x in items}
        for row in extra:
            if row["emp_code"] not in seen:
                row["rank"] = boundary_rank
                items.append(row)
        items.sort(key=lambda row: (-row["total_late_minutes"], row["emp_code"]))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(...),
    date_to: str = Query(...),
):
    start = iso_date(date_from)
    end = iso_date(date_to)
    if end < start:
        raise HTTPException(status_code=422, detail="date_to must be on or after date_from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="department not found")

    # Start with a database-side seed and use $densify so dates with no attendance
    # records (including weekends and empty range edges) are still returned.
    start_s, end_s = start.isoformat(), end.isoformat()
    pipeline = [
        {"$documents": [{"day": datetime.combine(start, time.min)}]},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [
            datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)
        ]}}},
        {"$set": {
            "day_string": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "weekday": {"$dayOfWeek": "$day"},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$day_string"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
            "as": "facts",
        }},
        {"$set": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$facts.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$facts.late_count", 0]}, 0]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$day_string"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "headcount"},
            ],
            "as": "head",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$head.headcount", 0]}, 0]},
            "is_working_day": {"$and": [{"$gt": ["$weekday", 1]}, {"$lt": ["$weekday", 7]}]},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                mongo_round_half_up({"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 4),
                None,
            ]}
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": "$day_string", "is_working_day": 1,
            "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": mongo_round_half_up("$moving_avg_7d", 4),
        }},
    ]
    # $documents is a database-level aggregation stage (MongoDB 6.0+).
    explain_result = db.command({
        "aggregate": 1,
        "pipeline": pipeline,
        "cursor": {},
        "allowDiskUse": True,
    })
    rows = explain_result.get("cursor", {}).get("firstBatch", [])
    return {"department": department, "from": start_s, "to": end_s, "items": rows}


# ---------------------------- Explain endpoint ----------------------------
# ---------------------------- Explain endpoint ----------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    month: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=100),
):
    """Return executionStats for the same core query/pipeline used by an endpoint."""
    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            if status not in ALL_STATUSES:
                raise HTTPException(status_code=422, detail="invalid status")
            q["status"] = status
        date_q = {}
        if date_from:
            iso_date(date_from)
            date_q["$gte"] = date_from
        if date_to:
            iso_date(date_to)
            date_q["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        if date_q:
            q["date"] = date_q
        return db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })

    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required")
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        first, last = month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [
                    {"$and": [
                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                        {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                        {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0
                ]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            }},
        ]
        return db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        first, last = month_range(month)
        # This is the complete attendance-facts pipeline used by department_summary,
        # including employee lookup and the department-level group.
        pipeline = [
            {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
            {"$group": {
                "_id": "$emp_code",
                "present_days": {"$sum": {"$cond": [
                    {"$and": [
                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                        {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                        {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0
                ]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                "hours_sum": {"$sum": {"$cond": [
                    {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0
                ]}},
                "hours_count": {"$sum": {"$cond": [
                    {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, 1, 0
                ]}},
            }},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.joined_on": {"$lte": last.isoformat()}}},
            {"$group": {
                "_id": "$employee.department",
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "hours_sum": {"$sum": "$hours_sum"},
                "hours_count": {"$sum": "$hours_count"},
            }},
        ]
        return db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        first, last = month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
        ]
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline += [
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$limit": limit},
            {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "rank": 1}},
        ]
        return db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})

    # Explain the same date-seeded, gap-filled pipeline used by department_trend.
    if not department or not date_from or not date_to:
        raise HTTPException(status_code=422, detail="department, date_from, and date_to are required")
    start, end = iso_date(date_from), iso_date(date_to)
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="invalid date range")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="department not found")
    pipeline = [
        {"$documents": [{"day": datetime.combine(start, time.min)}]},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)]}}},
        {"$set": {"day_string": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}, "weekday": {"$dayOfWeek": "$day"}}},
        {"$lookup": {
            "from": "attendance_logs", "let": {"day": "$day_string"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
                {"$group": {"_id": None,
                    "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ], "as": "facts",
        }},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$facts.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$facts.late_count", 0]}, 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$day_string"}, "pipeline": [
            {"$match": {"department": department}}, {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "headcount"},
        ], "as": "head"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$head.headcount", 0]}, 0]}, "is_working_day": {"$and": [{"$gt": ["$weekday", 1]}, {"$lt": ["$weekday", 7]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, mongo_round_half_up({"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": "$day_string", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": mongo_round_half_up("$moving_avg_7d", 4)}},
    ]
    return db.command({"explain": {"aggregate": 1, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})
