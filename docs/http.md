# 本地接口

服务只用于本地教研复核，不连接真实人员目录或外部数据源。数据以 UTF-8 JSON 传输；未找到对象返回 404，无效业务输入返回 400，正文超出 8MB 返回 413。

- `GET /health` 返回就绪状态。
- `GET /api/context` 返回产品配置、默认版本、已导入版本与保存记录目录。
- `GET /api/data?revision=版本` 返回该版本完整原始数据；省略版本使用演示材料。
- `POST /api/datasets` 接收 `label` 和完整 `data`，返回新建或复用的 `revision`。
- `POST /api/explore` 接收可选 `revision` 和 `selection`，返回数据指纹、实际选择和领域描述。
- `POST /api/compare` 接收同样的 `revision`，逐个复算多个方案，返回比较表。
- `POST /api/notebooks/compare` 用同样的比较输入加 `title`，重新复算后冻结整张比较表。
- `POST /api/notebooks` 使用相同选择参数和 `title`，服务端重新计算后保存，不能由客户端伪造结果。
- `GET /api/notebooks` 返回材料目录，`GET /api/notebooks/编号` 返回冻结描述、选择条件和所用数据。
- `GET /api/notebooks/编号/verify` 按冻结输入重新复算，返回 `matches` 与两个指纹。

## 选择条件

排期资源账使用 `{"mode":"schedule","plan":"balanced"}`。设计评估使用：

```json
{
  "mode": "design",
  "plan": "balanced",
  "overrides": {
    "classes": 10,
    "class_size": 14,
    "rounds": 2,
    "sessions": 8,
    "arm_counts": {"A": 5, "B": 5}
  },
  "assumptions": {
    "class_sd": [1, 3],
    "absence": [0.05, 0.2]
  },
  "simulation": {"replications": 240, "seed": 20260914}
}
```

- `overrides` 只改班级数、每班人数、轮次、时段数和班级安排，不回写源数据。改班级数时
  必须给 `class_allocation` 或 `arm_counts`（可加 `order: alternating|blocked`）。
- `assumptions` 可覆盖 `class_sd`、`student_sd`、`residual_sd`、`absence`、`period_sd`
  的区间；复测相关由方差分量推导，不能单独覆盖。
- `simulation` 可设 `replications`、`seed`、`alpha`、`target_power`。

## 比较

`POST /api/compare` 的 `selection` 使用 `plans`（方案编号列表，省略则比较数据中的全部
方案）、可选的 `overrides_by_plan`（只允许本次参与比较的方案）、`assumptions` 和
`simulation`。返回的每一行都带自己的三层结论、费用与 `recipe_sha256`；整张表另有自己的
`recipe.sha256`，汇总参与方案与数据指纹。表内没有排名、优先级或推荐字段。

## 领域描述

排期资源账包含 `summary`、`rows`、`series` 和 `notes`。设计评估包含 `design`、
`resource`、`schedule`、`identification`、`randomisation`、`scenarios`、
`correlation_check`、`feasibility`、`recipe` 和 `notes`；资源、识别与统计能力分开
呈现。学习进展另含作答历史。这里记录的是已有浏览交互，不限制新分析功能的内部结构。
增添业务能力时需保持现有原始记录和保存材料可读取。
