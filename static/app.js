/* PVE Remote Client frontend */
const state = {
  profiles: [],
  profileId: null,
  connected: false,
  userDisconnected: false,
  metricsTimer: null,
  metricsActive: false,
  metricsInFlight: false,
  guestsInFlight: false,
  guestMetricsInFlight: false,
  guestMetricsStale: false,
  ws: null,
  termBuf: "",
  currentPath: "/",
  guestCache: { vms: [], cts: [] },
  serviceMap: {},
  embedStack: [],
};

const $ = (id) => document.getElementById(id);

function csrfToken() {
  const item = document.cookie.split(";").map((part) => part.trim())
    .find((part) => part.startsWith("pve_client_csrf="));
  return item ? decodeURIComponent(item.split("=", 2)[1] || "") : "";
}
const views = ["dashboard", "guests", "files", "editor", "terminal", "profiles", "embed"];
const titles = {
  dashboard: ["仪表盘", "系统资源与运行状态"],
  guests: ["虚拟机 / 容器", "创建、启停与管理 QEMU / LXC"],
  files: ["文件管理", "浏览、上传、下载、删除远程文件"],
  editor: ["配置编辑", "直接编辑 PVE / 系统配置文件"],
  terminal: ["终端", "SSH 交互终端（端口 22）"],
  profiles: ["连接配置", "内网 IP 或公网 IP / 域名 + 22 端口"],
  embed: ["系统管理", "在软件内打开各系统管理页"],
};

function toast(msg, ok = true) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast " + (ok ? "ok" : "err");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), ok ? 2800 : 5500);
}

async function api(path, options = {}) {
  const timeoutMs = options.timeoutMs === undefined ? 30000 : options.timeoutMs;
  const opt = { headers: {}, ...options };
  delete opt.timeoutMs;
  const controller = new AbortController();
  const priorSignal = opt.signal;
  if (priorSignal) priorSignal.addEventListener("abort", () => controller.abort(), { once: true });
  opt.signal = controller.signal;
  const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
  if (opt.body && !(opt.body instanceof FormData)) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(opt.body);
  }
  const method = String(opt.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = csrfToken();
    if (csrf) opt.headers["X-CSRF-Token"] = csrf;
  }
  let res;
  try {
    res = await fetch(path, opt);
  } catch (err) {
    if (err && err.name === "AbortError") throw new Error("请求超时，请检查 PVE 连接后重试");
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || res.statusText || "请求失败";
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

async function setupAuthUI() {
  try {
    const health = await api("/api/health");
    $("logoutLink")?.classList.toggle("hidden", !health?.auth);
  } catch {
    // Keep the optional action hidden if an older backend or proxy blocks the probe.
  }
}

function setView(name) {
  views.forEach((v) => {
    $(`view-${v}`)?.classList.toggle("active", v === name);
  });
  document.querySelectorAll(".nav-item").forEach((b) => {
    // embed is not a nav item; keep previous nav highlight off
    b.classList.toggle("active", b.dataset.view === name);
  });
  const [t, s] = titles[name] || [name, ""];
  $("viewTitle").textContent = t;
  $("viewSub").textContent = s;
  if (name === "profiles") loadProfiles();
  if (name === "guests") refreshGuests().catch(() => {});
  if (name === "files") loadDir(state.currentPath).catch(() => {});
  if (name === "dashboard") {
    refreshMetrics().catch(() => {});
    loadNode().catch(() => {});
    refreshGuests().catch(() => {});
  }
  if (name === "terminal" && !state.ws) connectTerminal();
}

function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n);
  for (let i = 0; i < u.length; i++) {
    if (Math.abs(v) < 1024 || i === u.length - 1) return (i === 0 ? v : v.toFixed(1)) + u[i];
    v /= 1024;
  }
}

function fmtRate(n) {
  return fmtBytes(n) + "/s";
}

function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

async function loadProfiles(selectId) {
  state.profiles = await api("/api/profiles");
  const sel = $("profileSelect");
  sel.innerHTML = "";
  if (!state.profiles.length) {
    sel.innerHTML = `<option value="">（请先添加连接）</option>`;
  }
  state.profiles.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.id;
    const host = p.domain || p.host || "";
    o.textContent = `${p.name || host} · ${host}:${p.port || 22}`;
    sel.appendChild(o);
  });
  if (selectId) sel.value = selectId;
  else if (state.profileId) sel.value = state.profileId;
  if (!sel.value && state.profiles.length) sel.value = state.profiles[0].id;
  state.profileId = sel.value || null;
  renderProfileList();
}

