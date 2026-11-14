from pathlib import Path
import json

from .domain import explore, validate
from .design import (MODEL_VERSION, assess_plan, compare_plans, schedule_ledger,
                     build_design, validate_design_tables)
from .storage import Store, fingerprint


DESIGN_MODE = "design"


def _normalise(selection, mode=None):
    """把选择条件规整为可复算的完整输入，不填入任何未声明的方案假设。"""
    if not isinstance(selection, dict):
        raise ValueError("选择条件必须是对象")
    mode = mode or selection.get("mode", "schedule")
    if mode not in {"schedule", "design"}:
        raise ValueError("未知的查看模式")
    result = dict(selection)
    result["mode"] = mode
    if mode == DESIGN_MODE:
        if not isinstance(result.get("plan"), str) or not result["plan"]:
            raise ValueError("设计评估必须指定一个排期方案")
        result.setdefault("overrides", {})
        result.setdefault("assumptions", {})
        result.setdefault("simulation", {})
        for name in ["overrides", "assumptions", "simulation"]:
            if not isinstance(result[name], dict):
                raise ValueError("设计评估的 " + name + " 必须是对象")
    return result


def _normalise_comparison(selection):
    if not isinstance(selection, dict):
        raise ValueError("选择条件必须是对象")
    result = dict(selection)
    result["mode"] = "compare"
    result.setdefault("assumptions", {})
    result.setdefault("simulation", {})
    result.setdefault("overrides_by_plan", {})
    for name in ["assumptions", "simulation", "overrides_by_plan"]:
        if not isinstance(result[name], dict):
            raise ValueError("比较的 " + name + " 必须是对象")
    return result


class Workbench:
    def __init__(self, root, state):
        self.root = Path(root)
        self.product = json.loads((self.root / "product.json").read_text(encoding="utf-8"))
        self.store = Store(Path(state) / "research.sqlite3")
        initial = json.loads((self.root / "fixtures" / "data.json").read_text(encoding="utf-8"))
        validate(initial)
        validate_design_tables(initial)
        self.default_revision = self.store.put_dataset("演示材料", initial)

    def context(self):
        return {"product": self.product, "default_revision": self.default_revision,
                "datasets": self.store.datasets(), "notebooks": self.store.notebooks()}

    def upload(self, body):
        data = body["data"]
        try:
            validate(data)
            validate_design_tables(data)
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("字段结构不完整或类型不符") from error
        return {"revision": self.store.put_dataset(body.get("label", ""), data)}

    def _data(self, body):
        revision = body.get("revision", self.default_revision)
        selection = body.get("selection", self.product["selection"])
        return revision, _normalise(selection), self.store.dataset(revision)

    def inspect(self, body):
        revision, selection, data = self._data(body)
        if selection["mode"] == DESIGN_MODE:
            view = self._assess(data, selection)
        else:
            view = explore(data, selection)
        return {"revision": revision, "selection": selection, "data_sha256": fingerprint(data), "view": view}

    def _assess(self, data, selection):
        view = assess_plan(data, selection["plan"], selection.get("overrides"),
                           selection.get("assumptions"), selection.get("simulation"))
        view["revision_sha256"] = fingerprint(data)
        view["candidate"] = {
            "plan": selection["plan"],
            "overrides": selection.get("overrides", {}),
            "assumptions": selection.get("assumptions", {}),
            "simulation": selection.get("simulation", {}),
        }
        return view

    def _comparison(self, data, selection):
        plans = selection.get("plans")
        if plans is None:
            plans = [record["plan_id"] for record in data["plans"]]
        if not isinstance(plans, list) or not plans:
            raise ValueError("请至少选择一个方案进行比较")
        overrides_by_plan = selection.get("overrides_by_plan") or {}
        if set(overrides_by_plan) - set(plans):
            raise ValueError("方案调整引用了未参与比较的方案")
        view = compare_plans(data, plans, overrides_by_plan, selection.get("assumptions"),
                             selection.get("simulation"))
        view["revision_sha256"] = fingerprint(data)
        view["candidate"] = {"plans": plans, "overrides_by_plan": overrides_by_plan,
                             "assumptions": selection.get("assumptions", {}),
                             "simulation": selection.get("simulation", {})}
        view["schedule"] = {
            plan_id: schedule_ledger(data, build_design(
                data, plan_id, overrides_by_plan.get(plan_id))) for plan_id in plans
        }
        recipe = {
            "model_version": MODEL_VERSION,
            "candidate": view["candidate"],
            "rows": [{"plan_id": row["plan_id"], "recipe_sha256": row["recipe_sha256"]}
                     for row in view["rows"]],
            "data_sha256": fingerprint(data),
        }
        recipe["sha256"] = fingerprint(recipe)
        view["recipe"] = recipe
        return view, plans

    def compare(self, body):
        revision = body.get("revision", self.default_revision)
        selection = _normalise_comparison(body.get("selection", {}))
        data = self.store.dataset(revision)
        view, plans = self._comparison(data, selection)
        return {"revision": revision, "selection": {**selection, "plans": plans},
                "data_sha256": fingerprint(data), "view": view}

    def save_comparison(self, body):
        revision = body.get("revision", self.default_revision)
        selection = _normalise_comparison(body.get("selection", {}))
        data = self.store.dataset(revision)
        view, plans = self._comparison(data, selection)
        result = {"revision": revision, "selection": {**selection, "plans": plans},
                  "data_sha256": fingerprint(data), "view": view}
        return self.store.save(body.get("title", ""), revision, result["selection"], result)

    def verify(self, identifier):
        """按保存记录冻结的输入重新复算，对照结果指纹，证明相同方案可复算。"""
        notebook = self.store.notebook(identifier)
        data = notebook["dataset"]
        selection = notebook["selection"]
        if selection.get("mode") == "compare":
            comparison = _normalise_comparison(selection)
            view, plans = self._comparison(data, comparison)
            recomputed = {"revision": notebook["revision"],
                          "selection": {**comparison, "plans": plans},
                          "data_sha256": fingerprint(data), "view": view}
            recipe = view["recipe"]["sha256"]
        else:
            revision, selection, _ = self._data(
                {"revision": notebook["revision"], "selection": selection})
            if selection["mode"] == DESIGN_MODE:
                recomputed = {"revision": revision, "selection": selection,
                              "data_sha256": fingerprint(data), "view": self._assess(data, selection)}
            else:
                recomputed = {"revision": revision, "selection": selection,
                              "data_sha256": fingerprint(data), "view": explore(data, selection)}
            recipe = (recomputed["view"].get("recipe", {}) or {}).get("sha256")
        stored = notebook["result"]
        return {"id": notebook["id"], "matches": fingerprint(recomputed) == fingerprint(stored),
                "stored_sha256": fingerprint(stored), "recomputed_sha256": fingerprint(recomputed),
                "recipe_sha256": recipe}

    def save(self, body):
        result = self.inspect(body)
        return self.store.save(body.get("title", ""), result["revision"], result["selection"], result)
