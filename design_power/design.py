"""设计评估：在明确的规划区间内，复算一个排期方案能区分什么、不能区分什么。

本模块把三个问题分开回答，而不是合成一个分数：

1. 资源：给定班级数、每班人数、复测轮次和时段，座次、时段数与预算是否装得下。
2. 识别：随机化发生在哪一层，每个关注差异是否至少有两个独立单位；哪些差异会
   被班级或时段整个吸收，因而无法与班级/时段分开。
3. 统计能力：在假设区间的两端重复仿真，报告拒绝零假设的比例及其蒙特卡洛区间，
   以及在目标功效下可检出的最小差异。

所有功效数字来自对上述设计的一次次复算，不使用独立样本公式。仿真设置（种子、
重复次数、显著性水平）、假设区间和方案参数共同构成复算指纹；改变其中任何一项
都会得到新的指纹。结论取区间中不利的一端，不把多组尝试里最乐观的一次当作承诺。
"""

from dataclasses import dataclass
import math
import random
from statistics import mean

from .validation import finite
from .storage import fingerprint


MODEL_VERSION = "design-power/1"
# 固定情景偏移，保证同一 (种子, 情景, 重复序号) 在任何时候得到同一组随机数。
SCENARIO_OFFSETS = {"low": 104729, "high": 130363}
TARGET_POWER = 0.8
Z_95 = 1.959963984540054


# ---------------------------------------------------------------------------
# 分布与区间工具（仅标准库）
# ---------------------------------------------------------------------------

def _betacf(a, b, x):
    maximum, epsilon, tiny = 300, 3.0e-14, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, maximum + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return h


def _betai(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                 + a * math.log(x) + b * math.log(1.0 - x))
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(statistic, degrees):
    """双侧 t 检验的 p 值；自由度不足 1 时没有可报告的推断。"""
    if degrees < 1 or not math.isfinite(statistic):
        return 1.0
    return _betai(degrees / 2.0, 0.5, degrees / (degrees + statistic * statistic))


def wilson_interval(successes, total, z=Z_95):
    if total <= 0:
        return [0.0, 1.0]
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2.0 * total)) / denominator
    half = z * math.sqrt(proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)) / denominator
    return [max(0.0, centre - half), min(1.0, centre + half)]


def _pair(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} 应为含两个端点的区间")
    low, high = value
    finite(low, minimum=0)
    finite(high, minimum=0)
    if low > high:
        raise ValueError(f"{name} 的左端点不能大于右端点")
    return float(low), float(high)


def validate_design_tables(data):
    """导入时校验评估所需的结构：因素层级、估计目标、最小差异与历史区间。

    资源是否装得下不在这里判断——那属于资源门，必须作为可解释的结论呈现，
    而不是让整份数据导入失败。
    """
    units, levels = _factor_map(data)
    estimands, minimums, class_factor, student_factor, interaction = _resolve_estimands(data, units, levels)
    if class_factor is None or student_factor is None:
        raise ValueError("设计评估需要一个班级层因素和一个学生层因素")
    Uncertainty.from_data(data).validate()
    for plan in data.get("plans", []):
        for field in ["classes", "class_size", "rounds", "sessions"]:
            value = plan.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("方案参数必须是正整数：" + field)
        allocation = plan.get("class_allocation")
        if not isinstance(allocation, list) or len(allocation) != plan["classes"]:
            raise ValueError("班级安排与班数不一致")
        if any(value not in levels[class_factor] for value in allocation):
            raise ValueError("班级安排引用了因素未提供的水平")
        if plan.get("student_levels") != len(levels[student_factor]):
            raise ValueError("方案的 student_levels 与因素水平数不一致")
    return {"class_factor": class_factor, "student_factor": student_factor,
            "interaction": interaction, "estimands": list(estimands)}


