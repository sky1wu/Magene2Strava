"use strict";

const els = {
  dateLabel: document.querySelector("#dateLabel"),
  freshness: document.querySelector("#freshness"),
  refreshButton: document.querySelector("#refreshButton"),
  syncButton: document.querySelector("#syncButton"),
  syncRate: document.querySelector("#syncRate"),
  distanceMetric: document.querySelector("#distanceMetric"),
  rideCountMetric: document.querySelector("#rideCountMetric"),
  durationMetric: document.querySelector("#durationMetric"),
  elevationMetric: document.querySelector("#elevationMetric"),
  queuedMetric: document.querySelector("#queuedMetric"),
  errorMetric: document.querySelector("#errorMetric"),
  trendChart: document.querySelector("#trendChart"),
  weeklyTotal: document.querySelector("#weeklyTotal"),
  chartWrap: document.querySelector("#chartWrap"),
  chartTooltip: document.querySelector("#chartTooltip"),
  chartEmpty: document.querySelector("#chartEmpty"),
  routeDate: document.querySelector("#routeDate"),
  routeCanvas: document.querySelector("#routeCanvas"),
  routeKey: document.querySelector("#routeKey"),
  routeEmpty: document.querySelector("#routeEmpty"),
  latestDistance: document.querySelector("#latestDistance"),
  latestDuration: document.querySelector("#latestDuration"),
  latestPoints: document.querySelector("#latestPoints"),
  searchInput: document.querySelector("#searchInput"),
  statusFilter: document.querySelector("#statusFilter"),
  activityList: document.querySelector("#activityList"),
  loadMoreButton: document.querySelector("#loadMoreButton"),
  connectionList: document.querySelector("#connectionList"),
  authDialog: document.querySelector("#authDialog"),
  authDialogEyebrow: document.querySelector("#authDialogEyebrow"),
  authDialogTitle: document.querySelector("#authDialogTitle"),
  onelapAuthPanel: document.querySelector("#onelapAuthPanel"),
  onelapAccountInput: document.querySelector("#onelapAccountInput"),
  onelapPasswordInput: document.querySelector("#onelapPasswordInput"),
  onelapAuthButton: document.querySelector("#onelapAuthButton"),
  stravaAuthPanel: document.querySelector("#stravaAuthPanel"),
  stravaCookieInput: document.querySelector("#stravaCookieInput"),
  stravaCookieButton: document.querySelector("#stravaCookieButton"),
  stravaHarInput: document.querySelector("#stravaHarInput"),
  stravaHarButton: document.querySelector("#stravaHarButton"),
  stravaClientId: document.querySelector("#stravaClientId"),
  stravaClientSecret: document.querySelector("#stravaClientSecret"),
  stravaOauthButton: document.querySelector("#stravaOauthButton"),
  menuButton: document.querySelector("#menuButton"),
  sidebar: document.querySelector(".sidebar"),
  mobileOverlay: document.querySelector("#mobileOverlay"),
  toastRegion: document.querySelector("#toastRegion"),
  syncDialog: document.querySelector("#syncDialog"),
  uploadLimit: document.querySelector("#uploadLimit"),
  dialogQueued: document.querySelector("#dialogQueued"),
  dialogErrors: document.querySelector("#dialogErrors"),
  previewButton: document.querySelector("#previewButton"),
  confirmSyncButton: document.querySelector("#confirmSyncButton"),
  jobDialog: document.querySelector("#jobDialog"),
  jobEyebrow: document.querySelector("#jobEyebrow"),
  jobDialogTitle: document.querySelector("#jobDialogTitle"),
  jobSpinner: document.querySelector("#jobSpinner"),
  jobProgressBar: document.querySelector("#jobProgressBar"),
  jobLog: document.querySelector("#jobLog"),
  jobDoneButton: document.querySelector("#jobDoneButton"),
  jobCloseButton: document.querySelector("#jobCloseButton"),
};

const app = {
  dashboard: null,
  visibleActivities: 10,
  activeJob: null,
  jobTimer: null,
};

