# 教学试验排期试算台

用于教研人员查看本地记录、选择业务范围、核对原始数据并保存复核材料。页面既能看现有方案的资源占用，也能对一个方案做设计评估：在可调整的班级数、每班人数和复测安排下，分别说明资源是否装得下、随机化发生在哪一层、哪些效应会被班级或时段混在一起，以及在假设区间内能检出多大的差异、这一判断有多不确定。所有示例都是本地生成的虚构记录，不含个人信息、真实研究数据或受限材料。

设计评估把资源超限、结构上无法区分和统计能力不足分开呈现，不做方案排名，也不预设某个方案更好。功效来自固定种子和固定重复次数的仿真，不套独立样本公式；相同输入会得到相同结果与同一复算指纹。反复调整时应保存每次想保留的方案，不把最乐观的一次当作承诺。方法细节见 `docs/data-contract.md`。

## 运行

需要 Python 3.11，运行不依赖第三方服务或第三方包。推荐使用锁定基础镜像的容器；也可以在项目根目录直接启动。

```bash
python3 -m design_power --host 127.0.0.1 --port 8080
```

浏览器打开 `http://127.0.0.1:8080`。状态默认保存在 `.state/research.sqlite3`，可用 `--state` 指定独立目录。页面提供范围选择、表格和图、原始记录查看、完整数据导入、保存和导出；切换到“设计评估”可调整方案参数与规划假设区间，查看三层结论并与排期表核对。保存的是数据版本与当时的完整输入，不会因后续导入变化，也不随重新调整而漂移。

```bash
docker build -t design_power:local .
docker run --rm -p 8080:8080 -v design_power-state:/app/.state design_power:local
```

## 检查

```bash
python3 -m unittest discover -s tests -v
```

回归覆盖数据口径、HTTP 交互、非法导入回滚、不可变版本与重启后的历史读取，以及设计评估的区间情景、随机化层级、资源与统计分层、复算指纹和保存后重算一致。测试使用临时状态目录，不改变演示数据。详见 `docs/data-contract.md`、`docs/http.md` 和 `docs/walkthrough.md`。如果扩展运行依赖，请同时在依赖清单和镜像中固定版本。

## 构建与浏览器回归

服务只使用 Python 标准库。下列固定版本工具用于构建和自动浏览器检查，不是运行服务的前置依赖。

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-build.txt -r requirements-test.txt
python3 -m playwright install --with-deps chromium
python3 -m build --wheel --no-isolation
python3 tests/browser_smoke.py
```

自动浏览器检查在随机本地端口启动真实应用，使用临时数据库验证范围切换、原始记录、保存复开、导出和幂等导入；结束后关闭浏览器和服务。它不要求预先启动服务器，也不依赖外部数据。`npm start` 是现有 Python 服务的启动别名，支持附加 `--host`、`--port` 参数；`npm run test:browser` 调用同一浏览器回归。npm 锁文件不引入 Node 运行包。

支持 CPython 3.11 系列，产品镜像仍由不可变 digest 固定至同一 Python 3.11.14 镜像；运行兼容性不依赖补丁版本专有行为。上述构建工具和测试工具均通过固定版本清单预装，正式启动和浏览器操作无须下载。