# ---------------------------------------------------------------------------
# 规划假设与情景
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Uncertainty:
    """规划假设区间；端点用于构造低端/高端情景，不把历史估计当成真值。

    重复测量按方差分量建模：班级层、学生稳定层、每轮测量残差、时段/批次扰动。
    两轮之间的复测相关由分量推导（学生稳定方差 / 学生层总方差），不另设一个会与
    分量冲突的自由参数；历史材料给出的相关区间只用于一致性核对。
    """
    class_sd: tuple
    student_sd: tuple
    residual_sd: tuple
    absence: tuple
    period_sd: tuple = (0.0, 0.0)
    reported_correlation: tuple | None = None

    @classmethod
    def from_data(cls, data):
        historical = data.get("historical")
        if not isinstance(historical, dict):
            raise ValueError("缺少历史方差与缺席区间：historical")
        variances = historical.get("variance_ranges")
        if not isinstance(variances, dict):
            raise ValueError("缺少 historical.variance_ranges")
        for name in ["class", "student", "residual"]:
            if name not in variances:
                raise ValueError("缺少历史方差区间：" + name)
        period = variances.get("period", [0, 0])
        correlation = historical.get("repeat_correlation")
        reported = None
        if correlation is not None:
            reported = tuple(_pair(correlation, "historical.repeat_correlation"))
            if reported[1] >= 1:
                raise ValueError("复测相关系数区间必须小于 1")
        return cls(
            class_sd=tuple(math.sqrt(value) for value in _pair(variances["class"], "historical.variance_ranges.class")),
            student_sd=tuple(math.sqrt(value) for value in _pair(variances["student"], "historical.variance_ranges.student")),
            residual_sd=tuple(math.sqrt(value) for value in _pair(variances["residual"], "historical.variance_ranges.residual")),
            absence=tuple(_pair(historical.get("absence_ranges"), "historical.absence_ranges")),
            period_sd=tuple(math.sqrt(value) for value in _pair(period, "historical.variance_ranges.period")),
            reported_correlation=reported,
        )

    @classmethod
    def from_overrides(cls, data, overrides):
        base = cls.from_data(data)
        if overrides is None:
            return base
        if not isinstance(overrides, dict):
            raise ValueError("假设区间必须是对象")
        allowed = {"class_sd", "student_sd", "residual_sd", "absence", "period_sd"}
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError("未知的假设区间：" + ", ".join(sorted(unknown))
                             + "；复测相关由方差分量推导，不能单独覆盖")
        values = base.as_json()
        values.update(overrides)
        return cls(
            class_sd=tuple(_pair(values["class_sd"], "class_sd")),
            student_sd=tuple(_pair(values["student_sd"], "student_sd")),
            residual_sd=tuple(_pair(values["residual_sd"], "residual_sd")),
            absence=tuple(_pair(values["absence"], "absence")),
            period_sd=tuple(_pair(values["period_sd"], "period_sd")),
            reported_correlation=base.reported_correlation,
        )

    def validate(self):
        for name in ["class_sd", "student_sd", "residual_sd", "absence", "period_sd"]:
            low, high = getattr(self, name)
            if not (math.isfinite(low) and math.isfinite(high)) or low < 0 or low > high:
                raise ValueError("假设区间无效：" + name)
        if self.absence[1] >= 1:
            raise ValueError("空缺率区间必须小于 1")
        if self.student_sd[1] + self.residual_sd[1] <= 0:
            raise ValueError("学生层与残差方差不能同时为 0，否则复测相关无定义")
        if self.reported_correlation is not None:
            low, high = self.reported_correlation
            if not (math.isfinite(low) and math.isfinite(high)) or not 0 <= low <= high < 1:
                raise ValueError("历史复测相关系数应在 0 与 1 之间")
        return self

    def as_json(self):
        return {
            "class_sd": list(self.class_sd),
            "student_sd": list(self.student_sd),
            "residual_sd": list(self.residual_sd),
            "absence": list(self.absence),
            "period_sd": list(self.period_sd),
        }


def implied_correlation(student_sd, residual_sd):
    total = student_sd ** 2 + residual_sd ** 2
    return student_sd ** 2 / total if total > 0 else 0.0


@dataclass(frozen=True)
class Scenario:
    position: str
    label: str
    class_sd: float
    student_sd: float
    residual_sd: float
    absence: float
    period_sd: float

    @property
    def retest_correlation(self):
        return implied_correlation(self.student_sd, self.residual_sd)

    def assumptions(self):
        return {
            "class_sd": self.class_sd,
            "student_sd": self.student_sd,
            "residual_sd": self.residual_sd,
            "absence": self.absence,
            "period_sd": self.period_sd,
            "implied_retest_correlation": round(self.retest_correlation, 4),
        }


def scenarios(uncertainty):
    """只取区间两端：低端把各项都放在有利端，高端都放在不利端。

    高端用于判断统计能力是否充足；低端只作为乐观边界展示。不把区间内的
    某个中间组合包装成“最可能”的情景，也不搜索对结论最有利的组合。
    “有利端”指低方差、低空缺、低时段扰动。
    """
    uncertainty.validate()
    return [
        Scenario("low", "区间低端（各项取有利端）", uncertainty.class_sd[0], uncertainty.student_sd[0],
                 uncertainty.residual_sd[0], uncertainty.absence[0], uncertainty.period_sd[0]),
        Scenario("high", "区间高端（各项取不利端）", uncertainty.class_sd[1], uncertainty.student_sd[1],
                 uncertainty.residual_sd[1], uncertainty.absence[1], uncertainty.period_sd[1]),
    ]


def correlation_diagnostics(uncertainty):
    """把历史报告的相关区间与方差分量推导出的相关并列，冲突时明确提示而不是掩盖。"""
    if uncertainty.reported_correlation is None:
        return {"reported": None, "implied": None, "compatible": True,
                "note": "数据未提供复测相关区间；相关由方差分量推导。"}
    low, high = uncertainty.reported_correlation
    implied_low = implied_correlation(uncertainty.student_sd[0], uncertainty.residual_sd[1])
    implied_high = implied_correlation(uncertainty.student_sd[1], uncertainty.residual_sd[0])
    compatible = not (implied_high < low or implied_low > high)
    return {
        "reported": [low, high],
        "implied": [round(implied_low, 4), round(implied_high, 4)],
        "compatible": compatible,
        "note": ("历史相关区间与方差分量推导出的范围有重叠。" if compatible else
                 "历史相关区间与方差分量推导出的范围不重叠：以方差分量为准，并在记录中保留这一冲突。"),
    }