const svgNS = "http://www.w3.org/2000/svg";
const syncedStatuses = new Set(["uploaded", "duplicate", "matched"]);

function svgElement(name, attributes = {}) {
  const node = document.createElementNS(svgNS, name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  els.toastRegion.append(toast);
  window.setTimeout(() => toast.remove(), 4300);
}

async function readHar(input) {
  const file = input.files?.[0];
  if (!file) throw new Error("请先选择 HAR 文件");
  if (file.size > 12 * 1024 * 1024) throw new Error("HAR 文件不能超过 12 MB");
  try {
    const value = JSON.parse(await file.text());
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
    return value;
  } catch {
    throw new Error("HAR 文件不是有效的 JSON");
  }
}

async function runAuthAction(button, pendingLabel, action) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = pendingLabel;
  try {
    await action();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function openAuthDialog(provider) {
  const onelap = provider === "onelap";
  els.onelapAuthPanel.hidden = !onelap;
  els.stravaAuthPanel.hidden = onelap;
  els.authDialogEyebrow.textContent = onelap ? "顽鹿运动" : "Strava";
  els.authDialogTitle.textContent = onelap ? "配置自动登录" : "更新 Web 会话";
  els.authDialog.showModal();
}

async function loginOnelap() {
  await runAuthAction(els.onelapAuthButton, "正在登录…", async () => {
    const account = els.onelapAccountInput.value.trim();
    const password = els.onelapPasswordInput.value;
    if (!account || !password) throw new Error("请输入顽鹿账号和密码");
    await request("/api/auth/onelap/login", {
      method: "POST",
      body: JSON.stringify({ account, password }),
    });
    els.authDialog.close();
    els.onelapPasswordInput.value = "";
    await loadDashboard();
    showToast("顽鹿账号登录已配置");
  });
}

async function importStravaCookie() {
  await runAuthAction(els.stravaCookieButton, "正在验证…", async () => {
    await request("/api/auth/strava/web-session", {
      method: "POST",
      body: JSON.stringify({ cookie_header: els.stravaCookieInput.value }),
    });
    els.authDialog.close();
    els.stravaCookieInput.value = "";
    await loadDashboard();
    showToast("Strava Web 会话已保存");
  });
}

async function importStravaHar() {
  await runAuthAction(els.stravaHarButton, "正在提取…", async () => {
    const har = await readHar(els.stravaHarInput);
    await request("/api/auth/strava/har", {
      method: "POST",
      body: JSON.stringify({ har }),
    });
    els.authDialog.close();
    els.stravaHarInput.value = "";
    await loadDashboard();
    showToast("已从 HAR 保存 Strava Web 会话");
  });
}

async function startStravaOauth() {
  await runAuthAction(els.stravaOauthButton, "正在跳转…", async () => {
    const result = await request("/api/auth/strava/start", {
      method: "POST",
      body: JSON.stringify({
        client_id: els.stravaClientId.value.trim(),
        client_secret: els.stravaClientSecret.value.trim(),
        redirect_uri: `${window.location.origin}/api/auth/strava/callback`,
      }),
    });
    window.location.assign(result.authorization_url);
  });
}

function formatDuration(seconds, compact = false) {
  const total = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (compact) return hours ? `${hours}h ${String(minutes).padStart(2, "0")}m` : `${minutes}m`;
  return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分钟`;
}

function formatDate(epoch, options = {}) {
  if (!epoch) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    ...options,
  }).format(new Date(epoch * 1000));
}

function relativeTime(epoch) {
  if (!epoch) return "尚无同步记录";
  const delta = Math.max(0, Date.now() / 1000 - epoch);
  if (delta < 60) return "刚刚更新";
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前更新`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前更新`;
  return `${Math.floor(delta / 86400)} 天前更新`;
}

function statusView(status) {
  if (syncedStatuses.has(status)) return { key: "synced", label: status === "duplicate" ? "已存在" : "已同步" };
  if (status === "error") return { key: "error", label: "异常" };
  if (status === "local") return { key: "local", label: "仅本地" };
  if (status === "pending") return { key: "queued", label: "处理中" };
  return { key: "queued", label: "待同步" };
}

function animateNumber(element, target, formatter = (value) => String(Math.round(value))) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    element.textContent = formatter(target);
    return;
  }
  const started = performance.now();
  const duration = 620;
  const from = Number(String(element.textContent).replace(/[^0-9.-]/g, "")) || 0;
  function frame(now) {
    const progress = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    element.textContent = formatter(from + (target - from) * eased);
    if (progress < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

function renderSummary(data) {
  const summary = data.summary;
  animateNumber(els.syncRate, summary.sync_rate, (value) => `${value.toFixed(1)}%`);
  animateNumber(els.distanceMetric, summary.total_distance_km, (value) => value.toLocaleString("zh-CN", { maximumFractionDigits: 1 }));
  els.rideCountMetric.textContent = `共 ${summary.total_rides.toLocaleString("zh-CN")} 次活动`;
  els.durationMetric.textContent = formatDuration(summary.total_duration_s, true);
  animateNumber(els.elevationMetric, summary.total_elevation_m, (value) => Math.round(value).toLocaleString("zh-CN"));
  const queued = Math.max(0, summary.total_records - summary.synced_records);
  animateNumber(els.queuedMetric, queued);
  els.errorMetric.textContent = summary.error_records ? `${summary.error_records} 条记录需要处理` : "队列状态正常";
  els.errorMetric.style.color = summary.error_records ? "var(--danger)" : "";
  els.dialogQueued.textContent = queued.toLocaleString("zh-CN");
  els.dialogErrors.textContent = summary.error_records.toLocaleString("zh-CN");

  const freshnessEpoch = data.cache_updated_at || summary.last_sync_at;
  els.freshness.lastChild.textContent = relativeTime(freshnessEpoch);
  els.freshness.classList.toggle("is-stale", !data.cache_updated_at);
}

function weekStart(date) {
  const value = new Date(date);
  value.setHours(0, 0, 0, 0);
  const day = (value.getDay() + 6) % 7;
  value.setDate(value.getDate() - day);
  return value;
}

function weeklyData(activities) {
  const end = weekStart(new Date());
  const weeks = [];
  for (let index = 7; index >= 0; index -= 1) {
    const start = new Date(end);
    start.setDate(end.getDate() - index * 7);
    weeks.push({ start, distance: 0, rides: 0 });
  }
  const first = weeks[0].start.getTime();
  for (const activity of activities) {
    const stamp = activity.start_time * 1000;
    const bucket = Math.floor((weekStart(new Date(stamp)).getTime() - first) / (7 * 86400000));
    if (bucket >= 0 && bucket < weeks.length) {
      weeks[bucket].distance += (activity.distance_m || 0) / 1000;
      weeks[bucket].rides += 1;
    }
  }
  return weeks;
}

function niceStep(value) {
  const exponent = Math.floor(Math.log10(Math.max(value, 0.001)));
  const magnitude = 10 ** exponent;
  const fraction = value / magnitude;
  const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return niceFraction * magnitude;
}

function renderChart(activities) {
  const weeks = weeklyData(activities);
  const total = weeks.reduce((sum, item) => sum + item.distance, 0);
  els.weeklyTotal.textContent = `${total.toFixed(1)} km`;
  els.trendChart.replaceChildren();
  els.chartEmpty.hidden = total > 0;
  els.trendChart.hidden = total === 0;
  if (!total) return;

  const width = 820;
  const height = 250;
  const padding = { top: 36, right: 22, bottom: 38, left: 50 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const rawMaximum = Math.max(...weeks.map((item) => item.distance), 1);
  const tickStep = niceStep(rawMaximum / 4);
  const maxValue = Math.max(tickStep, Math.ceil(rawMaximum / tickStep) * tickStep);
  els.trendChart.setAttribute(
    "aria-label",
    `近八周骑行距离：${weeks.map((item) => `${item.distance.toFixed(1)} 公里`).join("，")}`,
  );

  const defs = svgElement("defs");
  const gradient = svgElement("linearGradient", { id: "areaGradient", x1: 0, y1: 0, x2: 0, y2: 1 });
  gradient.append(svgElement("stop", { offset: "0%", "stop-color": "#fc4c02", "stop-opacity": 0.16 }));
  gradient.append(svgElement("stop", { offset: "100%", "stop-color": "#fc4c02", "stop-opacity": 0 }));
  defs.append(gradient);
  els.trendChart.append(defs);

  for (let value = 0; value <= maxValue + tickStep / 2; value += tickStep) {
    const y = padding.top + plotHeight - (value / maxValue) * plotHeight;
    els.trendChart.append(svgElement("line", { class: "chart-grid", x1: padding.left, y1: y, x2: width - padding.right, y2: y }));
    const tick = svgElement("text", { class: "chart-axis-label", x: padding.left - 10, y: y + 3, "text-anchor": "end" });
    tick.textContent = value === 0 ? "0" : `${Number(value.toFixed(1))}`;
    els.trendChart.append(tick);
  }
  const unit = svgElement("text", { class: "chart-axis-label", x: padding.left - 10, y: padding.top - 15, "text-anchor": "end" });
  unit.textContent = "km";
  els.trendChart.append(unit);

  const points = weeks.map((item, index) => ({
    ...item,
    x: padding.left + (plotWidth / (weeks.length - 1)) * index,
    y: padding.top + plotHeight - (item.distance / maxValue) * plotHeight,
  }));
  const path = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const area = `${path} L${points.at(-1).x},${padding.top + plotHeight} L${points[0].x},${padding.top + plotHeight} Z`;
  els.trendChart.append(svgElement("path", { class: "chart-area", d: area }));
  els.trendChart.append(svgElement("path", { class: "chart-line", d: path }));

  points.forEach((point, index) => {
    const label = svgElement("text", { class: "chart-axis-label", x: point.x, y: height - 10, "text-anchor": "middle" });
    label.textContent = `${point.start.getMonth() + 1}/${point.start.getDate()}`;
    els.trendChart.append(label);
    const valueLabel = svgElement("text", { class: "chart-value-label", x: point.x, y: point.y - 12, "text-anchor": "middle" });
    valueLabel.textContent = point.distance < 10 ? point.distance.toFixed(1) : String(Math.round(point.distance));
    els.trendChart.append(valueLabel);
    const circle = svgElement("circle", { class: "chart-point", cx: point.x, cy: point.y, r: 4, tabindex: 0 });
    const reveal = (event) => showChartTooltip(event, point, index);
    circle.addEventListener("pointerenter", reveal);
    circle.addEventListener("focus", reveal);
    circle.addEventListener("pointerleave", hideChartTooltip);
    circle.addEventListener("blur", hideChartTooltip);
    els.trendChart.append(circle);
  });
}

function showChartTooltip(event, point) {
  const chartRect = els.trendChart.getBoundingClientRect();
  const wrapRect = els.chartWrap.getBoundingClientRect();
  const scaleX = chartRect.width / 820;
  const scaleY = chartRect.height / 250;
  els.chartTooltip.replaceChildren();
  const strong = document.createElement("strong");
  strong.textContent = `${point.distance.toFixed(1)} km`;
  const text = document.createTextNode(`${point.rides} 次骑行 · ${formatDate(point.start.getTime() / 1000)}`);
  els.chartTooltip.append(strong, text);
  els.chartTooltip.style.left = `${chartRect.left - wrapRect.left + point.x * scaleX}px`;
  els.chartTooltip.style.top = `${chartRect.top - wrapRect.top + point.y * scaleY}px`;
  els.chartTooltip.hidden = false;
  event.preventDefault();
}

function hideChartTooltip() {
  els.chartTooltip.hidden = true;
}

function renderRoute(route, activities) {
  els.routeCanvas.replaceChildren();
  const hasRoute = Boolean(route?.points?.length > 1 && route.width && route.height);
  els.routeCanvas.hidden = !hasRoute;
  els.routeKey.hidden = !hasRoute;
  els.routeEmpty.hidden = hasRoute;
  if (!hasRoute) {
    els.routeDate.textContent = "—";
    els.latestDistance.textContent = "—";
    els.latestDuration.textContent = "—";
    els.latestPoints.textContent = "—";
    return;
  }

  const activity = activities.find((item) => String(item.id) === String(route.activity_id));
  const width = 360;
  const height = 220;
  const padding = 24;
  const routeWidth = Math.max(Number(route.width), 1e-9);
  const routeHeight = Math.max(Number(route.height), 1e-9);
  const scale = Math.min((width - padding * 2) / routeWidth, (height - padding * 2) / routeHeight);
  const offsetX = (width - routeWidth * scale) / 2;
  const offsetY = (height - routeHeight * scale) / 2;
  const points = route.points.map(([x, y]) => [offsetX + x * scale, offsetY + y * scale]);
  const pathData = points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const shadow = svgElement("path", { class: "route-shadow", d: pathData });
  const line = svgElement("path", { class: "route-line", d: pathData });
  const [startX, startY] = points[0];
  const [endX, endY] = points.at(-1);
  const start = svgElement("circle", { class: "route-start", cx: startX, cy: startY, r: 5 });
  const end = svgElement("circle", { class: "route-end", cx: endX, cy: endY, r: 7 });
  els.routeCanvas.append(shadow, line, start, end);
  const pathLength = line.getTotalLength();
  line.style.strokeDasharray = String(pathLength);
  line.style.strokeDashoffset = String(pathLength);
  requestAnimationFrame(() => requestAnimationFrame(() => { line.style.strokeDashoffset = "0"; }));

  const started = activity?.start_time || route.start_time;
  els.routeDate.textContent = formatDate(started, { year: "numeric" });
  els.latestDistance.textContent = activity?.distance_m ? `${(activity.distance_m / 1000).toFixed(1)} km` : "—";
  els.latestDuration.textContent = activity?.duration_s ? formatDuration(activity.duration_s, true) : "—";
  els.latestPoints.textContent = Number(route.source_points || route.points.length).toLocaleString("zh-CN");
  els.routeCanvas.setAttribute(
    "aria-label",
    `${formatDate(started, { year: "numeric" })}活动的真实 FIT GPS 轨迹，共 ${els.latestPoints.textContent} 个采样点`,
  );
}

function bicycleIcon() {
  const svg = svgElement("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" });
  svg.append(
    svgElement("circle", { cx: 6, cy: 16, r: 3.5 }),
    svgElement("circle", { cx: 18, cy: 16, r: 3.5 }),
    svgElement("path", { d: "m6 16 4-7 3 7m-7 0h7l5-7m-9 0h4m3-2h3" }),
  );
  return svg;
}

function filteredActivities() {
  if (!app.dashboard) return [];
  const query = els.searchInput.value.trim().toLocaleLowerCase("zh-CN");
  const filter = els.statusFilter.value;
  return app.dashboard.activities.filter((activity) => {
    const view = statusView(activity.status);
    const matchesQuery = !query || activity.name.toLocaleLowerCase("zh-CN").includes(query) || formatDate(activity.start_time, { year: "numeric" }).includes(query);
    return matchesQuery && (filter === "all" || view.key === filter);
  });
}

function renderActivities() {
  const all = filteredActivities();
  const visible = all.slice(0, app.visibleActivities);
  els.activityList.replaceChildren();
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "list-empty";
    empty.textContent = "没有符合条件的活动";
    els.activityList.append(empty);
  }

  for (const activity of visible) {
    const row = document.createElement("div");
    row.className = "activity-row";
    row.setAttribute("role", "row");

    const primary = document.createElement("div");
    primary.className = "activity-primary";
    primary.setAttribute("role", "cell");
    const icon = document.createElement("span");
    icon.className = "activity-icon";
    icon.append(bicycleIcon());
    const copy = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = activity.name;
    const date = document.createElement("small");
    date.textContent = formatDate(activity.start_time, { year: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit" });
    copy.append(title, date);
    primary.append(icon, copy);

    const distance = document.createElement("span");
    distance.className = "activity-cell";
    distance.setAttribute("role", "cell");
    distance.textContent = activity.distance_m ? (activity.distance_m / 1000).toFixed(1) : "—";
    if (activity.distance_m) distance.append(Object.assign(document.createElement("small"), { textContent: " km" }));

    const duration = document.createElement("span");
    duration.className = "activity-cell";
    duration.setAttribute("role", "cell");
    duration.textContent = activity.duration_s ? formatDuration(activity.duration_s, true) : "—";

    const status = statusView(activity.status);
    const badge = document.createElement("span");
    badge.className = `status-badge ${status.key}`;
    badge.setAttribute("role", "cell");
    badge.textContent = status.label;
    if (activity.error) badge.title = activity.error;
    row.append(primary, distance, duration, badge);
    els.activityList.append(row);
  }
  els.loadMoreButton.hidden = visible.length >= all.length;
  els.loadMoreButton.textContent = `显示更多活动（剩余 ${Math.max(0, all.length - visible.length)} 条）`;
}

function renderConnections(connections) {
  els.connectionList.replaceChildren();
  const abbreviations = { onelap: "顽", strava: "ST", storage: "FIT" };
  for (const connection of connections) {
    const item = document.createElement("article");
    item.className = "connection-item";
    const logo = document.createElement("span");
    logo.className = `connection-logo ${connection.id}`;
    logo.textContent = abbreviations[connection.id] || "·";
    const copy = document.createElement("span");
    copy.className = "connection-copy";
    const name = document.createElement("strong");
    name.textContent = connection.name;
    const detail = document.createElement("span");
    detail.textContent = connection.detail;
    copy.append(name, detail);
    const state = document.createElement("span");
    state.className = `connection-state ${connection.status}`;
    state.title = connection.status === "connected" ? "连接正常" : "需要处理";
    const controls = document.createElement("span");
    controls.className = "connection-controls";
    controls.append(state);
    if (connection.action && ["onelap", "strava"].includes(connection.id)) {
      const action = document.createElement("button");
      action.className = "connection-action";
      action.type = "button";
      action.textContent = connection.action;
      action.addEventListener("click", () => openAuthDialog(connection.id));
      controls.append(action);
    }
    item.append(logo, copy, controls);
    els.connectionList.append(item);
  }
}

function renderDashboard(data) {
  app.dashboard = data;
  renderSummary(data);
  renderChart(data.activities);
  renderRoute(data.latest_route, data.activities);
  renderActivities();
  renderConnections(data.connections);
}

async function loadDashboard() {
  try {
    const data = await request("/api/dashboard");
    renderDashboard(data);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function refreshData() {
  els.refreshButton.disabled = true;
  const original = els.refreshButton.lastChild.textContent;
  els.refreshButton.lastChild.textContent = "正在刷新";
  try {
    const data = await request("/api/refresh", { method: "POST", body: "{}" });
    renderDashboard(data);
    showToast(`已刷新 ${data.activities.length.toLocaleString("zh-CN")} 条活动`);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    els.refreshButton.disabled = false;
    els.refreshButton.lastChild.textContent = original;
  }
}

function openSyncDialog() {
  if (!app.dashboard) return;
  els.syncDialog.showModal();
}

async function startJob(mode) {
  const maxUploads = Math.max(1, Math.min(100, Number(els.uploadLimit.value) || 15));
  els.syncDialog.close();
  els.jobEyebrow.textContent = mode === "preview" ? "同步预检" : "同步进行中";
  els.jobDialogTitle.textContent = mode === "preview" ? "正在检查同步计划" : "正在处理骑行数据";
  els.jobSpinner.className = "job-spinner";
  els.jobProgressBar.style.width = "8%";
  els.jobLog.textContent = "正在启动…";
  els.jobDoneButton.hidden = true;
  els.jobCloseButton.hidden = false;
  els.jobDialog.showModal();
  try {
    app.activeJob = await request("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ mode, max_uploads: maxUploads }),
    });
    pollJob();
  } catch (error) {
    els.jobDialog.close();
    showToast(error.message, "error");
  }
}

async function pollJob() {
  if (!app.activeJob) return;
  window.clearTimeout(app.jobTimer);
  try {
    const job = await request(`/api/jobs/${app.activeJob.id}`);
    app.activeJob = job;
    els.jobLog.textContent = job.lines.length ? job.lines.join("\n") : "正在连接数据源…";
    els.jobLog.scrollTop = els.jobLog.scrollHeight;
    const progressLine = [...job.lines].reverse().find((line) => /\[\s*\d+\/\d+/.test(line));
    const match = progressLine?.match(/\[\s*(\d+)\/(\d+)/);
    const progress = match ? Math.min(96, (Number(match[1]) / Number(match[2])) * 100) : Math.min(88, 10 + job.lines.length * 1.6);
    els.jobProgressBar.style.width = `${progress}%`;
    if (job.status === "running") {
      app.jobTimer = window.setTimeout(pollJob, 900);
      return;
    }
    const success = job.status === "completed";
    els.jobProgressBar.style.width = "100%";
    els.jobProgressBar.style.background = success ? "var(--success)" : "var(--danger)";
    els.jobSpinner.classList.add(success ? "is-done" : "is-error");
    els.jobEyebrow.textContent = success ? "任务已完成" : "任务未完成";
    els.jobDialogTitle.textContent = success ? "骑行数据处理完成" : "同步遇到问题";
    els.jobDoneButton.hidden = false;
    els.jobCloseButton.hidden = false;
    await loadDashboard();
    showToast(success ? "同步任务已完成" : "同步任务失败，请查看日志", success ? "success" : "error");
  } catch (error) {
    showToast(error.message, "error");
    app.jobTimer = window.setTimeout(pollJob, 1800);
  }
}

function toggleMenu(force) {
  const open = typeof force === "boolean" ? force : !els.sidebar.classList.contains("is-open");
  els.sidebar.classList.toggle("is-open", open);
  els.mobileOverlay.hidden = !open;
  els.menuButton.setAttribute("aria-expanded", String(open));
}

function setupNavigation() {
  const navItems = [...document.querySelectorAll("[data-nav]")];
  navItems.forEach((item) => item.addEventListener("click", () => toggleMenu(false)));
  const sections = navItems.map((item) => document.querySelector(`#${item.dataset.nav}`)).filter(Boolean);
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    navItems.forEach((item) => item.classList.toggle("is-active", item.dataset.nav === visible.target.id));
  }, { rootMargin: "-25% 0px -60%", threshold: [0.01, 0.35] });
  sections.forEach((section) => observer.observe(section));
}

