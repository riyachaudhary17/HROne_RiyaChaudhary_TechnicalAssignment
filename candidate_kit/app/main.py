"""Employee Attendance & Analytics API."""

from __future__ import annotations

import calendar
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

load_dotenv()
logger = logging.getLogger(__name__)

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
attendance_logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
EpochMillis = Annotated[
    StrictInt,
    Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS),
]
NullableEpochMillis = EpochMillis | None


def create_indexes() -> None:
    employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="employees_emp_code_unique",
    )
    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="employees_department_joined_on",
    )
    employees.create_index([("joined_on", ASCENDING)], name="employees_joined_on")
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_emp_code_date_unique",
    )
    attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_emp_code",
    )
    attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_emp_code",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        create_indexes()
    except ServerSelectionTimeoutError:
        logger.warning("MongoDB is unavailable during startup; indexes were not created")
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


def _parse_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD")
    return parsed


def _date_range(value: str, field_name: str = "date") -> date:
    try:
        return _parse_date(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name}: {exc}") from exc


def _month_bounds(month: str) -> tuple[str, str, date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")
    year, month_number = map(int, month.split("-"))
    try:
        start = date(year, month_number, 1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be a valid calendar month") from exc
    last_day = calendar.monthrange(year, month_number)[1]
    end = date(year, month_number, last_day)
    return start.isoformat(), end.isoformat(), start, end


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def _to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp()) * 1000


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _round_number(value: int | float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def _is_overnight(employee: dict[str, Any]) -> bool:
    return employee["shift_end"] <= employee["shift_start"]


def _attendance_date(punch_in: datetime, employee: dict[str, Any]) -> str:
    local = _as_utc(punch_in).astimezone(IST)
    day = local.date()
    if _is_overnight(employee) and local.time().replace(tzinfo=None) < _clock(employee["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, employee: dict[str, Any], attendance_date: str) -> int:
    shift_start = datetime.combine(_parse_date(attendance_date), _clock(employee["shift_start"]), tzinfo=IST)
    elapsed_seconds = int((_as_utc(punch_in) - shift_start.astimezone(UTC)).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 10 * 60 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    elapsed_seconds = int((_as_utc(punch_out) - _as_utc(punch_in)).total_seconds())
    hours = (Decimal(elapsed_seconds) / Decimal(3600)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(hours)


def compute_overtime(punch_out: datetime, employee: dict[str, Any], attendance_date: str) -> int:
    shift_day = _parse_date(attendance_date)
    overnight = _is_overnight(employee)
    if overnight:
        shift_day += timedelta(days=1)
    shift_end = datetime.combine(shift_day, _clock(employee["shift_end"]), tzinfo=IST)
    elapsed_seconds = int((_as_utc(punch_out) - shift_end.astimezone(UTC)).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds >= 30 * 60 else 0


def _validate_punch_pair(punch_in: datetime | None, punch_out: datetime | None) -> None:
    if punch_out is None:
        return
    if punch_in is None:
        raise HTTPException(status_code=422, detail="punch_out requires punch_in")
    elapsed = _as_utc(punch_out) - _as_utc(punch_in)
    if elapsed.total_seconds() <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if elapsed > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")


def _employee_response(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": _to_epoch_ms(document["created_at"]),
    }


def _history_response(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for entry in history:
        changes = {}
        for field_name, change in entry.get("changes", {}).items():
            converted = dict(change)
            if field_name in ("punch_in", "punch_out"):
                converted["from"] = _to_epoch_ms(converted.get("from"))
                converted["to"] = _to_epoch_ms(converted.get("to"))
            changes[field_name] = converted
        result.append(
            {
                "at": _to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return result


def _attendance_response(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": _to_epoch_ms(document.get("punch_in")),
        "punch_out": _to_epoch_ms(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": _history_response(document.get("history", [])),
    }


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        _parse_date(value)
        return value

    @model_validator(mode="after")
    def validate_shift(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: EpochMillis | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def validate_punched_at(self) -> PunchInRequest:
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or an epoch-millisecond integer")
        return self


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: EpochMillis | None = None

    @model_validator(mode="after")
    def validate_punched_at(self) -> PunchOutRequest:
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or an epoch-millisecond integer")
        return self


class RegularizeRequest(BaseModel):
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: NullableEpochMillis = None
    punch_out: NullableEpochMillis = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_status(self) -> RegularizeRequest:
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        return self


def _working_day_expression(date_field: str = "$date") -> dict[str, Any]:
    weekday = {"$dayOfWeek": {"$dateFromString": {"dateString": date_field}}}
    return {"$and": [{"$gte": [weekday, 2]}, {"$lte": [weekday, 6]}]}


def _monthly_attendance_pipeline(emp_code: str, start: str, end: str) -> list[dict[str, Any]]:
    is_presence = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    is_working_day = _working_day_expression()
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [is_presence, is_working_day]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def _monthly_employee_metrics_pipeline(start: str, end: str) -> list[dict[str, Any]]:
    present = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    workday = _working_day_expression()
    records_pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$set": {
                "_is_workday": workday,
                "_is_present": present,
                "_work_hours": "$work_hours",
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$_is_present", "$_is_workday"]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$_is_present", {"$ne": [{"$ifNull": ["$_work_hours", None]}, None]}]},
                            "$_work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$_is_present", {"$ne": [{"$ifNull": ["$_work_hours", None]}, None]}]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]
    return [
        {"$match": {"joined_on": {"$lte": end}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": records_pipeline,
                "as": "_monthly",
            }
        },
        {"$set": {"_metrics": {"$arrayElemAt": ["$_monthly", 0]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$_metrics.present_days", 0]}},
                "work_hours_sum": {"$sum": {"$ifNull": ["$_metrics.work_hours_sum", 0]}},
                "work_hours_count": {"$sum": {"$ifNull": ["$_metrics.work_hours_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$_metrics.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$_metrics.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$_metrics.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$_metrics.on_duty_count", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def _department_summary_pipeline(
    start: str,
    end: str,
    department: str | None,
) -> list[dict[str, Any]]:
    pipeline = _monthly_employee_metrics_pipeline(start, end)
    if department is not None:
        pipeline[0]["$match"]["department"] = department
    return pipeline


def _leaderboard_pipeline(
    start: str,
    end: str,
    department: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "pipeline": ([{"$match": {"department": department}}] if department is not None else []),
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$_employee.name",
                "department": "$_employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def _trend_pipeline(department: str, start: date, end: date) -> list[dict[str, Any]]:
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "date": {"$literal": start_dt}}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {
            "$set": {
                "_date_string": {
                    "$dateToString": {
                        "date": "$date",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
                "_is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$_date_string", "dept": {"$literal": department}},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", "$$dept"]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "_date_string",
                "foreignField": "date",
                "pipeline": [
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "pipeline": [{"$match": {"department": department}}],
                            "as": "_employee",
                        }
                    },
                    {"$unwind": "$_employee"},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    }
                ],
                "as": "_daily",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.count", 0]}, 0]},
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.late_count", 0]}, 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$_is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_date_string",
                "is_working_day": "$_is_working_day",
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


def _explain_find(query: dict[str, Any], page: int, page_size: int) -> dict[str, Any]:
    return db.command(
        "explain",
        {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        },
        verbosity="executionStats",
    )


def _explain_aggregate(collection: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


def _json_safe(document: dict[str, Any]) -> dict[str, Any]:
    return json_util.loads(json_util.dumps(document))


def _attendance_query(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_filter: dict[str, str] = {}
        if date_from is not None:
            date_filter["$gte"] = date_from
        if date_to is not None:
            date_filter["$lte"] = date_to
        query["date"] = date_filter
    if status is not None:
        query["status"] = status
    return query


@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    document = body.model_dump()
    document["created_at"] = _utc_now()
    try:
        employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return _employee_response(document)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    query = {"department": department} if department is not None else {}
    total = employees.count_documents(query)
    records = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [_employee_response(record) for record in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    employee = employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punch_time = _from_epoch_ms(body.punched_at) if body.punched_at is not None else _utc_now()
    attendance_day = _attendance_date(punch_time, employee)
    document = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punch_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_time, employee, attendance_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return _attendance_response(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    employee = employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punch_time = _from_epoch_ms(body.punched_at) if body.punched_at is not None else _utc_now()
    record = attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_time}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    punch_in_time = record.get("punch_in")
    if punch_time <= punch_in_time:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if punch_time - punch_in_time > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in_time, punch_time)
    overtime_minutes = compute_overtime(punch_time, employee, record["date"])
    updated = attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_time,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.5,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    return _attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    parsed_from = _date_range(date_from, "date_from") if date_from is not None else None
    parsed_to = _date_range(date_to, "date_to") if date_to is not None else None
    if parsed_from is not None and parsed_to is not None and parsed_from > parsed_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    query = _attendance_query(
        emp_code,
        parsed_from.isoformat() if parsed_from else None,
        parsed_to.isoformat() if parsed_to else None,
        status,
    )
    total = attendance_logs.count_documents(query)
    records = (
        attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [_attendance_response(record) for record in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest) -> dict[str, Any]:
    attendance_day = _date_range(date)
    employee = employees.find_one({"emp_code": emp_code})
    record = attendance_logs.find_one({"emp_code": emp_code, "date": attendance_day.isoformat()})
    if employee is None or record is None:
        raise HTTPException(status_code=404, detail="employee or attendance record not found")

    supplied = body.model_fields_set
    final_status = body.status if "status" in supplied else record["status"]
    if final_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(status_code=422, detail="punch times cannot be supplied with ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        final_punch_in = (
            _from_epoch_ms(body.punch_in)
            if "punch_in" in supplied and body.punch_in is not None
            else None
            if "punch_in" in supplied
            else record.get("punch_in")
        )
        final_punch_out = (
            _from_epoch_ms(body.punch_out)
            if "punch_out" in supplied and body.punch_out is not None
            else None
            if "punch_out" in supplied
            else record.get("punch_out")
        )
        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if _attendance_date(final_punch_in, employee) != attendance_day.isoformat():
            raise HTTPException(status_code=422, detail="punch_in must remain on the record attendance date")
        _validate_punch_pair(final_punch_in, final_punch_out)

    if final_status in ("ABSENT", "LEAVE"):
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_late_minutes = compute_late_minutes(final_punch_in, employee, attendance_day.isoformat())
        if final_punch_out is None:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False
        else:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(final_punch_out, employee, attendance_day.isoformat())
            final_half_day = final_work_hours < 4.5

    previous_values = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    next_values = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
    }
    changes = {
        field_name: {"from": previous_values[field_name], "to": next_values[field_name]}
        for field_name in next_values
        if previous_values[field_name] != next_values[field_name]
    }
    if not changes:
        raise HTTPException(status_code=422, detail="regularization must change at least one field")

    history_entry = {
        "at": _utc_now(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history_filter = (
        {"history": record["history"]}
        if "history" in record
        else {"history": {"$exists": False}}
    )
    updated = attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": attendance_day.isoformat(), **history_filter},
        {"$set": next_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record was concurrently regularized")
    return _attendance_response(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> dict[str, Any]:
    employee = employees.find_one({"emp_code": emp_code}, {"_id": 1, "joined_on": 1})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    start_text, month_end_text, month_start, month_end = _month_bounds(month)
    summary = next(
        attendance_logs.aggregate(_monthly_attendance_pipeline(emp_code, start_text, month_end_text)),
        None,
    ) or {}
    joined_on = _parse_date(employee["joined_on"])
    working_start = max(month_start, joined_on)
    working_days = 0
    if working_start <= month_end:
        working_days = sum(
            1
            for day_offset in range((month_end - working_start).days + 1)
            if (working_start + timedelta(days=day_offset)).weekday() < 5
        )
    present_days = _round_number(summary.get("present_days", 0))
    attendance_pct = (
        _round_number(Decimal(str(present_days)) / Decimal(working_days) * 100)
        if working_days
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: str | None = None,
) -> dict[str, Any]:
    start, month_end, _, _ = _month_bounds(month)
    results = list(employees.aggregate(_department_summary_pipeline(start, month_end, department)))
    items = []
    for result in results:
        items.append(
            {
                "department": result["department"],
                "headcount": result["headcount"],
                "present_days": _round_number(result["present_days"]),
                "avg_work_hours": _round_number(result["avg_work_hours"]) if result["avg_work_hours"] is not None else None,
                "late_count": result["late_count"],
                "total_late_minutes": result["total_late_minutes"],
                "leave_count": result["leave_count"],
                "on_duty_count": result["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
) -> dict[str, Any]:
    start, month_end, _, _ = _month_bounds(month)
    items = list(attendance_logs.aggregate(_leaderboard_pipeline(start, month_end, department, limit)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: str,
) -> dict[str, Any]:
    start = _date_range(from_, "from")
    end = _date_range(to, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    pipeline = _trend_pipeline(department, start, end)
    items = []
    for row in attendance_logs.database.command(
        "aggregate",
        "employees",
        pipeline=pipeline,
        cursor={},
    )["cursor"]["firstBatch"]:
        items.append(
            {
                "date": row["date"],
                "is_working_day": row["is_working_day"],
                "headcount": row["headcount"],
                "present_count": _round_number(row["present_count"]),
                "late_count": row["late_count"],
                "attendance_rate": (
                    _round_number(row["attendance_rate"], 4)
                    if row["attendance_rate"] is not None
                    else None
                ),
                "moving_avg_7d": (
                    _round_number(row["moving_avg_7d"], 4)
                    if row["moving_avg_7d"] is not None
                    else None
                ),
            }
        )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: str | None = None,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    collection: str
    if endpoint == "attendance_list":
        parsed_from = _date_range(date_from, "date_from") if date_from is not None else None
        parsed_to = _date_range(date_to, "date_to") if date_to is not None else None
        if parsed_from is not None and parsed_to is not None and parsed_from > parsed_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        query = _attendance_query(
            emp_code,
            parsed_from.isoformat() if parsed_from else None,
            parsed_to.isoformat() if parsed_to else None,
            status,
        )
        collection = "attendance_logs"
        explain = _explain_find(query, page, page_size)
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        start, month_end, _, _ = _month_bounds(month)
        collection = "attendance_logs"
        explain = _explain_aggregate(
            collection,
            _monthly_attendance_pipeline(emp_code, start, month_end),
        )
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        start, month_end, _, _ = _month_bounds(month)
        collection = "employees"
        explain = _explain_aggregate(
            collection,
            _department_summary_pipeline(start, month_end, department),
        )
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        start, month_end, _, _ = _month_bounds(month)
        collection = "attendance_logs"
        explain = _explain_aggregate(
            collection,
            _leaderboard_pipeline(start, month_end, department, limit),
        )
    else:
        if department is None or from_ is None or to is None:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        start_day = _date_range(from_, "from")
        end_day = _date_range(to, "to")
        if end_day < start_day or (end_day - start_day).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid department trend date range")
        collection = "employees"
        explain = _explain_aggregate(
            collection,
            _trend_pipeline(department, start_day, end_day),
        )
    return {"endpoint": endpoint, "collection": collection, "explain": _json_safe(explain)}