# ---------------------------------------------------------------------------
# 方案参数与调整
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SimulationSpec:
    replications: int = 240
    seed: int = 20260914
    alpha: float = 0.05
    target_power: float = TARGET_POWER

    def validate(self):
        if not isinstance(self.replications, int) or isinstance(self.replications, bool):
            raise ValueError("仿真重复次数必须是整数")
        if not 40 <= self.replications <= 4000:
            raise ValueError("仿真重复次数应在 40 与 4000 之间")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise ValueError("随机种子必须是非负整数")
        finite(self.alpha, minimum=0.001, maximum=0.25)
        finite(self.target_power, minimum=0.5, maximum=0.99)
        return self

    @classmethod
    def from_value(cls, value):
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value.validate()
        if not isinstance(value, dict):
            raise ValueError("仿真设置必须是对象")
        allowed = {"replications", "seed", "alpha", "target_power"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("未知的仿真设置：" + ", ".join(sorted(unknown)))
        return cls(**value).validate()

    def as_json(self):
        return {"replications": self.replications, "seed": self.seed,
                "alpha": self.alpha, "target_power": self.target_power}


@dataclass(frozen=True)
class PlanDesign:
    plan_id: str
    classes: int
    class_size: int
    rounds: int
    sessions: int
    allocation: tuple
    class_levels: tuple
    student_levels: tuple
    estimands: tuple
    class_factor: str
    student_factor: str
    interaction: str
    minimum_differences: dict
    derivation: tuple

    def as_json(self):
        return {
            "plan_id": self.plan_id,
            "classes": self.classes,
            "class_size": self.class_size,
            "rounds": self.rounds,
            "sessions": self.sessions,
            "class_allocation": list(self.allocation),
            "class_levels": list(self.class_levels),
            "student_levels": list(self.student_levels),
            "estimands": list(self.estimands),
            "class_factor": self.class_factor,
            "student_factor": self.student_factor,
            "interaction": self.interaction,
            "minimum_differences": dict(self.minimum_differences),
            "derivation": list(self.derivation),
        }


def _factor_map(data):
    factors = data.get("factors")
    if not isinstance(factors, list) or not factors:
        raise ValueError("factors 不能为空")
    units = {}
    levels = {}
    for factor in factors:
        if not isinstance(factor, dict):
            raise ValueError("因素记录必须是对象")
        name = factor.get("name")
        unit = factor.get("unit")
        if not isinstance(name, str) or not name:
            raise ValueError("因素名称必须是非空字符串")
        if name in units:
            raise ValueError("因素名称重复：" + name)
        if unit not in {"class", "student"}:
            raise ValueError("因素的随机化层级必须是 class 或 student：" + name)
        values = factor.get("levels")
        if not isinstance(values, list) or len(values) != 2 or len(set(values)) != 2:
            raise ValueError("因素必须给出两个不同水平：" + name)
        units[name] = unit
        levels[name] = tuple(values)
    return units, levels


def _resolve_estimands(data, units, levels):
    estimands = data.get("estimands")
    if not isinstance(estimands, list) or not estimands:
        raise ValueError("estimands 不能为空")
    class_names = [name for name, unit in units.items() if unit == "class"]
    student_names = [name for name, unit in units.items() if unit == "student"]
    selected_class = [name for name in class_names if name in estimands]
    selected_student = [name for name in student_names if name in estimands]
    if len(selected_class) > 1 or len(selected_student) > 1:
        raise ValueError("当前设计只支持一个班级层因素和一个学生层因素")
    interaction = None
    for name in estimands:
        if ":" in name:
            parts = name.split(":")
            if len(parts) != 2 or set(parts) != {*(selected_class + selected_student)} or not selected_class or not selected_student:
                raise ValueError("交互项必须是班级层因素与学生层因素的组合：" + name)
            interaction = name
    supported = set(selected_class) | set(selected_student) | ({interaction} if interaction else set())
    if set(estimands) - supported:
        raise ValueError("estimands 含有当前设计不支持的项目：" + ", ".join(sorted(set(estimands) - supported)))
    if not selected_class and not selected_student:
        raise ValueError("estimands 没有引用任何已声明的因素")
    minimums = data.get("minimum_differences")
    if not isinstance(minimums, dict):
        raise ValueError("缺少 minimum_differences")
    for name in estimands:
        finite(minimums.get(name), minimum=0)
    return tuple(estimands), dict(minimums), (selected_class[0] if selected_class else None), \
        (selected_student[0] if selected_student else None), interaction


def _allocation_from(classes, class_levels, base_allocation, arm_counts, order):
    if arm_counts is None and base_allocation is not None:
        if len(base_allocation) != classes:
            raise ValueError("调整班级数后必须同时给出各班教学方式")
        if any(value not in class_levels for value in base_allocation):
            raise ValueError("班级安排引用了因素未提供的水平")
        return tuple(base_allocation)
    if not isinstance(arm_counts, dict):
        raise ValueError("调整班级数时请给出各班教学方式或 A/B 班数")
    if set(arm_counts) != set(class_levels):
        raise ValueError("班数分配必须正好使用因素的两个水平")
    counts = []
    for level in class_levels:
        value = arm_counts[level]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("每个水平的班数必须是非负整数")
        counts.append(value)
    if sum(counts) != classes:
        raise ValueError("各班数之和必须等于班级数")
    if order not in {"alternating", "blocked"}:
        raise ValueError("分配顺序必须是 alternating 或 blocked")
    if order == "blocked":
        return tuple([class_levels[0]] * counts[0] + [class_levels[1]] * counts[1])
    remaining = list(counts)
    result = []
    index = 0
    while len(result) < classes:
        level = class_levels[index % 2]
        position = index % 2
        if remaining[position] > 0:
            result.append(level)
            remaining[position] -= 1
            index += 1
        else:
            other = 1 - position
            if remaining[other] <= 0:
                break
            result.append(class_levels[other])
            remaining[other] -= 1
            index += 1
    return tuple(result)


def build_design(data, plan_id, overrides=None):
    """从数据中的方案出发，允许只调整班级数、每班人数、轮次和时段，不改写源数据。"""
    units, levels = _factor_map(data)
    estimands, minimums, class_factor, student_factor, interaction = _resolve_estimands(data, units, levels)
    plan = next((record for record in data.get("plans", []) if record.get("plan_id") == plan_id), None)
    if plan is None:
        raise ValueError("未知排期方案")
    if class_factor is None:
        raise ValueError("当前设计没有班级层因素，无法进行整班随机化评估")
    if student_factor is None:
        raise ValueError("当前设计没有学生层因素，无法进行班内练习评估")
    class_levels = levels[class_factor]
    student_level_levels = levels[student_factor]
    overrides = overrides or {}
    if not isinstance(overrides, dict):
        raise ValueError("方案调整必须是对象")
    allowed = {"classes", "class_size", "rounds", "sessions", "class_allocation", "arm_counts", "order"}
    unknown = set(overrides) - allowed
    if unknown:
        raise ValueError("未知的方案调整字段：" + ", ".join(sorted(unknown)))
    classes = overrides.get("classes", plan.get("classes"))
    class_size = overrides.get("class_size", plan.get("class_size"))
    rounds = overrides.get("rounds", plan.get("rounds"))
    sessions = overrides.get("sessions", plan.get("sessions"))
    for name, value in [("classes", classes), ("class_size", class_size), ("rounds", rounds), ("sessions", sessions)]:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} 必须是正整数")
    if classes > 400:
        raise ValueError("班级数超过 400，当前仿真不支持")
    if class_size > 200:
        raise ValueError("每班人数超过 200，当前仿真不支持")
    if rounds > 8:
        raise ValueError("复测轮次超过 8，当前仿真不支持")
    if sessions > 400:
        raise ValueError("使用时段数超过 400，当前仿真不支持")
    base_allocation = plan.get("class_allocation")
    if overrides.get("class_allocation") is not None:
        allocation = _allocation_from(classes, class_levels, overrides["class_allocation"], None, None)
        derivation = ["使用显式班级安排"]
    elif overrides.get("arm_counts") is not None:
        allocation = _allocation_from(classes, class_levels, None, overrides["arm_counts"],
                                      overrides.get("order", "alternating"))
        derivation = ["按 " + overrides.get("order", "alternating") + " 顺序生成班级安排"]
    else:
        allocation = _allocation_from(classes, class_levels, base_allocation, None, None)
        derivation = ["沿用原方案的班级安排"]
    declared = plan.get("student_levels")
    if declared is not None and declared != len(student_level_levels):
        raise ValueError("方案的 student_levels 与因素水平数不一致")
    if class_size < 2:
        raise ValueError("每班人数至少为 2，否则班内练习无法区分")
    return PlanDesign(plan_id=plan_id, classes=classes, class_size=class_size, rounds=rounds,
                      sessions=sessions, allocation=allocation, class_levels=class_levels,
                      student_levels=student_level_levels, estimands=estimands,
                      class_factor=class_factor, student_factor=student_factor,
                      interaction=interaction, minimum_differences=minimums,
                      derivation=tuple(derivation))