function setup() {
  els.dateLabel.textContent = new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "long" }).format(new Date());
  els.refreshButton.addEventListener("click", refreshData);
  els.syncButton.addEventListener("click", openSyncDialog);
  els.previewButton.addEventListener("click", () => startJob("preview"));
  els.confirmSyncButton.addEventListener("click", () => startJob("sync"));
  els.searchInput.addEventListener("input", () => { app.visibleActivities = 10; renderActivities(); });
  els.statusFilter.addEventListener("change", () => { app.visibleActivities = 10; renderActivities(); });
  els.loadMoreButton.addEventListener("click", () => { app.visibleActivities += 15; renderActivities(); });
  els.menuButton.addEventListener("click", () => toggleMenu());
  els.mobileOverlay.addEventListener("click", () => toggleMenu(false));
  els.jobDoneButton.addEventListener("click", () => { app.activeJob = null; window.clearTimeout(app.jobTimer); });
  els.onelapAuthButton.addEventListener("click", loginOnelap);
  els.stravaCookieButton.addEventListener("click", importStravaCookie);
  els.stravaHarButton.addEventListener("click", importStravaHar);
  els.stravaOauthButton.addEventListener("click", startStravaOauth);
  const authResult = new URLSearchParams(window.location.search);
  if (authResult.get("auth") === "strava-success") showToast("Strava API OAuth 授权成功");
  if (authResult.get("auth") === "strava-error") showToast(authResult.get("message") || "Strava 授权失败", "error");
  if (authResult.has("auth")) window.history.replaceState({}, "", window.location.pathname + window.location.hash);
  setupNavigation();
  loadDashboard();
}

setup();