function renderProfileList() {
  const box = $("profileList");
  box.innerHTML = "";
  if (!state.profiles.length) {
    box.innerHTML = `<div class="muted">还没有连接配置。点击「新建配置」填写主机 IP / 公网域名与 22 端口。</div>`;
    return;
  }
  state.profiles.forEach((p) => {
    const div = document.createElement("div");
    div.className = "list-item";
    div.innerHTML = `
      <div>
        <div class="title">${escapeHtml(p.name || p.host || "未命名")}</div>
        <div class="sub">${escapeHtml((p.domain || p.host || "") + ":" + (p.port || 22))} · ${escapeHtml(p.username || "root")}${p.note ? " · " + escapeHtml(p.note) : ""}</div>
      </div>
      <button class="btn" data-edit-profile="${p.id}">编辑</button>
    `;
    box.appendChild(div);
  });
  box.querySelectorAll("[data-edit-profile]").forEach((btn) => {
    btn.addEventListener("click", () => fillProfileForm(btn.dataset.editProfile));
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fillProfileForm(id) {
  const p = state.profiles.find((x) => x.id === id);
  if (!p) return;
  $("profileFormTitle").textContent = "编辑配置";
  $("pfId").value = p.id;
  $("pfName").value = p.name || "";
  $("pfHost").value = p.host || "";
  $("pfDomain").value = p.domain || "";
  $("pfPort").value = p.port || 22;
  $("pfUser").value = p.username || "root";
  $("pfAuth").value = p.auth_method || "password";
  $("pfPassword").value = "";
  $("pfKey").value = "";
  $("pfKeyPass").value = "";
  $("pfAccept").checked = p.accept_unknown_host !== false;
  $("pfNote").value = p.note || "";
  toggleAuthFields();
}

function newProfileForm() {
  $("profileFormTitle").textContent = "新建配置";
  $("pfId").value = "";
  $("pfName").value = "";
  $("pfHost").value = "";
  $("pfDomain").value = "";
  $("pfPort").value = 22;
  $("pfUser").value = "root";
  $("pfAuth").value = "password";
  $("pfPassword").value = "";
  $("pfKey").value = "";
  $("pfKeyPass").value = "";
  $("pfAccept").checked = true;
  $("pfNote").value = "";
  $("profileTestLog").classList.add("hidden");
  toggleAuthFields();
}

function toggleAuthFields() {
  const key = $("pfAuth").value === "key";
  $("pfKeyBox").classList.toggle("hidden", !key);
  $("pfPasswordBox").classList.toggle("hidden", key);
}

function collectProfile() {
  return {
    id: $("pfId").value || null,
    name: $("pfName").value.trim(),
    host: $("pfHost").value.trim(),
    domain: $("pfDomain").value.trim(),
    port: Number($("pfPort").value || 22),
    username: $("pfUser").value.trim() || "root",
    auth_method: $("pfAuth").value,
    password: $("pfPassword").value,
    private_key: $("pfKey").value,
    key_passphrase: $("pfKeyPass").value,
    accept_unknown_host: $("pfAccept").checked,
    note: $("pfNote").value.trim(),
  };
}

async function saveProfile() {
  const body = collectProfile();
  if (!body.host && !body.domain) {
    toast("请填写主机 IP 或域名", false);
    return;
  }
  const saved = await api("/api/profiles", { method: "POST", body });
  toast("连接配置已保存");
  await loadProfiles(saved.id);
  fillProfileForm(saved.id);
}

async function deleteProfile() {
  const id = $("pfId").value;
  if (!id) return toast("请先选择要删除的配置", false);
  if (!confirm("确定删除该连接配置？")) return;
  await api(`/api/profiles/${id}`, { method: "DELETE" });
  toast("已删除");
  newProfileForm();
  await loadProfiles();
}

async function testProfile() {
  const log = $("profileTestLog");
  log.classList.remove("hidden");
  log.textContent = "正在测试连接…";
  try {
    const body = collectProfile();
    if (!body.id) {
      // temp save not required; test with form values
    }
    const res = await api("/api/profiles/test", { method: "POST", body });
    if (res.ok) {
      log.textContent = "连接成功\n\n" + (res.output || "");
      toast("SSH 连接测试成功");
    } else {
      log.textContent = "连接失败: " + (res.error || "未知错误");
      toast(res.error || "连接失败", false);
    }
  } catch (e) {
    log.textContent = String(e.message || e);
    toast(String(e.message || e), false);
  }
}

function setConnStatus(text, cls = "") {
  const el = $("connStatus");
  el.textContent = text;
  el.className = "status " + cls;
}

function currentProfileId() {
  const id = $("profileSelect").value || state.profileId;
  state.profileId = id || null;
  return state.profileId;
}

/** Bind selected profile; backend will open SSH on demand. */
function ensureProfile() {
  const id = currentProfileId();
  if (!id) {
    toast("请先在左侧选择或新建连接配置", false);
    setConnStatus("未选择连接", "err");
    return null;
  }
  if (state.userDisconnected) {
    toast("连接已断开，请先点「连接」", false);
    setConnStatus("已断开 · 请重新连接", "err");
    return null;
  }
  return id;
}

let connecting = false;

async function connect() {
  const id = currentProfileId();
  if (!id) {
    toast("请先在左侧选择或新建连接配置", false);
    return;
  }
  if (connecting) return;
  connecting = true;
  state.userDisconnected = false;
  setConnStatus("连接中…");
  $("btnConnect").disabled = true;
  try {
    closeTerminal();
    const res = await api(`/api/connect/${id}`, { method: "POST" });
    state.connected = true;
    state.userDisconnected = false;
    setConnStatus("已连接 · " + ((res.output || "").split("\n")[0] || ""), "ok");
    toast("已连接到 PVE");
    startMetrics();
    // load side data in background so connect UI returns fast
    Promise.allSettled([
      refreshGuests(),
      loadDir("/"),
      loadNode(),
      connectTerminal(),
    ]).catch(() => {});
  } catch (e) {
    state.connected = false;
    setConnStatus(String(e.message || e), "err");
    // A failed SSH handshake must not hide the saved appliance links.  Their
    // HTTP proxies are independent from the PVE SSH session and can still be
    // useful while monitoring/terminal access is recovering.
    await loadSavedServices();
    renderServiceCards(buildRowsFromServicesAndGuests(state.guestCache || {}));
    toast(String(e.message || e), false);
  } finally {
    connecting = false;
    $("btnConnect").disabled = false;
  }
}

async function disconnect() {
  stopMetrics();
  closeTerminal();
  state.connected = false;
  state.userDisconnected = true;
  const id = state.profileId || $("profileSelect").value;
  if (id) {
    try { await api(`/api/disconnect/${id}`, { method: "POST" }); } catch {}
  }
  setConnStatus("已断开 · 请重新连接", "err");
  // clear live data so page no longer looks manageable
  const svc = $("serviceGrid");
  if (svc) svc.innerHTML = `<div class="muted" style="padding:12px">已断开连接。点左侧「连接」后可继续管理各系统。</div>`;
  $("vmList").innerHTML = `<div class="muted" style="padding:10px">已断开连接</div>`;
  $("ctList").innerHTML = `<div class="muted" style="padding:10px">已断开连接</div>`;
  $("fileList").innerHTML = `<div class="muted" style="padding:12px">已断开连接</div>`;
  $("mCpu").textContent = "—";
  $("mMem").textContent = "—";
  $("mLoad").textContent = "—";
  $("mNet").textContent = "—";
  $("mCpuBar").style.width = "0%";
  $("mMemBar").style.width = "0%";
  toast("已断开，页面已锁定，需重新连接才能管理");
}

function startMetrics() {
  stopMetrics();
  state.metricsActive = true;
  refreshMetrics();
}

function stopMetrics() {
  if (state.metricsTimer) clearInterval(state.metricsTimer);
  state.metricsTimer = null;
  state.metricsActive = false;
}

async function refreshMetrics() {
  if (!ensureProfile()) return;
  if (state.metricsInFlight) return;
  state.metricsInFlight = true;
  try {
    const m = await api(`/api/metrics/${state.profileId}`, { timeoutMs: 12000 });
    state.connected = true;
    setConnStatus(m.stale ? "监控暂时中断（显示上次数据）" : "已连接" + (m.host ? " · " + m.host : ""), m.stale ? "err" : "ok");
    const cpu = m.cpu_percent ?? 0;
    const mem = m.mem_percent ?? 0;
    $("mCpu").textContent = cpu.toFixed(1) + "%";
    $("mCpuBar").style.width = Math.min(100, cpu) + "%";
    $("mMem").textContent = mem.toFixed(1) + "%";
    $("mMemBar").style.width = Math.min(100, mem) + "%";
    $("mLoad").textContent = (m.load || "—").split(" ").slice(0, 3).join(" ");
    $("mHost").textContent = [m.host, m.cpu_model].filter(Boolean).join(" · ") || "—";
    $("mNet").textContent = fmtRate(m.net_rx_bps) + " / " + fmtRate(m.net_tx_bps);

    const disks = $("diskList");
    disks.innerHTML = "";
    (m.disks || []).forEach((d) => {
      const row = document.createElement("div");
      row.className = "list-item";
      row.innerHTML = `
        <div style="flex:1">
          <div class="title">${escapeHtml(d.mount)}</div>
          <div class="sub">${fmtBytes(d.used)} / ${fmtBytes(d.total)} · 剩余 ${fmtBytes(d.available)}</div>
          <div class="bar ${d.percent > 85 ? "warn" : ""}" style="margin-top:8px"><i style="width:${Math.min(100, d.percent)}%"></i></div>
        </div>
        <div style="font-weight:700">${d.percent}%</div>
      `;
      disks.appendChild(row);
    });
    if (!disks.children.length) disks.innerHTML = `<div class="muted">无磁盘信息</div>`;

    $("hostInfo").textContent = [
      "主机: " + (m.host || "—"),
      "运行: " + (m.uptime || "—"),
      "负载: " + (m.load || "—"),
      "内存: " + fmtBytes(m.mem_used) + " / " + fmtBytes(m.mem_total),
      "CPU: " + (m.cpu_model || "—"),
    ].join("\n");

    if (m.error && !m.stale) setConnStatus("已连接（有告警）", "ok");
  } catch (e) {
    setConnStatus("监控暂时中断，正在自动重试", "err");
  } finally {
    // Guest meters use their own cached endpoint. Keep them refreshing even
    // when the heavier host-wide probe times out over an external network.
    refreshGuestMetrics(state.profileId);
    state.metricsInFlight = false;
    if (state.metricsActive) {
      if (state.metricsTimer) clearTimeout(state.metricsTimer);
      state.metricsTimer = setTimeout(refreshMetrics, 5000);
    }
  }
}

async function loadNode() {
  if (!ensureProfile()) return;
  try {
    const n = await api(`/api/node/${state.profileId}`);
    $("nodeInfo").textContent = JSON.stringify(n, null, 2);
  } catch (e) {
    $("nodeInfo").textContent = String(e.message || e);
  }
}

const SERVICE_RULES = [
  { kind: "ikuai", label: "iKuai 软路由", icon: "IK", keys: ["ikuai", "爱快"] },
  { kind: "istore", label: "iStore / OpenWrt", icon: "iS", keys: ["istore", "istoreos", "openwrt", "lede"] },
  { kind: "fnos", label: "飞牛 OS", icon: "fn", keys: ["fnos", "飞牛", "fnos"] },
  { kind: "nextcloud", label: "Nextcloud", icon: "NC", keys: ["nextcloud", "next-cloud", "nc"] },
  { kind: "nas", label: "NAS", icon: "NAS", keys: ["nas", "truenas", "unraid", "omv", "openmediavault"] },
  { kind: "generic", label: "系统", icon: "OS", keys: [] },
];

function detectService(name, saved) {
  const n = String(name || "").toLowerCase();
  let rule = SERVICE_RULES.find((r) => r.keys.some((k) => n.includes(k))) || SERVICE_RULES[SERVICE_RULES.length - 1];
  if (saved && saved.kind) {
    const byKind = SERVICE_RULES.find((r) => r.kind === saved.kind);
    if (byKind) rule = byKind;
  }
  const label = (saved && saved.label) || rule.label;
  const iconText = (saved && saved.label ? saved.label.slice(0, 2) : rule.icon);
  return { kind: rule.kind, label, icon: iconText };
}

function guestAction(vmid, gtype, action, done) {
  if (!ensureProfile()) return;
  api("/api/guests/action", {
    method: "POST",
    body: { profile_id: state.profileId, vmid, gtype, action },
  })
    .then((res) => {
      toast(res.ok ? `${action} 已提交` : (res.stderr || res.stdout || "操作失败"), !!res.ok);
      setTimeout(() => {
        refreshGuests();
        if (typeof done === "function") done();
      }, 700);
    })
    .catch((e) => toast(String(e.message || e), false));
}

function normalizeUrl(url) {
  let u = String(url || "").trim();
  if (!u) return "";
  if (!/^https?:\/\//i.test(u)) u = "http://" + u;
  return u;
}

function showEmbedBlocked(show, msg) {
  const el = $("embedBlocked");
  if (!el) return;
  el.classList.toggle("hidden", !show);
  if (msg) $("embedBlockedMsg").textContent = msg;
}

function loadEmbed(entry) {
  let url = String(entry.url || "");
  if (url.startsWith("http://") || url.startsWith("https://")) {
    // absolute kept as-is: the dedicated embed origin, or an external page
  } else if (url.startsWith("/")) {
    url = url;
  } else {
    url = normalizeUrl(url);
  }
  $("embedTitle").textContent = entry.title || "系统管理";
  $("embedUrl").value = entry.external || url;
  $("embedFrame").src = "about:blank";
  showEmbedBlocked(false);
  setTimeout(() => {
    $("embedFrame").src = url;
  }, 30);
  setView("embed");
}

function embedStackTop() {
  return state.embedStack[state.embedStack.length - 1] || null;
}

// The embedded UI runs on its own loopback origin, so the client cannot read
// its location. The proxy reports the page it is currently serving instead.
async function embedSyncState() {
  const top = embedStackTop();
  if (!top || !top.key) return null;
  try {
    const s = await api(`/api/embed/state?key=${encodeURIComponent(top.key)}`);
    if (s.base) {
      const base = String(s.base).replace(/\/+$/, "");
      const path = String(s.path || "").replace(/^\/+/, "");
      top.external = path ? `${base}/${path}` : `${base}/`;
      $("embedUrl").value = top.external;
    }
    return s;
  } catch (_) {
    return null;
  }
}

async function openServiceUrl(url, title, vmid) {
  const pid = currentProfileId();
  if (!pid) {
    toast("请先在左侧选择连接配置", false);
    return;
  }
  if (!vmid) {
    toast("缺少系统 ID，无法内嵌打开", false);
    return;
  }
  if (!url) {
    toast("请先在卡片「配置」里填写管理页地址", false);
    return;
  }
  try {
    // Serves the system from the root of its own origin so absolute URLs
    // (/Action/login, /cgi-bin/luci/...) and its router keep working.
    const info = await api("/api/embed/open", {
      method: "POST",
      body: { profile_id: pid, vmid: String(vmid) },
    });
    // Remote viewers (reverse proxy / tunnel) must use the public hostname,
    // because their 127.0.0.1 is their own machine, not this client.
    const localView = ["127.0.0.1", "localhost", "::1", "[::1]"].includes(location.hostname);
    const origin = !localView && info.public_origin ? info.public_origin : info.origin;
    state.embedStack.push({
      url: origin,
      origin,
      localOrigin: info.local_origin || info.origin,
      publicOrigin: info.public_origin || "",
      key: info.key,
      title: title || info.label || "系统管理",
      external: info.external || normalizeUrl(url),
      vmid,
    });
    loadEmbed(embedStackTop());
  } catch (e) {
    toast(String(e.message || e), false);
  }
}

async function embedBack() {
  const top = embedStackTop();
  if (!top) {
    closeEmbed();
    return;
  }
  const s = top.key
    ? await api("/api/embed/back", { method: "POST", body: { key: top.key } }).catch(() => null)
    : null;
  if (s && s.path) {
    top.url = (top.origin || "") + String(s.path).replace(/^\/+/, "");
    loadEmbed(top);
    return;
  }
  if (state.embedStack.length > 1) {
    state.embedStack.pop();
    loadEmbed(embedStackTop());
    return;
  }
  closeEmbed();
}

function closeEmbed() {
  state.embedStack = [];
  $("embedFrame").src = "about:blank";
  showEmbedBlocked(false);
  setView("dashboard");
}

function embedOpenExternal() {
  const top = state.embedStack[state.embedStack.length - 1] || {};
  const url = top.external || top.url;
  if (!url) return;
  const u = normalizeUrl(url);
  window.open(u.startsWith("http") ? u : location.origin + u, "_blank");
}

function bindEmbedUI() {
  $("btnEmbedBack").onclick = embedBack;
  $("btnEmbedBack2").onclick = () => {
    if (state.embedStack.length > 1) embedBack();
    else closeEmbed();
  };
  $("btnEmbedHome").onclick = closeEmbed;
  $("btnEmbedReload").onclick = async () => {
    const top = embedStackTop();
    if (!top || !top.origin) return;
    const s = top.key
      ? await api(`/api/embed/state?key=${encodeURIComponent(top.key)}`).catch(() => null)
      : null;
    const path = s && s.path ? String(s.path).replace(/^\/+/, "") : "";
    top.url = top.origin + path;
    showEmbedBlocked(false);
    $("embedFrame").src = "about:blank";
    setTimeout(() => { $("embedFrame").src = top.url; }, 20);
  };
  $("btnEmbedReset").onclick = () => {
    const top = embedStackTop();
    if (!top || !top.origin) return;
    if (!window.confirm("清除这个系统在本应用内的登录状态并重新登录？")) return;
    showEmbedBlocked(false);
    $("embedFrame").src = top.origin + "_pve_client/reset-appliance-session";
  };
  $("btnEmbedExternal").onclick = embedOpenExternal;
  $("btnEmbedExternal2").onclick = embedOpenExternal;
  const frame = $("embedFrame");
  frame.addEventListener("load", () => {
    // Follow the page the embedded system navigated to (cross-origin, so the
    // proxy is the only source of truth). Some UIs block framing entirely;
    // in that case the user can fall back to「外部打开」.
    embedSyncState();
  });
}

function openServiceModal(row) {
  $("svcVmid").value = row.vmid;
  $("svcType").value = row.type || "qemu";
  $("svcLabel").value = row.label || row.name || "";
  $("svcUrl").value = row.web_url || "";
  $("svcPublicUrl").value = row.public_url || "";
  $("svcNote").value = row.note || "";
  if ($("svcLocalPort")) {
    const n = Number(row.vmid);
    $("svcLocalPort").textContent = Number.isFinite(n) ? `9000 + ${n} = ${9000 + n}` : "9000 + VMID";
  }
  const detected = detectService(row.name, row);
  $("svcKind").value = row.kind || detected.kind || "";
  $("modalService").classList.remove("hidden");
}

async function saveServiceLink() {
  const vmid = $("svcVmid").value;
  if (!vmid) return;
  const pid = currentProfileId();
  if (!pid) {
    toast("请先选择连接配置", false);
    return;
  }
  const payload = {
    label: $("svcLabel").value.trim(),
    web_url: $("svcUrl").value.trim(),
    kind: $("svcKind").value,
    note: $("svcNote").value.trim(),
    public_url: ($("svcPublicUrl")?.value || "").trim(),
  };
  try {
    // local API only — no SSH, should return immediately
    await api("/api/services", {
      method: "POST",
      body: { profile_id: pid, vmid, ...payload },
    });
    state.serviceMap = state.serviceMap || {};
    state.serviceMap[String(vmid)] = payload;
    // merge into guest cache if present
    const all = [...((state.guestCache && state.guestCache.vms) || []), ...((state.guestCache && state.guestCache.cts) || [])];
    const hit = all.find((x) => String(x.vmid) === String(vmid));
    if (hit) {
      Object.assign(hit, payload);
    } else {
      (state.guestCache = state.guestCache || { vms: [], cts: [] }).vms.push({
        vmid,
        type: $("svcType").value || "qemu",
        name: payload.label || vmid,
        status: "unknown",
        ...payload,
      });
    }
    $("modalService").classList.add("hidden");
    toast("已保存，可直接点「软件内管理」");
    renderServiceCards(state.guestCache || { vms: [], cts: [] });
    // refresh live status in background — do not block UI
    refreshGuests().catch(() => {});
  } catch (e) {
    toast(String(e.message || e), false);
  }
}

function mergeGuestMetrics(rows, metricsList) {
  const map = {};
  (metricsList || []).forEach((m) => {
    map[`${m.vmid}`] = m;
  });
  return rows.map((r) => ({ ...r, metrics: map[String(r.vmid)] || null }));
}

function uptimeText(sec) {
  sec = Number(sec || 0);
  if (sec <= 0) return "—";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}天${h}小时`;
  if (h) return `${h}小时${m}分`;
  return `${m}分钟`;
}

function renderServiceCards(data) {
  const box = $("serviceGrid");
  if (!box) return;
  const rows = mergeGuestMetrics([...(data.vms || []), ...(data.cts || [])], data.metrics);
  if (!rows.length) {
    box.innerHTML = `<div class="muted" style="padding:12px">暂无系统。连接 PVE 后自动列出；也可在「虚拟机 / 容器」页查看，或先「连接」再刷新。</div>`;
    return;
  }
  box.innerHTML = rows.map((r) => {
    const meta = detectService(r.name, r);
    const running = r.status === "running";
    const title = r.label || meta.label;
    const subName = r.name || "（未命名）";
    const mt = r.metrics;
    const cpu = mt ? mt.cpu_percent : null;
    const memP = mt ? mt.mem_percent : null;
    const diskP = mt ? mt.disk_percent : null;
    const netReady = !!(mt && mt.net_sampled);
    const netIn = mt ? Number(mt.netin_bps || 0) : 0;
    const netOut = mt ? Number(mt.netout_bps || 0) : 0;
    return `
      <div class="service-card ${running ? "running" : "stopped"}" data-vmid="${escapeHtml(r.vmid)}" data-type="${escapeHtml(r.type || "qemu")}">
        <div class="service-top">
          <div>
            <div class="service-name">${escapeHtml(title)}</div>
            <div class="service-meta">${escapeHtml(subName)} · ID ${escapeHtml(r.vmid)} · ${escapeHtml(r.type === "lxc" ? "LXC" : "VM")}</div>
          </div>
          <div class="service-icon ${escapeHtml(meta.kind)}">${escapeHtml(meta.icon)}</div>
        </div>
        <div>
          <span class="badge ${running ? "on" : "off"}">${escapeHtml(r.status || "unknown")}</span>
          ${mt && running ? `<span class="muted" style="margin-left:8px">运行 ${escapeHtml(uptimeText(mt.uptime))}</span>` : ""}
          ${r.web_url ? `<div class="service-url" style="margin-top:6px">${escapeHtml(r.web_url)}</div>` : ""}
        </div>
        <div class="svc-meters">
          <div class="svc-meter">
            <div class="svc-meter-label"><span>CPU</span><span>${cpu == null ? "—" : cpu + "%"}</span></div>
            <div class="bar ${cpu != null && cpu > 85 ? "warn" : ""}"><i style="width:${cpu == null ? 0 : Math.min(100, cpu)}%"></i></div>
          </div>
          <div class="svc-meter">
            <div class="svc-meter-label"><span>内存</span><span>${memP == null ? "—" : memP + "%"}${mt && mt.maxmem ? " · " + fmtBytes(mt.mem) + "/" + fmtBytes(mt.maxmem) : ""}</span></div>
            <div class="bar ${memP != null && memP > 85 ? "warn" : ""}"><i style="width:${memP == null ? 0 : Math.min(100, memP)}%"></i></div>
          </div>
          <div class="svc-meter">
            <div class="svc-meter-label"><span>磁盘</span><span>${
              !mt || !mt.maxdisk
                ? "—"
                : (mt.disk === 0 && (mt.type || r.type) === "qemu")
                  ? "已分配 " + fmtBytes(mt.maxdisk) + "（未装客户机代理）"
                  : diskP + "% · " + fmtBytes(mt.disk) + "/" + fmtBytes(mt.maxdisk)
            }</span></div>
            <div class="bar ${diskP != null && diskP > 85 ? "warn" : ""}"><i style="width:${diskP == null ? 0 : Math.min(100, diskP)}%"></i></div>
          </div>
          <div class="svc-meter svc-network">
            <div class="svc-meter-label"><span>网络${state.guestMetricsStale ? " · 缓存" : ""}</span><span>${
              !mt ? "—" : netReady ? "↓ " + fmtRate(netIn) + " · ↑ " + fmtRate(netOut) : "采样中…"
            }</span></div>
            ${mt ? `<div class="svc-net-total">累计 ↓ ${fmtBytes(mt.netin || 0)} · ↑ ${fmtBytes(mt.netout || 0)}</div>` : ""}
          </div>
        </div>
        <div class="service-actions">
          ${running
            ? `<button class="btn" data-svc-act="shutdown">关机</button>
               <button class="btn" data-svc-act="stop">强制停止</button>
               <button class="btn" data-svc-act="reboot">重启</button>`
            : `<button class="btn primary" data-svc-act="start">启动</button>`}
          <button class="btn" data-svc-open="1">软件内管理</button>
          <button class="btn" data-svc-cfg="1">配置</button>
        </div>
      </div>
    `;
  }).join("");

  box.querySelectorAll(".service-card").forEach((card) => {
    const vmid = card.dataset.vmid;
    const gtype = card.dataset.type;
    const row = rows.find((x) => String(x.vmid) === String(vmid) && (x.type || "qemu") === gtype) || rows.find((x) => String(x.vmid) === String(vmid));
    card.querySelectorAll("[data-svc-act]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        guestAction(vmid, gtype, btn.dataset.svcAct);
      });
    });
    card.querySelector("[data-svc-open]")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openServiceUrl(row && row.web_url, (row && (row.label || row.name)) || "系统管理", vmid);
    });
    card.querySelector("[data-svc-cfg]")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openServiceModal(row || { vmid, type: gtype });
    });
  });
}

async function loadSavedServices() {
  const pid = currentProfileId();
  if (!pid) return;
  try {
    const saved = await api(`/api/services/${pid}`);
    state.serviceMap = saved || {};
  } catch (_) {
    state.serviceMap = state.serviceMap || {};
  }
}

function buildRowsFromServicesAndGuests(guestData) {
  const guests = [...((guestData && guestData.vms) || []), ...((guestData && guestData.cts) || [])];
  const byId = {};
  guests.forEach((g) => { byId[String(g.vmid)] = g; });
  const saved = state.serviceMap || {};
  Object.keys(saved).forEach((vmid) => {
    if (!byId[vmid]) {
      byId[vmid] = {
        vmid,
        type: "qemu",
        name: saved[vmid].label || vmid,
        status: "unknown",
        label: saved[vmid].label || "",
        web_url: saved[vmid].web_url || "",
        public_url: saved[vmid].public_url || "",
        kind: saved[vmid].kind || "",
        note: saved[vmid].note || "",
        offlineCard: true,
      };
    } else {
      Object.assign(byId[vmid], {
        label: saved[vmid].label || byId[vmid].label || "",
        web_url: saved[vmid].web_url || byId[vmid].web_url || "",
        public_url: saved[vmid].public_url || byId[vmid].public_url || "",
        kind: saved[vmid].kind || byId[vmid].kind || "",
        note: saved[vmid].note || byId[vmid].note || "",
      });
    }
  });
  return {
    vms: Object.values(byId).filter((x) => (x.type || "qemu") !== "lxc"),
    cts: Object.values(byId).filter((x) => x.type === "lxc"),
    metrics: (guestData && guestData.metrics) || [],
  };
}

async function refreshGuests() {
  const pid = ensureProfile();
  if (!pid) return;
  if (state.guestsInFlight) return;
  state.guestsInFlight = true;
  await loadSavedServices();
  try {
    const data = await api(`/api/guests/${pid}`, { timeoutMs: 12000 });
    state.connected = true;
    // Keep the previous meters visible while the independent metric call refreshes.
    const previousMetrics = state.guestCache.metrics || [];
    const withPreviousMetrics = { ...data, metrics: previousMetrics };
    state.guestCache = withPreviousMetrics;
    refreshGuestMetrics(pid);
    const merged = buildRowsFromServicesAndGuests(withPreviousMetrics);
    state.guestCache = merged;
    renderGuestTables(data);
    renderServiceCards(merged);
  } catch (e) {
    // still show saved service cards without SSH
    const fallback = buildRowsFromServicesAndGuests(state.guestCache || {});
    renderServiceCards(fallback);
    toast(String(e.message || e), false);
  } finally {
    state.guestsInFlight = false;
  }
}

async function refreshGuestMetrics(pid) {
  if (!pid || state.guestMetricsInFlight || state.userDisconnected) return;
  state.guestMetricsInFlight = true;
  try {
    const m = await api(`/api/guests-metrics/${pid}`, { timeoutMs: 20000 });
    if (pid !== currentProfileId()) return;
    state.guestMetricsStale = !!m.stale;
    state.guestCache.metrics = m.metrics || state.guestCache.metrics || [];
    renderServiceCards(state.guestCache);
  } catch (_) {
    // A short outage must not erase the last successful CPU/memory/disk values.
  } finally {
    state.guestMetricsInFlight = false;
  }
}

function renderGuestTables(data) {
  const render = (rows, type) => {
    if (!rows.length) return `<div class="muted" style="padding:10px">暂无</div>`;
    const body = rows.map((r) => `
      <tr>
        <td>${escapeHtml(r.vmid)}</td>
        <td>${escapeHtml(r.name || "-")}</td>
        <td><span class="badge ${r.status === "running" ? "on" : "off"}">${escapeHtml(r.status)}</span></td>
        <td class="actions">
          <button class="btn" data-act="start" data-vmid="${r.vmid}" data-type="${type}">启动</button>
          <button class="btn" data-act="shutdown" data-vmid="${r.vmid}" data-type="${type}">关机</button>
          <button class="btn" data-act="stop" data-vmid="${r.vmid}" data-type="${type}">强制停止</button>
          <button class="btn" data-act="reboot" data-vmid="${r.vmid}" data-type="${type}">重启</button>
        </td>
      </tr>
    `).join("");
    return `<table><thead><tr><th>ID</th><th>名称</th><th>状态</th><th>操作</th></tr></thead><tbody>${body}</tbody></table>`;
  };
  $("vmList").innerHTML = render(data.vms || [], "qemu");
  $("ctList").innerHTML = render(data.cts || [], "lxc");
  document.querySelectorAll("#vmList [data-act], #ctList [data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const res = await api("/api/guests/action", {
          method: "POST",
          body: {
            profile_id: state.profileId,
            vmid: btn.dataset.vmid,
            gtype: btn.dataset.type,
            action: btn.dataset.act,
          },
        });
        toast(res.ok ? "操作已提交" : (res.stderr || "操作失败"), !!res.ok);
        setTimeout(refreshGuests, 800);
      } catch (e) {
        toast(String(e.message || e), false);
      }
    });
  });
}

async function openCreateVm() {
  if (!ensureProfile()) return;
  // open immediately so the button always responds
  $("createVmLog").classList.add("hidden");
  $("modalCreateVm").classList.remove("hidden");
  $("vmVmid").value = "";
  $("vmStorage").innerHTML = `<option>加载中…</option>`;
  $("vmIso").innerHTML = `<option>加载中…</option>`;
  try {
    const h = await api(`/api/pve/helpers/${state.profileId}`, { timeoutMs: 12000 });
    $("vmVmid").value = h.nextid || "";
    fillSelect($("vmStorage"), h.storages, "local-lvm");
    fillSelect($("vmIso"), ["（无 ISO，仅创建空盘）", ...(h.isos || [])], "");
  } catch (e) {
    fillSelect($("vmStorage"), ["local-lvm", "local"], "local-lvm");
    fillSelect($("vmIso"), ["（无 ISO，仅创建空盘）"], "");
    $("createVmLog").classList.remove("hidden");
    $("createVmLog").textContent = "加载选项失败: " + (e.message || e);
    toast(String(e.message || e), false);
  }
}

async function openCreateCt() {
  if (!ensureProfile()) return;
  $("createCtLog").classList.add("hidden");
  $("modalCreateCt").classList.remove("hidden");
  $("ctVmid").value = "";
  $("ctStorage").innerHTML = `<option>加载中…</option>`;
  $("ctTemplate").innerHTML = `<option>加载中…</option>`;
  try {
    const h = await api(`/api/pve/helpers/${state.profileId}`, { timeoutMs: 12000 });
    $("ctVmid").value = h.nextid || "";
    fillSelect($("ctStorage"), h.storages, "local-lvm");
    const tpls = h.templates || [];
    if (!tpls.length) {
      fillSelect($("ctTemplate"), ["（未找到模板，请先在 PVE 下载）"], "");
    } else {
      fillSelect($("ctTemplate"), tpls, tpls[0]);
    }
  } catch (e) {
    fillSelect($("ctStorage"), ["local-lvm", "local"], "local-lvm");
    fillSelect($("ctTemplate"), ["（加载失败）"], "");
    $("createCtLog").classList.remove("hidden");
    $("createCtLog").textContent = "加载选项失败: " + (e.message || e);
    toast(String(e.message || e), false);
  }
}

function fillSelect(sel, items, prefer) {
  sel.innerHTML = "";
  items.forEach((x) => {
    const o = document.createElement("option");
    o.value = x.startsWith("（") ? "" : x;
    o.textContent = x;
    sel.appendChild(o);
  });
  if (prefer && [...sel.options].some((o) => o.value === prefer)) sel.value = prefer;
}

async function doCreateVm() {
  if (!ensureProfile()) return;
  const log = $("createVmLog");
  log.classList.remove("hidden");
  log.textContent = "创建中…";
  try {
    const iso = $("vmIso").value;
    const res = await api("/api/guests/create-vm", {
      method: "POST",
      body: {
        profile_id: state.profileId,
        vmid: $("vmVmid").value.trim(),
        name: $("vmName").value.trim(),
        cores: Number($("vmCores").value || 2),
        memory: Number($("vmMemory").value || 2048),
        disk_gb: Number($("vmDisk").value || 32),
        storage: $("vmStorage").value || "local-lvm",
        iso: iso.startsWith("（") ? "" : iso,
        bridge: $("vmBridge").value || "vmbr0",
      },
    });
    log.textContent = JSON.stringify(res, null, 2);
    if (res.ok) {
      toast("虚拟机创建成功");
      refreshGuests();
    } else {
      toast(res.error || "创建失败", false);
    }
  } catch (e) {
    log.textContent = String(e.message || e);
    toast(String(e.message || e), false);
  }
}

async function doCreateCt() {
  if (!ensureProfile()) return;
  const log = $("createCtLog");
  log.classList.remove("hidden");
  log.textContent = "创建中…";
  try {
    const res = await api("/api/guests/create-ct", {
      method: "POST",
      body: {
        profile_id: state.profileId,
        vmid: $("ctVmid").value.trim(),
        hostname: $("ctHost").value.trim(),
        ostemplate: $("ctTemplate").value,
        memory: Number($("ctMemory").value || 512),
        cores: Number($("ctCores").value || 1),
        disk_gb: Number($("ctDisk").value || 8),
        storage: $("ctStorage").value || "local-lvm",
        password: $("ctPass").value,
        unprivileged: $("ctUnpriv").checked,
        start: $("ctStart").checked,
      },
    });
    log.textContent = JSON.stringify(res, null, 2);
    if (res.ok) {
      toast("容器创建成功");
      refreshGuests();
    } else {
      toast(res.stderr || "创建失败", false);
    }
  } catch (e) {
    log.textContent = String(e.message || e);
    toast(String(e.message || e), false);
  }
}

async function uploadCreationMedia(kind) {
  if (!ensureProfile()) return;
  const isVm = kind === "vm";
  const input = $(isVm ? "vmIsoUpload" : "ctTemplateUpload");
  const button = $(isVm ? "btnUploadVmIso" : "btnUploadCtTemplate");
  const log = $(isVm ? "createVmLog" : "createCtLog");
  const file = input.files && input.files[0];
  if (!file) return toast(isVm ? "请先选择 ISO 或镜像文件" : "请先选择 LXC 模板文件", false);
  const lower = file.name.toLowerCase();
  const valid = isVm
    ? [".iso", ".img", ".qcow2"].some((ext) => lower.endsWith(ext))
    : [".tar.gz", ".tar.xz", ".tar.zst"].some((ext) => lower.endsWith(ext));
  if (!valid) return toast(isVm ? "仅支持 .iso / .img / .qcow2" : "仅支持 .tar.gz / .tar.xz / .tar.zst", false);

  button.disabled = true;
  log.classList.remove("hidden");
  log.textContent = `正在上传 ${file.name}（${fmtBytes(file.size)}）…\n大文件请保持软件运行。`;
  try {
    const remoteDir = isVm ? "/var/lib/vz/template/iso" : "/var/lib/vz/template/cache";
    const res = await uploadFile(file, remoteDir);
    const value = isVm ? file.name : `local:vztmpl/${file.name}`;
    const select = $(isVm ? "vmIso" : "ctTemplate");
    if (![...select.options].some((o) => o.value === value)) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.appendChild(option);
    }
    select.value = value;
    log.textContent = `上传完成：${res.path || file.name}\n已自动选中，可继续创建。`;
    toast("上传完成，已自动选中");
  } catch (e) {
    log.textContent = "上传失败：" + (e.message || e);
    toast(String(e.message || e), false);
  } finally {
    button.disabled = false;
  }
}

async function loadDir(path) {
  if (!ensureProfile()) return;
  const box = $("fileList");
  box.innerHTML = `<div class="muted" style="padding:16px">正在列出目录…</div>`;
  try {
    const data = await api(`/api/fs/list?profile_id=${encodeURIComponent(state.profileId)}&path=${encodeURIComponent(path || "/")}`);
    state.connected = true;
    state.currentPath = data.path;
    $("currentPath").textContent = data.path;
    $("pathInput").value = data.path;
    if (!data.entries.length) {
      box.innerHTML = `<div class="muted" style="padding:12px">目录为空</div>`;
      return;
    }
    const rows = data.entries.map((e) => `
      <tr data-path="${escapeHtml(e.path)}" data-dir="${e.is_dir ? 1 : 0}">
        <td>${e.is_dir ? `<span class="badge dir">目录</span>` : `<span class="badge off">文件</span>`}</td>
        <td class="name-cell" style="cursor:pointer;color:#8ec8ff">${escapeHtml(e.name)}</td>
        <td>${e.is_dir ? "—" : fmtBytes(e.size)}</td>
        <td>${escapeHtml(e.mode)}</td>
        <td>${fmtTime(e.mtime)}</td>
        <td class="actions">
          ${e.is_dir ? "" : `<button class="btn" data-open="${escapeHtml(e.path)}">编辑</button>
          <button class="btn" data-dl="${escapeHtml(e.path)}">下载</button>`}
          <button class="btn danger" data-del="${escapeHtml(e.path)}" data-dir="${e.is_dir ? 1 : 0}">删除</button>
        </td>
      </tr>
    `).join("");
    box.innerHTML = `<table><thead><tr><th>类型</th><th>名称</th><th>大小</th><th>权限</th><th>修改时间</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`;
    box.querySelectorAll(".name-cell").forEach((td) => {
      td.addEventListener("dblclick", () => {
        const tr = td.closest("tr");
        if (tr.dataset.dir === "1") loadDir(tr.dataset.path);
      });
      td.addEventListener("click", () => {
        const tr = td.closest("tr");
        if (tr.dataset.dir === "1") loadDir(tr.dataset.path);
      });
    });
    box.querySelectorAll("[data-open]").forEach((b) => {
      b.addEventListener("click", () => openInEditor(b.dataset.open));
    });
    box.querySelectorAll("[data-dl]").forEach((b) => {
      b.addEventListener("click", () => downloadRemote(b.dataset.dl));
    });
    box.querySelectorAll("[data-del]").forEach((b) => {
      b.addEventListener("click", async () => {
        const p = b.dataset.del;
        const rec = b.dataset.dir === "1";
        const name = (p.split("/").filter(Boolean).pop() || p);
        if (!confirm(rec
          ? `【1/2】删除目录：${p}\n\n该目录下的所有内容都会被永久删除。\n确定继续？`
          : `【1/2】删除文件：${p}\n\n确定继续？`)) return;
        if (rec) {
          const typed = prompt(`【2/2】高风险操作：将递归删除目录\n\n${p}\n\n请手动输入目录名称以确认删除：\n${name}`);
          if (typed === null || typed.trim() !== name) {
            toast("名称不匹配，已取消删除", false);
            return;
          }
        } else if (!confirm(`【2/2】再次确认：永久删除文件\n\n${p}\n\n删除后无法恢复，确定删除？`)) {
          return;
        }
        try {
          await api("/api/fs/remove", { method: "POST", body: { profile_id: state.profileId, path: p, recursive: rec } });
          toast("已删除 " + name);
          loadDir(state.currentPath);
        } catch (e) {
          toast(String(e.message || e), false);
        }
      });
    });
  } catch (e) {
    toast(String(e.message || e), false);
  }
}

async function downloadRemote(path) {
  if (!ensureProfile()) return;
  const name = (path.split("/").filter(Boolean).pop() || "download.bin");
  toast("正在下载 " + name + " …");
  try {
    const url = `/api/fs/download?profile_id=${encodeURIComponent(state.profileId)}&path=${encodeURIComponent(path)}`;
    const res = await fetch(url);
    if (!res.ok) {
      let msg = "下载失败";
      try {
        const data = await res.json();
        msg = data.detail || data.error || msg;
      } catch {}
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    const blob = await res.blob();
    if (!blob || blob.size === 0) {
      throw new Error("下载内容为空");
    }
    const a = document.createElement("a");
    const objUrl = URL.createObjectURL(blob);
    a.href = objUrl;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objUrl), 15000);
    toast(`已下载 ${name}（${fmtBytes(blob.size)}）`);
  } catch (e) {
    toast(String(e.message || e), false);
  }
}

async function openInEditor(path) {
  setView("editor");
  $("editPath").value = path;
  if (!ensureProfile()) return;
  try {
    const data = await api(`/api/fs/read?profile_id=${encodeURIComponent(state.profileId)}&path=${encodeURIComponent(path)}`);
    $("editor").value = data.content;
    $("editMeta").textContent = `${data.path} · ${fmtBytes(data.size)} · ${data.encoding}`;
  } catch (e) {
    toast(String(e.message || e), false);
  }
}

async function saveEditor() {
  const path = $("editPath").value.trim();
  if (!path) return toast("请填写文件路径", false);
  if (!ensureProfile()) return;
  try {
    const res = await api("/api/fs/write", {
      method: "POST",
      body: { profile_id: state.profileId, path, content: $("editor").value },
    });
    toast("已保存 " + fmtBytes(res.bytes));
    $("editMeta").textContent = `${path} · ${fmtBytes(res.bytes)} · 已保存`;
  } catch (e) {
    toast(String(e.message || e), false);
  }
}

async function uploadFile(file, remoteDir) {
  const fd = new FormData();
  fd.append("file", file);
  const url = `/api/fs/upload?profile_id=${encodeURIComponent(state.profileId)}&remote_dir=${encodeURIComponent(remoteDir)}`;
  const res = await fetch(url, { method: "POST", body: fd, headers: { "X-CSRF-Token": csrfToken() } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "上传失败");
  return data;
}

/* Terminal */
function termAppend(text) {
  const term = $("term");
  term.textContent += text;
  if (term.textContent.length > 200000) {
    term.textContent = term.textContent.slice(-120000);
  }
  term.scrollTop = term.scrollHeight;
}

function closeTerminal() {
  if (state.ws) {
    try { state.ws.close(); } catch {}
  }
  state.ws = null;
}

function connectTerminal() {
  closeTerminal();
  if (!ensureProfile()) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = new URL(`${proto}://${location.host}/ws/terminal/${encodeURIComponent(state.profileId)}`);
  const csrf = csrfToken();
  if (csrf) wsUrl.searchParams.set("__pve_ws_csrf", csrf);
  const url = wsUrl.href;
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    termAppend("\n[错误] 无法创建终端连接: " + (e.message || e) + "\n");
    return;
  }
  state.ws = ws;
  termAppend("\n[系统] 正在连接终端…\n");
  ws.onmessage = (ev) => {
    if (state.ws !== ws) return;
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "data") termAppend(msg.data);
      else if (msg.type === "ready") {
        state.connected = true;
        termAppend("\n[系统] " + msg.data + "\n");
      } else if (msg.type === "error") termAppend("\n[错误] " + msg.data + "\n");
      else if (msg.type === "closed") termAppend("\n[系统] " + msg.data + "\n");
    } catch {
      termAppend(String(ev.data));
    }
  };
  ws.onclose = (ev) => {
    if (state.ws !== ws) return;
    state.ws = null;
    termAppend(`\n[系统] 终端连接已关闭（code=${ev.code}${ev.reason ? " " + ev.reason : ""}）\n`);
  };
  ws.onerror = () => {
    if (state.ws !== ws) return;
    termAppend("\n[错误] 终端通道异常。若其他功能正常，多半是 WebSocket 未就绪；请点「重连终端」。\n");
  };
}