# ---------------------------------------------------------------------------
# 资源门与排期诊断
# ---------------------------------------------------------------------------

def resource_gate(data, design):
    """只回答资源装不装得下；不评价随机化是否成立或统计能力是否足够。

    口径与排期表一致：每个时段的 seats 是单机房座位数、rooms 是并行机房数，
    所以该时段可容纳 seats×rooms 人；一个方案使用前 sessions 个时段。测量可以
    多班同时进行，因此这里不把“每班独立占一间机房”当作约束，只核对总座次、
    单机房是否至少能坐下一名学生、时段数、预算。
    """
    slots = data.get("slots")
    if not isinstance(slots, list):
        raise ValueError("缺少 slots")
    selected = list(slots[:design.sessions])
    missing = max(0, design.sessions - len(slots))
    reasons = []
    checks = []
    if missing:
        reasons.append(f"方案需要 {design.sessions} 个时段，现有只有 {len(slots)} 个")
        checks.append({"name": "时段数", "required": design.sessions, "available": len(slots),
                       "ok": False, "blocking": True})
    else:
        checks.append({"name": "时段数", "required": design.sessions, "available": len(slots),
                       "ok": True, "blocking": True})
    seat_need = design.classes * design.class_size * design.rounds
    seat_capacity = sum(slot["seats"] * slot["rooms"] for slot in selected)
    checks.append({"name": "测量座次", "required": seat_need, "available": seat_capacity,
                   "ok": seat_capacity >= seat_need, "blocking": True})
    if seat_capacity < seat_need:
        reasons.append(f"测量座次需求 {seat_need} 超过所选时段总座次 {seat_capacity}")
    room_cost = sum(slot["cost"] * slot["rooms"] for slot in selected)
    cost = room_cost + design.classes * design.class_size * data["per_student_cost"]
    budget = data["budget"]
    checks.append({"name": "预算", "required": round(cost, 2), "available": budget,
                   "ok": cost <= budget, "blocking": True})
    if cost > budget:
        reasons.append(f"预计费用 {round(cost, 2)} 超过预算 {budget}")
    # 提示项：若一个班要在同一机房内完成测量，单机房座位数是需要留意的余量。
    # 排期表未规定测量是否允许拆班，所以只作提示，不据此判定资源超限。
    smallest_room = min((slot["seats"] for slot in selected), default=0)
    checks.append({"name": "单机房座位与班额（提示，不计入判定）", "required": design.class_size,
                   "available": smallest_room, "ok": smallest_room >= design.class_size,
                   "blocking": False})
    rows = [{"slot_id": slot["slot_id"], "seats": slot["seats"], "rooms": slot["rooms"],
             "seat_capacity": slot["seats"] * slot["rooms"],
             "room_cost": slot["cost"] * slot["rooms"],
             "class_size_headroom": slot["seats"] - design.class_size} for slot in selected]
    return {
        "status": "infeasible" if reasons else "feasible",
        "reasons": reasons,
        "checks": checks,
        "rows": rows,
        "cost": round(cost, 2),
        "budget": budget,
        "total_seat_capacity": seat_capacity,
        "required_seats": seat_need,
        "missing_sessions": missing,
        "per_student_cost": data["per_student_cost"],
        "students": design.classes * design.class_size,
    }


