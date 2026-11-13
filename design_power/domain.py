from collections import Counter, defaultdict
from statistics import mean, median
import math

from .validation import require_tables, unique, finite, references


def validate(data):
    require_tables(data, ["plans", "slots", "factors", "estimands"])
    unique(data["plans"], ["plan_id"])
    unique(data["slots"], ["slot_id"])
    finite(data["budget"], minimum=0)
    finite(data["per_student_cost"], minimum=0)
    for plan in data["plans"]:
        for field in ["classes", "class_size", "rounds", "sessions", "student_levels"]:
            finite(plan[field], minimum=1)
            if not isinstance(plan[field], int):
                raise ValueError("人数和排期数量必须是整数")
        if len(plan["class_allocation"]) != plan["classes"]:
            raise ValueError("班级安排与班数不一致")
    for slot in data["slots"]:
        for field in ["seats", "rooms", "cost"]:
            finite(slot[field], minimum=0)


def explore(data, selection):
    plan_id = selection.get("plan", "balanced")
    plan = next((record for record in data["plans"] if record["plan_id"] == plan_id), None)
    if plan is None:
        raise ValueError("未知排期方案")
    requested = plan["classes"] * plan["class_size"] * plan["rounds"]
    selected_slots = data["slots"][:plan["sessions"]]
    available = sum(slot["seats"] * slot["rooms"] for slot in selected_slots)
    cost = sum(slot["cost"] * slot["rooms"] for slot in selected_slots) + plan["classes"] * plan["class_size"] * data["per_student_cost"]
    rows = [{"slot_id": slot["slot_id"], "seat_capacity": slot["seats"] * slot["rooms"],
             "room_cost": slot["cost"] * slot["rooms"]} for slot in selected_slots]
    return {"summary": {"计划参与人数": plan["classes"] * plan["class_size"], "测量座次需求": requested,
                        "时段总座次": available, "预计费用": cost, "预算": data["budget"]},
            "rows": rows, "series": [{"label": row["slot_id"], "y": row["seat_capacity"]} for row in rows],
            "notes": ["这里只列资源账，不判定随机化设计是否成立，也不提供统计功效。", "总座次足够不保证每班在所需时段都能被安排。"]}