function sendTerm(data) {
  if (!state.ws || state.ws.readyState !== 1) {
    termAppend("\n[系统] 终端未连接\n");
    return;
  }
  state.ws.send(JSON.stringify({ type: "data", data }));
}

/* wire up */
function bindUI() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => setView(btn.dataset.view));
  });
  bindEmbedUI();
  $("btnConnect").onclick = connect;
  $("btnDisconnect").onclick = disconnect;
  $("btnRefresh").onclick = () => {
    if (!ensureProfile()) return;
    refreshMetrics();
    loadNode();
    refreshGuests();
  };
  $("profileSelect").onchange = async () => {
    const old = state.profileId;
    const next = $("profileSelect").value;
    state.profileId = next;
    state.connected = false;
    state.userDisconnected = false;
    stopMetrics();
    closeTerminal();
    if (old && old !== next) {
      try { await api(`/api/disconnect/${old}`, { method: "POST" }); } catch {}
    }
    if (next) connect().catch(() => {});
  };

  $("btnNewProfile").onclick = newProfileForm;
  $("btnSaveProfile").onclick = saveProfile;
  $("btnDeleteProfile").onclick = deleteProfile;
  $("btnTestProfile").onclick = testProfile;
  $("pfAuth").onchange = toggleAuthFields;

  $("btnRefreshGuests").onclick = refreshGuests;
  $("btnCreateVm").onclick = openCreateVm;
  $("btnCreateCt").onclick = openCreateCt;
  $("btnDoCreateVm").onclick = doCreateVm;
  $("btnDoCreateCt").onclick = doCreateCt;
  $("btnUploadVmIso").onclick = () => uploadCreationMedia("vm");
  $("btnUploadCtTemplate").onclick = () => uploadCreationMedia("ct");
  const btnSaveService = $("btnSaveService");
  if (btnSaveService) btnSaveService.onclick = saveServiceLink;
  document.querySelectorAll("[data-close]").forEach((b) => {
    b.onclick = () => $(b.dataset.close).classList.add("hidden");
  });

  $("btnReloadDir").onclick = () => loadDir(state.currentPath);
  $("btnUp").onclick = () => {
    const p = state.currentPath || "/";
    if (p === "/") return;
    const parent = p.replace(/\/+$/, "").split("/").slice(0, -1).join("/") || "/";
    loadDir(parent);
  };
  $("btnGoPath").onclick = () => loadDir($("pathInput").value || "/");
  $("pathInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadDir($("pathInput").value || "/");
  });
  $("btnMkdir").onclick = async () => {
    const name = prompt("新目录名称");
    if (!name) return;
    const base = state.currentPath === "/" ? "" : state.currentPath;
    try {
      await api("/api/fs/mkdir", { method: "POST", body: { profile_id: state.profileId, path: base + "/" + name } });
      toast("目录已创建");
      loadDir(state.currentPath);
    } catch (e) {
      toast(String(e.message || e), false);
    }
  };
  $("btnUpload").onclick = () => $("fileInput").click();
  $("fileInput").onchange = async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    try {
      toast("上传中…");
      const res = await uploadFile(f, state.currentPath || "/");
      toast("上传完成 " + fmtBytes(res.size));
      loadDir(state.currentPath);
    } catch (err) {
      toast(String(err.message || err), false);
    } finally {
      e.target.value = "";
    }
  };

  $("btnOpenFile").onclick = () => openInEditor($("editPath").value.trim());
  $("btnSaveFile").onclick = saveEditor;

  $("btnTermClear").onclick = () => { $("term").textContent = ""; };
  $("btnTermReconnect").onclick = connectTerminal;
  $("termInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const v = $("termInput").value;
      sendTerm(v + "\n");
      $("termInput").value = "";
    }
  });
  $("term").addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key === "c") {
      e.preventDefault();
      sendTerm("\x03");
    }
  });

  setInterval(() => {
    $("clock").textContent = new Date().toLocaleString();
  }, 1000);
}

async function boot() {
  bindUI();
  toggleAuthFields();
  await setupAuthUI();
  try {
    await loadProfiles();
  } catch (e) {
    toast("加载配置失败: " + e.message, false);
  }
  if (state.profiles.length) {
    setConnStatus("未连接 · 可直接点「连接」");
    // show saved management cards immediately (no SSH needed)
    await loadSavedServices();
    renderServiceCards(buildRowsFromServicesAndGuests(state.guestCache || {}));
    connect().catch(() => {});
  } else {
    setView("profiles");
    newProfileForm();
    toast("请先新建连接配置（IP/域名 + 22 端口）", false);
  }
}

boot();