# ---------------------------------------------------------------------------
# 估计
# ---------------------------------------------------------------------------

def _welch(groups):
    """两组独立均值的 Welch t 检验；每组至少两个单位才可推断。"""
    if len(groups) != 2 or any(len(group) < 2 for group in groups):
        return None
    counts = [len(group) for group in groups]
    averages = [mean(group) for group in groups]
    variances = [sum((value - average) ** 2 for value in group) / (count - 1)
                 for group, average, count in zip(groups, averages, counts)]
    difference = averages[0] - averages[1]
    variance = variances[0] / counts[0] + variances[1] / counts[1]
    if variance <= 0:
        return {"difference": difference, "standard_error": 0.0, "degrees": 0,
                "p_value": 0.0 if difference != 0 else 1.0, "unidentified": False}
    standard_error = math.sqrt(variance)
    denominator = ((variances[0] / counts[0]) ** 2 / (counts[0] - 1)
                   + (variances[1] / counts[1]) ** 2 / (counts[1] - 1))
    degrees = variance * variance / denominator if denominator > 0 else 0.0
    if degrees < 1 or not math.isfinite(degrees):
        return {"difference": difference, "standard_error": standard_error, "degrees": 0,
                "p_value": 1.0, "unidentified": True}
    return {"difference": difference, "standard_error": standard_error, "degrees": degrees,
            "p_value": min(1.0, max(0.0, t_two_sided_p(difference / standard_error, degrees))),
            "unidentified": False}


def _one_sample(values):
    """班级层面处理效应的单样本 t 检验；不足两个班级时无法推断。"""
    if len(values) < 2:
        return {"difference": mean(values) if values else None, "standard_error": None,
                "degrees": 0, "p_value": 1.0, "unidentified": True}
    difference = mean(values)
    variance = sum((value - difference) ** 2 for value in values) / (len(values) - 1)
    if variance <= 0:
        return {"difference": difference, "standard_error": 0.0, "degrees": len(values) - 1,
                "p_value": 0.0 if difference != 0 else 1.0, "unidentified": False}
    standard_error = math.sqrt(variance / len(values))
    degrees = len(values) - 1
    return {"difference": difference, "standard_error": standard_error, "degrees": degrees,
            "p_value": min(1.0, max(0.0, t_two_sided_p(difference / standard_error, degrees))),
            "unidentified": False}


def _student_contrasts(by_class, design):
    """班内去均值后，计算练习主效应与教学×练习交互的班级层对比。"""
    practice_low, practice_high = design.student_levels
    practice_values = []
    interaction_by_arm = {level: [] for level in design.class_levels}
    ordered = sorted(by_class)
    for class_index in ordered:
        rows = by_class[class_index]
        arm = design.allocation[class_index]
        class_mean = mean(row["score"] for row in rows)
        cells = {}
        for row in rows:
            cells.setdefault(row["practice"], []).append(row["score"] - class_mean)
        low = cells.get(practice_low, [])
        high = cells.get(practice_high, [])
        if low and high:
            contrast = mean(high) - mean(low)
            practice_values.append(contrast)
            interaction_by_arm[arm].append(contrast)
    interaction = _welch([interaction_by_arm[level] for level in design.class_levels])
    return _one_sample(practice_values), interaction, len(practice_values)


def _teaching_contrast(by_class, design):
    group_low = []
    group_high = []
    for class_index in sorted(by_class):
        average = mean(row["score"] for row in by_class[class_index])
        if design.allocation[class_index] == design.class_levels[0]:
            group_low.append(average)
        else:
            group_high.append(average)
    return _welch([group_low, group_high])


