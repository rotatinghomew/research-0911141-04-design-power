const state = {context: null, current: null, data: null, mode: 'schedule'};
const element = identifier => document.getElementById(identifier);

async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '请求失败');
  return value;
}

function message(value, error = false) {
  element('status').textContent = value;
  element('status').className = error ? 'error' : '';
}

async function guarded(action) {
  try { await action(); } catch (error) { message(error.message, true); }
}

function selection() {
  const value = {};
  for (const control of state.context.product.options) value[control[0]] = element('select-' + control[0]).value;
  value.mode = state.mode;
  if (state.mode === 'design') {
    value.plan = element('design-plan').value;
    value.overrides = designOverrides();
    value.assumptions = assumptionOverrides();
    value.simulation = simulationSettings();
  }
  return {revision: element('revision').value, selection: value};
}

function numberOrNull(input) {
  const text = input.value.trim();
  if (!text) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

function designOverrides() {
  const overrides = {};
  for (const name of ['classes', 'class_size', 'rounds', 'sessions']) {
    const value = numberOrNull(element('override-' + name));
    if (value !== null) overrides[name] = Math.round(value);
  }
  const armA = numberOrNull(element('override-arm-a'));
  const armB = numberOrNull(element('override-arm-b'));
  if (armA !== null && armB !== null) overrides.arm_counts = {A: Math.round(armA), B: Math.round(armB)};
  return overrides;
}

function assumptionOverrides() {
  const overrides = {};
  for (const name of ['class_sd', 'student_sd', 'residual_sd', 'absence', 'period_sd']) {
    const low = numberOrNull(element('assume-' + name + '-low'));
    const high = numberOrNull(element('assume-' + name + '-high'));
    if (low !== null || high !== null) overrides[name] = [low === null ? 0 : low, high === null ? low : high];
  }
  return overrides;
}

function simulationSettings() {
  const settings = {};
  const replications = numberOrNull(element('sim-replications'));
  const seed = numberOrNull(element('sim-seed'));
  if (replications !== null) settings.replications = Math.round(replications);
  if (seed !== null) settings.seed = Math.round(seed);
  return settings;
}

function renderControls() {
  const revision = element('revision').value || state.context.default_revision;
  element('revision').replaceChildren();
  for (const dataset of state.context.datasets) {
    const option = document.createElement('option');
    option.value = dataset.revision;
    option.textContent = dataset.label + ' · ' + dataset.revision.slice(0, 9);
    element('revision').append(option);
  }
  element('revision').value = revision;
  if (!element('revision').value) element('revision').value = state.context.default_revision;
  if (element('controls').children.length) return;
  for (const [key, labelText, choices] of state.context.product.options) {
    const label = document.createElement('label');
    label.textContent = labelText;
    const select = document.createElement('select');
    select.id = 'select-' + key;
    for (const choice of choices) {
      const option = document.createElement('option');
      option.value = choice;
      option.textContent = choice;
      select.append(option);
    }
    select.value = state.context.product.selection[key];
    label.append(select);
    element('controls').append(label);
  }
  const modeLabel = document.createElement('label');
  modeLabel.textContent = '查看方式';
  const mode = document.createElement('select');
  mode.id = 'mode';
  for (const [value, text] of [['schedule', '排期资源账'], ['design', '设计评估']]) {
    const option = document.createElement('option');
    option.value = value; option.textContent = text; mode.append(option);
  }
  mode.onchange = () => { state.mode = mode.value; element('design-controls').hidden = state.mode !== 'design'; };
  modeLabel.append(mode); element('controls').append(modeLabel);
  state.mode = mode.value;
}

function renderDesignControls(data) {
  const container = element('design-controls');
  if (container.children.length && container.dataset.plans === data.plans.map(p => p.plan_id).join(',')) return;
  container.dataset.plans = data.plans.map(p => p.plan_id).join(',');
  container.replaceChildren();
  container.hidden = state.mode !== 'design';
  const planLabel = document.createElement('label');
  planLabel.textContent = '排期方案';
  const plan = document.createElement('select');
  plan.id = 'design-plan';
  for (const record of data.plans) {
    const option = document.createElement('option');
    option.value = record.plan_id;
    option.textContent = record.plan_id + ' · ' + record.classes + '班×' + record.class_size + '人×' + record.rounds + '轮';
    plan.append(option);
  }
  planLabel.append(plan); container.append(planLabel);
  const fields = [['classes', '班级数', '留空沿用方案'], ['class_size', '每班人数', '留空沿用方案'],
                  ['rounds', '测量轮次', '留空沿用方案'], ['sessions', '使用时段数', '留空沿用方案']];
  for (const [name, label, placeholder] of fields) {
    const field = document.createElement('label');
    field.textContent = label;
    const input = document.createElement('input');
    input.id = 'override-' + name; input.type = 'number'; input.min = '1'; input.placeholder = placeholder;
    field.append(input); container.append(field);
  }
  for (const [name, label] of [['arm-a', 'A 班数（与 B 一起填）'], ['arm-b', 'B 班数（与 A 一起填）']]) {
    const field = document.createElement('label');
    field.textContent = label;
    const input = document.createElement('input');
    input.id = 'override-' + name; input.type = 'number'; input.min = '0'; input.placeholder = '留空沿用方案';
    field.append(input); container.append(field);
  }
  const assumptions = data.historical || {};
  const ranges = assumptions.variance_ranges || {};
  const rows = [
    ['class_sd', '班级层标准差', (ranges.class || [0, 0]).map(Math.sqrt)],
    ['student_sd', '学生稳定标准差', (ranges.student || [0, 0]).map(Math.sqrt)],
    ['residual_sd', '每轮残差标准差', (ranges.residual || [0, 0]).map(Math.sqrt)],
    ['period_sd', '时段/批次标准差', (ranges.period || [0, 0]).map(Math.sqrt)],
    ['absence', '缺席率', assumptions.absence_ranges || [0, 0]],
  ];
  const heading = document.createElement('div');
  heading.className = 'field-group'; heading.textContent = '规划假设区间（留空使用数据中的历史区间）';
  container.append(heading);
  for (const [name, label, pair] of rows) {
    const field = document.createElement('label');
    field.textContent = label + '（' + pair.map(value => Number(value).toFixed(2)).join(' – ') + '）';
    const line = document.createElement('div'); line.className = 'pair';
    for (const side of ['low', 'high']) {
      const input = document.createElement('input');
      input.id = 'assume-' + name + '-' + side; input.type = 'number'; input.step = 'any'; input.min = '0';
      input.placeholder = side === 'low' ? '低端' : '高端';
      line.append(input);
    }
    field.append(line); container.append(field);
  }
  const simHeading = document.createElement('div');
  simHeading.className = 'field-group'; simHeading.textContent = '仿真设置（相同设置才能复算）';
  container.append(simHeading);
  for (const [id, label, value] of [['sim-replications', '重复次数', '240'], ['sim-seed', '随机种子', '20260914']]) {
    const field = document.createElement('label');
    field.textContent = label;
    const input = document.createElement('input');
    input.id = id; input.type = 'number'; input.placeholder = value;
    if (id === 'sim-replications') { input.min = '40'; input.max = '4000'; }
    else input.min = '0';
    field.append(input); container.append(field);
  }
}

function displayValue(value) {
  if (value === null || value === undefined) return '未观测';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(3);
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function drawChart(series) {
  const canvas = element('chart');
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  const values = series.filter(point => typeof point.y === 'number' && Number.isFinite(point.y));
  context.font = '12px sans-serif';
  if (!values.length) { context.fillText('该范围没有可定量的描述值', 30, 50); return; }
  const low = Math.min(0, ...values.map(point => point.y));
  const high = Math.max(1, ...values.map(point => point.y));
  const scale = value => 250 - (value - low) / (high - low || 1) * 210;
  context.strokeStyle = '#90a5ad';
  context.beginPath(); context.moveTo(45, 20); context.lineTo(45, 250); context.lineTo(940, 250); context.stroke();
  context.fillStyle = '#0a7168';
  const scatter = values.every(point => typeof point.x === 'number');
  const minimumX = scatter ? Math.min(...values.map(point => point.x)) : 0;
  const maximumX = scatter ? Math.max(...values.map(point => point.x)) : 1;
  values.forEach((point, index) => {
    const position = scatter ? 60 + (point.x - minimumX) / (maximumX - minimumX || 1) * 850 : 55 + index * 875 / values.length;
    if (scatter) {
      context.beginPath(); context.arc(position, scale(point.y), 3.5, 0, Math.PI * 2); context.fill();
    } else {
      context.fillRect(position, Math.min(scale(point.y), scale(0)), Math.max(3, 800 / values.length), Math.max(1, Math.abs(scale(point.y) - scale(0))));
    }
    if (values.length < 12) context.fillText(point.label, position, 275);
  });
  context.fillStyle = '#29434e';
  context.fillText(high.toFixed(2), 0, 45); context.fillText(low.toFixed(2), 0, 250);
  context.fillText('按表中顺序展示描述值；缺测不绘制为零', 50, 20);
}

function renderSchedule(view) {
  element('design-view').hidden = true;
  element('compare-view').hidden = true;
  element('ledger').hidden = false;
  element('ledger-heading').textContent = '现有方案的资源占用';
  element('summary').replaceChildren();
  for (const [label, value] of Object.entries(view.summary)) {
    const card = document.createElement('article');
    const title = document.createElement('span'); title.textContent = label;
    const amount = document.createElement('strong'); amount.textContent = displayValue(value);
    card.append(title, amount); element('summary').append(card);
  }
  element('notes').replaceChildren();
  for (const note of view.notes) {
    const paragraph = document.createElement('p'); paragraph.textContent = note; element('notes').append(paragraph);
  }
  fillTable(element('columns'), element('rows'), view.rows, row => {
    element('details').textContent = JSON.stringify(row, null, 2);
  });
  drawChart(view.series);
}

function fillTable(head, body, rows, onRow) {
  const fields = rows.length ? Object.keys(rows[0]) : [];
  const header = document.createElement('tr');
  for (const field of fields) { const cell = document.createElement('th'); cell.textContent = field; header.append(cell); }
  head.replaceChildren(header); body.replaceChildren();
  for (const row of rows) {
    const line = document.createElement('tr'); line.tabIndex = 0;
    for (const field of fields) { const cell = document.createElement('td'); cell.textContent = displayValue(row[field]); line.append(cell); }
    line.onclick = () => onRow(row);
    line.onkeydown = event => { if (event.key === 'Enter') line.click(); };
    body.append(line);
  }
}

const STATUS_TEXT = {
  reached: '达到目标', point_only: '点估计达到、区间未达到', uncertain: '区间跨过目标，精度不足',
  insufficient: '能力不足', unstable: '存在无法估计的重复', unidentifiable: '结构上无法区分',
  identified: '可区分', feasible: '通过', blocked: '有未通过项', infeasible: '资源超限'
};

function renderDesign(result) {
  const view = result.view;
  state.current = result;
  element('compare-view').hidden = true;
  element('design-view').hidden = false;
  element('ledger').hidden = true;
  element('design-summary').replaceChildren();
  const cards = [
    ['方案', view.design.plan_id],
    ['班级数×班额', view.design.classes + ' × ' + view.design.class_size],
    ['测量轮次', view.design.rounds + ' · 时段 ' + view.design.sessions],
    ['独立班级', JSON.stringify(view.identification[0] ? view.identification[0].independent_units : {})],
  ];
  for (const [label, value] of cards) {
    const card = document.createElement('article');
    const title = document.createElement('span'); title.textContent = label;
    const amount = document.createElement('strong'); amount.textContent = value;
    card.append(title, amount); element('design-summary').append(card);
  }
  element('design-notes').replaceChildren();
  for (const note of view.notes) {
    const paragraph = document.createElement('p'); paragraph.textContent = note; element('design-notes').append(paragraph);
  }

  const verdict = element('verdict');
  verdict.replaceChildren();
  const banner = document.createElement('p');
  banner.className = 'verdict ' + view.feasibility.verdict;
  banner.textContent = view.feasibility.verdict === 'feasible'
    ? '三层都通过：资源装得下、效应可区分、统计能力达到目标。这只说明可行，不说明优于其他方案。'
    : '至少有一层未通过；下方分层列出，而不是合并成一个“不可行”。';
  verdict.append(banner);
  const layers = document.createElement('div');
  layers.className = 'layers';
  for (const [key, label] of [['resource', '资源'], ['identification', '识别'], ['statistical_power', '统计能力']]) {
    const item = document.createElement('article');
    item.className = view.feasibility.passed[key] ? 'pass' : 'fail';
    const title = document.createElement('span'); title.textContent = label;
    const value = document.createElement('strong'); value.textContent = view.feasibility.passed[key] ? '通过' : '未通过';
    item.append(title, value); layers.append(item);
  }
  verdict.append(layers);
  element('feasibility').replaceChildren();
  for (const problem of view.feasibility.problems) {
    const paragraph = document.createElement('p');
    paragraph.className = 'problem';
    paragraph.textContent = ({resource: '资源层：', identification: '识别层：', statistical_power: '统计能力层：'})[problem.layer] + problem.detail;
    element('feasibility').append(paragraph);
  }
  if (!view.feasibility.problems.length) {
    const paragraph = document.createElement('p');
    paragraph.textContent = '没有未通过项。请结合成本与最小可检出差异判断是否值得做。';
    element('feasibility').append(paragraph);
  }

  fillTable(element('random-head'), element('random-rows'), view.randomisation.map(item => ({
    关注差异: item.estimand, 随机化层: item.randomised_at, 独立单位: JSON.stringify(item.independent_units),
    已扣除或区组: item.blocked_or_removed.join('、'), 会混在一起的效应: item.confounded_with.join('、'), 说明: item.note,
  })), row => { element('details').textContent = JSON.stringify(row, null, 2); });

  const high = view.scenarios.find(item => item.position === 'high');
  element('power-head').replaceChildren(headerRow(['情景', '关注差异', '随机化层', '目标差异', '功效', '蒙特卡洛区间', '可估计重复', '最小可检出差异', '状态']));
  element('power-rows').replaceChildren();
  for (const scenario of view.scenarios) {
    for (const entry of scenario.estimands) {
      const line = document.createElement('tr');
      for (const value of [
        scenario.position === 'high' ? '高端（判断用）' : '低端（乐观边界）',
        entry.estimand, entry.unit === 'class' ? '班级' : '学生',
        entry.target_difference,
        entry.power.toFixed(3),
        entry.power_interval[0].toFixed(3) + ' – ' + entry.power_interval[1].toFixed(3),
        entry.estimable_replications + ' / ' + entry.replications,
        entry.status === 'unidentifiable' ? '不适用'
          : (entry.mdd.lower_confidence === null ? (entry.mdd.reached ? '未达目标' : '无法达到')
            : entry.mdd.lower_confidence.toFixed(2)),
        STATUS_TEXT[entry.status] || entry.status,
      ]) { const cell = document.createElement('td'); cell.textContent = String(value); line.append(cell); }
      line.onclick = () => { element('details').textContent = JSON.stringify({design: view.design, scenario: scenario.assumptions, entry}, null, 2); };
      element('power-rows').append(line);
    }
  }
  element('scenario-notes').replaceChildren();
  const note = document.createElement('p');
  note.textContent = '低端只作乐观边界；检出能力以高端为准。最小可检出差异一列给出区间下限所需差异，'
    + '“无法达到”表示在当前复算内即使差异很大也没能达到目标功效。';
  element('scenario-notes').append(note);
  const correlation = document.createElement('p');
  correlation.textContent = '复测相关核对：' + view.correlation_check.note
    + '（历史 ' + displayValue(view.correlation_check.reported) + '，推导 ' + displayValue(view.correlation_check.implied) + '）';
  element('scenario-notes').append(correlation);

  element('schedule-summary').replaceChildren();
  for (const [label, value] of Object.entries(view.schedule.summary)) {
    const card = document.createElement('article');
    const title = document.createElement('span'); title.textContent = label;
    const amount = document.createElement('strong'); amount.textContent = displayValue(value);
    card.append(title, amount); element('schedule-summary').append(card);
  }
  const scheduleRows = view.schedule.checks.map(check => ({
    检查项: check.name, 需要: check.required, 可用: check.available,
    通过: check.ok, 决定资源判定: check.blocking,
  }));
  fillTable(element('schedule-head'), element('schedule-rows'), scheduleRows, row => {
    element('details').textContent = JSON.stringify(row, null, 2);
  });
  element('recipe').textContent = JSON.stringify({recipe: view.recipe, candidate: view.candidate}, null, 2);
  element('details').textContent = JSON.stringify({revision: result.revision, selection: result.selection,
    feasibility: view.feasibility, recipe_sha256: view.recipe.sha256}, null, 2);
  message('设计评估完成 · 复算指纹 ' + view.recipe.sha256.slice(0, 12) + ' · 数据 ' + result.revision.slice(0, 12));
}

function headerRow(values) {
  const row = document.createElement('tr');
  for (const value of values) { const cell = document.createElement('th'); cell.textContent = value; row.append(cell); }
  return row;
}

function renderComparison(result) {
  const view = result.view;
  state.current = result;
  element('design-view').hidden = true;
  element('compare-view').hidden = false;
  element('ledger').hidden = true;
  element('compare-notes').replaceChildren();
  for (const note of view.notes) {
    const paragraph = document.createElement('p'); paragraph.textContent = note; element('compare-notes').append(paragraph);
  }
  const head = headerRow(['方案', '资源', '费用/预算', '识别', '关注差异（高端）', '功效', '蒙特卡洛区间', '最小可检出差异', '三层结论', '复算指纹']);
  element('compare-head').replaceChildren(head);
  element('compare-rows').replaceChildren();
  for (const row of view.rows) {
    const power = row.estimands.map(item => item.estimand + ' ' + item.power.toFixed(2)
      + ' [' + item.power_interval[0].toFixed(2) + ', ' + item.power_interval[1].toFixed(2) + ']').join('；');
    const mdd = row.estimands.map(item => item.estimand + (item.status === 'unidentifiable' ? ' 不适用'
      : (item.mdd.lower_confidence === null ? ' 未达' : ' ' + item.mdd.lower_confidence.toFixed(2)))).join('；');
    const cells = [
      row.plan_id,
      (STATUS_TEXT[row.resource_status] || row.resource_status)
        + (row.resource_reasons.length ? '：' + row.resource_reasons.join('；') : ''),
      displayValue(row.cost) + ' / ' + displayValue(row.budget),
      row.identification.every(item => item.status === 'identified') ? '全部可区分' : row.identification.filter(item => item.status !== 'identified').map(item => item.estimand).join('、') + ' 无法区分',
      row.estimands.map(item => item.estimand).join('、'),
      row.estimands.map(item => item.power.toFixed(2)).join('；'),
      row.estimands.map(item => item.power_interval[0].toFixed(2) + '–' + item.power_interval[1].toFixed(2)).join('；'),
      mdd,
      row.feasibility.verdict === 'feasible' ? '三层通过' : '未通过：' + [...new Set(row.feasibility.problems.map(problem => ({resource: '资源', identification: '识别', statistical_power: '统计'})[problem.layer]))].join('、'),
      row.recipe_sha256.slice(0, 10),
    ];
    const line = document.createElement('tr');
    for (const value of cells) { const cell = document.createElement('td'); cell.textContent = value; line.append(cell); }
    line.onclick = () => {
      const schedule = view.schedule[row.plan_id];
      element('details').textContent = JSON.stringify({row, schedule: schedule ? schedule.summary : null, checks: schedule ? schedule.checks : null}, null, 2);
      element('compare-details').textContent = row.resource_reasons.length
        ? '资源未通过原因：' + row.resource_reasons.join('；')
        : '资源通过；是否实施仍需结合成本与可检出差异判断。';
    };
    element('compare-rows').append(line);
  }
  element('compare-details').textContent = '点击一行查看该方案在排期表中的资源核对。比较表没有默认胜出者。';
  element('details').textContent = JSON.stringify({revision: result.revision, selection: result.selection,
    compare: view.rows.map(row => ({plan_id: row.plan_id, feasibility: row.feasibility, sha: row.recipe_sha256})),
    recipe_sha256: view.recipe ? view.recipe.sha256 : null}, null, 2);
  message('已复算 ' + view.rows.length + ' 个方案 · 数据 ' + result.revision.slice(0, 12));
}

async function refresh() {
  state.context = await request('/api/context');
  renderControls();
  element('archive').replaceChildren();
  for (const notebook of state.context.notebooks) {
    const button = document.createElement('button'); button.textContent = notebook.title + ' #' + notebook.id;
    button.onclick = () => guarded(async () => {
      const saved = await request('/api/notebooks/' + notebook.id);
      element('revision').value = saved.revision;
      for (const [key, value] of Object.entries(saved.selection)) {
        const control = element('select-' + key); if (control && typeof value === 'string') control.value = value;
      }
      state.mode = saved.selection.mode || 'schedule';
      // “比较”不是一个独立的查看方式选项，回到界面时按设计评估显示控件。
      element('mode').value = state.mode === 'compare' ? 'design' : state.mode;
      element('design-controls').hidden = !['design', 'compare'].includes(state.mode);
      if (state.mode === 'compare') {
        renderComparison(saved.result);
        element('details').textContent = JSON.stringify(saved.result, null, 2);
        element('status').textContent = '已读取保存的比较记录，不随新数据变化';
      } else if (state.mode === 'design') {
        element('design-plan').value = saved.selection.plan;
        await loadData();
        renderDesign(saved.result);
        element('details').textContent = JSON.stringify(saved.result, null, 2);
        element('status').textContent = '已读取保存的设计评估，不随新数据变化';
      } else {
        const savedView = saved.result.view;
        element('design-view').hidden = true; element('compare-view').hidden = true;
        element('ledger').hidden = false;
        element('ledger-heading').textContent = '现有方案的资源占用';
        element('summary').replaceChildren();
        for (const [label, value] of Object.entries(savedView.summary)) {
          const card = document.createElement('article');
          const title = document.createElement('span'); title.textContent = label;
          const amount = document.createElement('strong'); amount.textContent = displayValue(value);
          card.append(title, amount); element('summary').append(card);
        }
        fillTable(element('columns'), element('rows'), savedView.rows, row => { element('details').textContent = JSON.stringify(row, null, 2); });
        drawChart(savedView.series);
        element('details').textContent = JSON.stringify(saved.result, null, 2);
        state.current = saved.result;
        element('status').textContent = '已读取保存的描述，不随新数据变化';
      }
    });
    element('archive').append(button);
  }
}

async function loadData() {
  state.data = await request('/api/data?revision=' + encodeURIComponent(element('revision').value));
  renderDesignControls(state.data);
}

element('inspect').onclick = () => guarded(async () => {
  await loadData();
  const result = await request('/api/explore', selection());
  if (state.mode === 'design') renderDesign(result); else {
    renderSchedule(result.view);
    state.current = result;
    element('details').textContent = JSON.stringify({revision: result.revision, selection: result.selection}, null, 2);
    message('已更新描述 · 数据 ' + result.revision.slice(0, 12));
  }
});
element('rerun').onclick = () => guarded(async () => {
  if (!state.current || !state.current.view || !state.current.view.recipe) throw new Error('请先完成一次设计评估');
  const response = element('rerun-result');
  response.textContent = '复算中…';
  const previous = state.current.view.recipe.sha256;
  const oldSimulation = state.current.selection.simulation || {};
  const replicationValue = numberOrNull(element('rerun-replications'));
  const seedValue = numberOrNull(element('rerun-seed'));
  const selectionValue = {...state.current.selection,
    simulation: {...oldSimulation,
      replications: replicationValue === null ? state.current.view.recipe.simulation.replications : Math.round(replicationValue),
      seed: seedValue === null ? state.current.view.recipe.simulation.seed : Math.round(seedValue)}};
  const result = await request('/api/explore', {revision: state.current.revision, selection: selectionValue});
  const same = result.view.recipe.sha256 === previous;
  renderDesign(result);
  element('rerun-result').textContent = same
    ? '与上次指纹相同：' + result.view.recipe.sha256.slice(0, 12) + '，相同设置复算出同一结果。'
    : '新指纹 ' + result.view.recipe.sha256.slice(0, 12) + '；设置已改变，数字不能再与上一次混用。';
  message('已按指定种子复算 · 指纹 ' + result.view.recipe.sha256.slice(0, 12));
});
element('assess').onclick = () => guarded(async () => {
  state.mode = 'design';
  element('mode').value = 'design';
  element('design-controls').hidden = false;
  await loadData();
  const result = await request('/api/explore', selection());
  renderDesign(result);
});
element('compare').onclick = () => guarded(async () => {
  state.mode = 'design';
  element('mode').value = 'design';
  element('design-controls').hidden = false;
  await loadData();
  const body = selection();
  const overrides = designOverrides();
  const payload = {revision: body.revision, selection: {
    plans: state.data.plans.map(record => record.plan_id),
    assumptions: assumptionOverrides(),
    overrides_by_plan: Object.keys(overrides).length ? {[element('design-plan').value]: overrides} : {},
    simulation: simulationSettings(),
  }};
  state.comparison = payload;
  const result = await request('/api/compare', payload);
  renderComparison(result);
});
element('save-compare').onclick = () => guarded(async () => {
  if (!state.comparison) throw new Error('请先复算一次比较');
  const saved = await request('/api/notebooks/compare', {...state.comparison,
    title: element('compare-title').value});
  element('compare-save-result').textContent = '已保存比较记录 #' + saved.id
    + '；指纹 ' + saved.result.view.recipe.sha256.slice(0, 12) + '。';
  await refresh();
  state.current = saved.result;
  element('details').textContent = JSON.stringify(saved.result, null, 2);
  message('已保存比较记录 #' + saved.id);
});
element('save').onclick = () => guarded(async () => {
  let saved;
  if (state.mode === 'compare') {
    if (!state.comparison) throw new Error('请先复算一次比较，再保存');
    saved = await request('/api/notebooks/compare', {...state.comparison, title: element('title').value});
  } else {
    saved = await request('/api/notebooks', {...selection(), title: element('title').value});
  }
  await refresh();
  if (saved.selection.mode === 'compare') {
    renderComparison(saved.result);
  } else if (saved.selection.mode === 'design') {
    renderDesign(saved.result);
  } else {
    renderSchedule(saved.result.view);
  }
  state.current = saved.result;
  element('details').textContent = JSON.stringify(saved.result, null, 2);
  message('已保存记录 #' + saved.id + '，可用 /api/notebooks/' + saved.id + '/verify 复算核对');
});
element('raw').onclick = () => guarded(async () => {
  const revision = state.current ? state.current.revision : element('revision').value;
  element('details').textContent = JSON.stringify(await request('/api/data?revision=' + encodeURIComponent(revision)), null, 2);
});
element('upload').onchange = () => guarded(async () => {
  const file = element('upload').files[0]; if (!file) return;
  const data = JSON.parse(await file.text());
  const uploaded = await request('/api/datasets', {label: file.name, data});
  await refresh(); element('revision').value = uploaded.revision;
  await loadData();
  const result = await request('/api/explore', selection());
  if (state.mode === 'design') renderDesign(result); else { renderSchedule(result.view); state.current = result; }
  message('已导入独立数据版本');
});
element('export').onclick = () => guarded(async () => {
  if (!state.current) throw new Error('请先查看一个范围');
  const data = await request('/api/data?revision=' + encodeURIComponent(state.current.revision));
  const blob = new Blob([JSON.stringify({description: state.current, data}, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob); const link = document.createElement('a');
  link.href = url; link.download = 'analysis-' + state.current.revision.slice(0, 12) + '.json'; link.click();
  URL.revokeObjectURL(url);
});
guarded(async () => {
  await refresh();
  await loadData();
  const result = await request('/api/explore', selection());
  renderSchedule(result.view);
  state.current = result;
  message('已更新描述 · 数据 ' + result.revision.slice(0, 12));
});