def _simulate_estimate(design, scenario, rng):
    """生成一次零效应复算，返回三个关注差异的检验结果。

    所有真实差异都由后续的平移量 delta 注入：均值差、Welch t 与单样本 t 的
    标准误和自由度对整体平移不变，因此同一批零效应复算可以精确给出任意 delta
    下的拒绝率，不必为每个候选差异重新仿真，也不会重复计入效应。
    """
    by_class = {}
    for class_index in range(design.classes):
        class_effect = rng.gauss(0.0, scenario.class_sd)
        # 时段/批次扰动：每个班级的测量批次共享一个扰动，它与教学方式完全混杂。
        batch_effect = rng.gauss(0.0, scenario.period_sd)
        practice_order = [design.student_levels[1]] * (design.class_size // 2) + \
                         [design.student_levels[0]] * (design.class_size - design.class_size // 2)
        rng.shuffle(practice_order)
        for practice in practice_order:
            stable = rng.gauss(0.0, scenario.student_sd)
            for round_index in range(design.rounds):
                if rng.random() < scenario.absence:
                    continue
                score = class_effect + batch_effect + stable + rng.gauss(0.0, scenario.residual_sd)
                by_class.setdefault(class_index, []).append(
                    {"score": score, "practice": practice, "round": round_index})
    results = {}
    practice_test, interaction_test, _ = _student_contrasts(by_class, design)
    results[design.student_factor] = practice_test
    if design.interaction:
        results[design.interaction] = interaction_test
    results[design.class_factor] = _teaching_contrast(by_class, design)
    return results, by_class


def _record(result):
    if result is None:
        return None
    return {"identified": not result["unidentified"], "difference": result["difference"],
            "standard_error": result["standard_error"], "degrees": result["degrees"],
            "truncated": False}


def _power_at(records, delta, alpha):
    """利用同一批零效应复算精确计算真实差异 delta 下的拒绝率。

    估计量对真实差异是整体平移，标准误和自由度不变；因此对每条复算，把
    观测差分平移到 delta 后重新做同一 t 检验即可。无法估计的复算（例如空缺后
    每班只剩一个练习水平）计为未拒绝，避免只挑“能算出来”的那部分复算报告
    乐观功效。
    """
    if not records:
        return 0.0
    rejected = 0
    for record in records:
        if record["truncated"] or not record["identified"]:
            continue
        standard_error = record["standard_error"]
        if standard_error is None:
            continue
        if standard_error <= 0:
            if delta != 0:
                rejected += 1
            continue
        statistic = (record["difference"] + delta) / standard_error
        if t_two_sided_p(statistic, record["degrees"]) <= alpha:
            rejected += 1
    return rejected / len(records)


def _mdd(records, alpha, target_power):
    """扫描目标功效对应的最小差异；达到不了就返回 None，而不是外推承诺。"""
    if not records:
        return {"point": None, "lower_confidence": None, "reached": False}
    upper = 1.0
    for _ in range(8):
        if _power_at(records, upper, alpha) >= target_power:
            break
        upper *= 2.0
    else:
        return {"point": None, "lower_confidence": None, "reached": False}
    points = []
    for index in range(81):
        delta = upper * index / 80.0
        power = _power_at(records, delta, alpha)
        successes = round(power * len(records))
        lower = wilson_interval(successes, len(records))[0]
        points.append((delta, power, lower))

    def crossing(key):
        previous = None
        for delta, power, lower in points:
            value = power if key == "point" else lower
            if value >= target_power:
                if previous is None:
                    return delta
                prior_delta, prior_value = previous
                if value == prior_value:
                    return delta
                fraction = (target_power - prior_value) / (value - prior_value)
                return prior_delta + fraction * (delta - prior_delta)
            previous = (delta, value)
        return None

    return {"point": crossing("point"), "lower_confidence": crossing("lower"),
            "reached": crossing("lower") is not None}


def schedule_ledger(data, design):
    """与排期表同一口径的账目，供“回到排期表核对”使用。"""
    resource = resource_gate(data, design)
    rows = resource["rows"]
    return {
        "summary": {
            "计划参与人数": design.classes * design.class_size,
            "测量座次需求": resource["required_seats"],
            "时段总座次": resource["total_seat_capacity"],
            "预计费用": resource["cost"],
            "预算": resource["budget"],
        },
        "rows": rows,
        "series": [{"label": row["slot_id"], "y": row["seat_capacity"]} for row in rows],
        "checks": resource["checks"],
        "notes": ["这是与排期表一致的资源账；座次总量足够不保证每班在所需时段都能被安排。",
                  "评估中的班级数、每班人数、复测轮次和时段已代入本账。"],
    }


# ---------------------------------------------------------------------------
# 组装结果
# ---------------------------------------------------------------------------

def randomisation_report(design, resource):
    """说明每个关注差异的随机化单位、独立单位数和会与之混在一起的层。"""
    class_counts = {level: design.allocation.count(level) for level in design.class_levels}
    report = []
    interaction_confounded = "交互项在班内估计、跨班比较；班级层面异质性会与交互混在一起"
    for name in design.estimands:
        if name == design.class_factor:
            report.append({
                "estimand": name,
                "unit": "class",
                "independent_units": class_counts,
                "randomised_at": "整班",
                "blocked_or_removed": ["班级", "时段"],
                "confounded_with": ["班级构成", "时段/机房批次", "教师或班级层面的任何稳定差异"],
                "note": "教学方式只改变一次，班级和时段都嵌套在方式之内，无法与方式效应分开。"
                        "增加学生人数不能增加这一层的独立单位。",
            })
        elif name == design.student_factor:
            report.append({
                "estimand": name,
                "unit": "student",
                "independent_units": {"classes": design.classes, "students_per_class": design.class_size},
                "randomised_at": "班内学生",
                "blocked_or_removed": ["班级", "时段"],
                "confounded_with": ["班级×练习分配的不平衡（有限样本）"],
                "note": "练习在班内随机，班内去均值会移除班级和时段的主效应；推断按班级层面的"
                        "对比汇聚，不把同班学生当作相互独立。",
            })
        else:
            report.append({
                "estimand": name,
                "unit": "class",
                "independent_units": class_counts,
                "randomised_at": "整班（教学方式）× 班内学生（练习）",
                "blocked_or_removed": ["班级", "时段"],
                "confounded_with": ["班级层面异质性", "时段与教学方式的重叠"],
                "note": interaction_confounded + "；每个班只能贡献本班教学方式下的练习对比。",
            })
    if resource["missing_sessions"]:
        for entry in report:
            if entry["estimand"] == design.class_factor:
                entry["confounded_with"].append("时段数不足导致部分班级无法按计划测量")
    return report


def identification_report(design):
    """结构上能不能区分，与统计能力是否足够分开。"""
    report = []
    class_counts = {level: design.allocation.count(level) for level in design.class_levels}
    for name in design.estimands:
        if name == design.class_factor:
            enough = all(count >= 2 for count in class_counts.values())
            report.append({
                "estimand": name,
                "status": "identified" if enough else "unidentifiable",
                "reason": "两种教学方式各至少有两个班级，可以做班级层比较" if enough
                          else "至少有一种教学方式不足两个班级，教学方式效应无法与班级/时段区分",
                "independent_units": class_counts,
            })
        elif name == design.student_factor:
            enough = design.class_size >= 2 and design.classes >= 2
            report.append({
                "estimand": name,
                "status": "identified" if enough else "unidentifiable",
                "reason": "每班至少两人形成两个练习水平，且至少两个班级提供班内对比" if enough
                          else "每班人数或班级数不足，班内练习对比无法在班级层推断",
                "independent_units": {"classes": design.classes, "students_per_class": design.class_size},
            })
        else:
            enough = all(count >= 2 for count in class_counts.values()) and design.class_size >= 2
            report.append({
                "estimand": name,
                "status": "identified" if enough else "unidentifiable",
                "reason": "两种教学方式各至少两个班级，且每班能形成两个练习对比" if enough
                          else "每种教学方式不足两个班级，或每班人数不足以在班内形成练习对比",
                "independent_units": class_counts,
            })
    return report


def _power_status(power, interval, spec, unstable, structurally_identified=True):
    # 结构上不可识别时，任何“功效”数字都没有解释；不与统计能力不足混为一谈。
    if not structurally_identified:
        return "unidentifiable", "结构上无法区分；增加人数或轮次不能补救"
    if unstable:
        return "unstable", "复算中存在无法估计的重复，需先处理空缺或班额"
    if interval[0] >= spec.target_power:
        return "reached", "区间下限已达到目标功效"
    if power >= spec.target_power:
        return "point_only", "点估计达到目标，但蒙特卡洛区间下限未达到，不作为承诺"
    if interval[1] < spec.target_power:
        return "insufficient", "区间上限仍低于目标功效"
    return "uncertain", "区间跨过目标功效，精度不足"


def assess_plan(data, plan_id, overrides=None, assumptions=None, simulation=None):
    """对一个方案做资源、识别、统计能力三层评估。结果完全由输入决定。"""
    spec = SimulationSpec.from_value(simulation)
    uncertainty = Uncertainty.from_overrides(data, assumptions).validate()
    design = build_design(data, plan_id, overrides)
    work = design.classes * design.class_size * design.rounds * spec.replications * 2
    if work > 750_000:
        raise ValueError(
            "该方案与仿真重复次数的组合超过本地复算上限；请减少班级数、班额、轮次或重复次数后再试"
            f"（当前约 {work} 个学生次）")
    resource = resource_gate(data, design)
    schedule = schedule_ledger(data, design)
    identification = identification_report(design)
    randomisation = randomisation_report(design, resource)
    scenario_results = []
    for scenario in scenarios(uncertainty):
        records = {name: [] for name in design.estimands}
        for replication in range(spec.replications):
            seed = spec.seed * 1000003 + SCENARIO_OFFSETS[scenario.position] * 10007 + replication
            results, _ = _simulate_estimate(design, scenario, random.Random(seed))
            for name in design.estimands:
                result = results.get(name)
                record = _record(result)
                if record is None:
                    record = {"identified": False, "difference": None, "standard_error": None,
                              "degrees": 0, "truncated": True}
                records[name].append(record)
        entries = []
        for name in design.estimands:
            target = design.minimum_differences.get(name, 0.0)
            power = _power_at(records[name], target, spec.alpha)
            successes = round(power * spec.replications)
            interval = wilson_interval(successes, spec.replications)
            estimable = sum(1 for record in records[name]
                            if record["identified"] and not record["truncated"])
            unstable = estimable < spec.replications
            identified_structurally = next(
                item["status"] == "identified" for item in identification if item["estimand"] == name)
            status, status_label = _power_status(power, interval, spec, unstable,
                                                 identified_structurally)
            mdd = _mdd(records[name], spec.alpha, spec.target_power) if identified_structurally \
                else {"point": None, "lower_confidence": None, "reached": False}
            entries.append({
                "estimand": name,
                "unit": "class" if name in {design.class_factor, design.interaction} else "student",
                "target_difference": target,
                "power": round(power, 4),
                "power_interval": [round(interval[0], 4), round(interval[1], 4)],
                "replications": spec.replications,
                "estimable_replications": estimable,
                "status": status,
                "status_label": status_label,
                "mdd": mdd,
            })
        scenario_results.append({
            "position": scenario.position,
            "label": scenario.label,
            "assumptions": scenario.assumptions(),
            "within_class_sd": round(math.sqrt(scenario.student_sd ** 2 + scenario.residual_sd ** 2), 4),
            "estimands": entries,
        })
    high = next(item for item in scenario_results if item["position"] == "high")
    feasibility = _feasibility(design, resource, identification, high["estimands"], spec)
    recipe = {
        "model_version": MODEL_VERSION,
        "plan": design.as_json(),
        "assumptions": uncertainty.as_json(),
        "correlation_check": correlation_diagnostics(uncertainty),
        "simulation": spec.as_json(),
        "scenario_offsets": dict(SCENARIO_OFFSETS),
        "random_numbers": "Mersenne Twister，每个重复的种子 = seed*1000003 + 情景偏移*10007 + 重复序号",
    }
    recipe["sha256"] = fingerprint(recipe)
    notes = [
        "功效来自仿真，不来自独立样本公式；独立单位是班级或班内学生，不是总人数。",
        "每个关注差异的功效分别按“该差异达到目标、其余差异为 0”计算，不是联合检出概率；"
        "显著性水平按单个检验计，未做多重比较校正。",
        "区间低端只作乐观边界；判断统计能力时使用区间高端。",
        "区间两端同时取各项的有利端或不利端，不是概率意义上的置信区间。",
        "同一方案参数、假设区间和仿真设置会得到同一结果；改变任一项都会改变复算指纹。",
        "不要在多组调整里只保留结果最好的一次；保存记录会冻结当时使用的全部输入。",
        "比较表只陈述差异，不判定哪个方案更好，也不替使用者选择。",
    ]
    if design.interaction:
        notes.append("交互项与班级层面异质性混在一起；不能把班级间练习差异的变化直接当成交互效应。")
    notes.append("时段是教学方式分配时的区组，不是随机化层；它不提供额外独立重复，"
                 "其固定偏移也无法与教学方式效应分开。")
    if resource["status"] == "infeasible":
        notes.append("资源未通过时，统计数字只描述“假如装得下”的能力，不代表可以实施。")
    if any(entry["status"] == "unidentifiable" for entry in identification):
        notes.append("识别未通过的项目无法靠增加人数或复测轮次补救；需要改变随机化或分配结构。")
    estimable_high = [entry for entry in high["estimands"] if entry["status"] != "unidentifiable"]
    if estimable_high and all(entry["status"] in {"insufficient", "unstable", "uncertain", "point_only"}
                              for entry in estimable_high):
        notes.append("区间高端下所有可估计的关注差异都未稳定达到目标功效；这是能力问题，"
                     "不是设计识别问题。")
    return {
        "model_version": MODEL_VERSION,
        "revision_sha256": None,
        "design": design.as_json(),
        "resource": resource,
        "schedule": schedule,
        "identification": identification,
        "randomisation": randomisation,
        "scenarios": scenario_results,
        "correlation_check": correlation_diagnostics(uncertainty),
        "feasibility": feasibility,
        "recipe": recipe,
        "notes": notes,
    }


def _feasibility(design, resource, identification, estimands, spec):
    """把资源超限、结构不可识别、统计能力不足分成三个独立结论。

    “可行”需要三者同时成立；任何一个未通过都不把方案说成不可行于所有目的，
    只说明它卡在哪一层。没有排名，也没有默认推荐。
    """
    problems = []
    if resource["status"] != "feasible":
        problems.append({"layer": "resource", "detail": "；".join(resource["reasons"])})
    unidentified = [item["estimand"] for item in identification if item["status"] != "identified"]
    if unidentified:
        problems.append({"layer": "identification", "detail": "无法区分：" + "、".join(unidentified)})
    # 无法识别的项目已经计入识别层，不在这里重复计为“功效不足”。
    estimable = [item for item in estimands if item["estimand"] not in unidentified]
    unstable = [item["estimand"] for item in estimable if item["status"] == "unstable"]
    if unstable:
        problems.append({"layer": "statistical_power",
                         "detail": "区间高端下多数重复无法形成可估计对比（空缺或班额不足）："
                                   + "、".join(unstable)})
    insufficient = [item["estimand"] for item in estimable if item["status"] == "insufficient"]
    if insufficient:
        problems.append({"layer": "statistical_power",
                         "detail": "区间高端下未达到目标功效：" + "、".join(insufficient)})
    uncertain = [item["estimand"] for item in estimable
                 if item["status"] in {"uncertain", "point_only"}]
    if uncertain:
        problems.append({"layer": "statistical_power",
                         "detail": "区间跨过目标功效或只有点估计达到，精度不足以作为承诺："
                                   + "、".join(uncertain)})
    passed = {"resource": resource["status"] == "feasible",
              "identification": not unidentified,
              "statistical_power": not insufficient and not uncertain}
    verdict = "feasible" if all(passed.values()) else "blocked"
    return {"verdict": verdict, "passed": passed, "problems": problems,
            "target_power": spec.target_power,
            "statement": "资源、识别、统计能力三层都通过才谈得上可行；本表不做方案优劣排序。"}


def compare_plans(data, plan_ids, overrides_by_plan=None, assumptions=None, simulation=None):
    """逐个复算方案，输出资源、识别和统计能力三层结果；不排序、不挑选。

    每个方案独立复算、独立指纹。不同方案的行可以有不同的班级数、班额、轮次、
    时段和仿真设置；表内只并列事实，不产生名次或“推荐”字段。
    """
    if not isinstance(plan_ids, list) or not plan_ids:
        raise ValueError("请至少选择一个方案进行比较")
    if len(plan_ids) > 12:
        raise ValueError("一次最多比较 12 个方案")
    if len(set(plan_ids)) != len(plan_ids):
        raise ValueError("比较列表中有重复方案")
    overrides_by_plan = overrides_by_plan or {}
    if not isinstance(overrides_by_plan, dict):
        raise ValueError("方案调整必须按方案编号给出")
    known = {record.get("plan_id") for record in data.get("plans", [])}
    unknown = [plan_id for plan_id in plan_ids if plan_id not in known]
    if unknown:
        raise ValueError("未知排期方案：" + "、".join(map(str, unknown)))
    spec = SimulationSpec.from_value(simulation)
    rows = []
    for plan_id in plan_ids:
        result = assess_plan(data, plan_id, overrides_by_plan.get(plan_id), assumptions, spec)
        high = next(item for item in result["scenarios"] if item["position"] == "high")
        rows.append({
            "plan_id": plan_id,
            "design": result["design"],
            "resource_status": result["resource"]["status"],
            "resource_reasons": result["resource"]["reasons"],
            "cost": result["resource"]["cost"],
            "budget": result["resource"]["budget"],
            "identification": result["identification"],
            "estimands": high["estimands"],
            "feasibility": result["feasibility"],
            "recipe_sha256": result["recipe"]["sha256"],
        })
    return {"rows": rows, "notes": [
        "比较表按数据中的方案逐个复算，每行的班级数、班额、轮次和时段可以不同。",
        "资源、识别和统计能力分列显示；三者都通过才谈得上可行，但可行也不等于更好。",
        "表中没有排名或推荐；成本和可检出差异需要结合教研判断自行取舍。",
        "每行的复算指纹不同时，说明方案参数、假设区间或仿真设置不同，数字不能直接混用。",
    ], "scenario": "high", "simulation": spec.as_json()}
