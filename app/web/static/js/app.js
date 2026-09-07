"use strict";

/* NexTunnel panel — shared helpers + SPA logic */
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

function toast(msg, type = "ok") {
  let wrap = $(".toast-wrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.className = "toast-wrap";
    document.body.appendChild(wrap);
  }
  const t = document.createElement("div");
  t.className = `toast ${type}`;
  t.textContent = msg;
  wrap.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("unauthorized");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty */ }
  if (!res.ok) {
    throw new Error((data && data.detail) || `خطای ${res.status}`);
  }
  return data;
}

/* value formatting */
function fmtBytes(b) {
  if (!b && b !== 0) { b = 0; }
  if (b < 1024) return `${b} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(2)} MB`;
  if (b < 1024 ** 4) return `${(b / 1024 ** 3).toFixed(2)} GB`;
  return `${(b / 1024 ** 4).toFixed(2)} TB`;
}
const fmtNum = v => v.toLocaleString("fa-IR");
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const SHA = x => Math.abs(x).toLocaleString("fa-IR", { maximumFractionDigits: 1 });

/* ── Navigation ── */
function navTo(section) {
  $$(".nav-item[data-nav]").forEach(n => n.classList.toggle("active", n.dataset.nav === section));
  $$("section[data-page]").forEach(s => s.classList.toggle("fade", s.dataset.page !== section));
  $$("section[data-page]").forEach(s => s.style.display = s.dataset.page === section ? "" : "none");
  $$("section[data-page]").forEach(s => { if (s.dataset.page === section) s.classList.add("fade"); });
  history.replaceState(null, "", section === "overview" ? "/" : `#${section}`);
}
document.addEventListener("click", e => {
  const n = e.target.closest(".nav-item[data-nav]");
  if (n) navTo(n.dataset.nav);
});

/* ── Modal ── */
function openModal(html) {
  const b = document.createElement("div");
  b.className = "modal-backdrop";
  b.innerHTML = `<div class="modal">${html}</div>`;
  b.addEventListener("click", e => { if (e.target === b) b.remove(); });
  document.body.appendChild(b);
  return b;
}

/* ═══ Dashboard ═════════════════════════════════════════════━━════════ */
let LINKS = [], SUBS = [], STATS = null, HOST = location.host;

function renderOverview() {
  const el = $("#page-overview");
  if (!STATS) return;
  el.innerHTML = `
    <div class="row-between">
      <div><div class="page-title">داشبورد</div><div class="subtitle">آخرین به‌روزرسانی: ${new Date().toLocaleTimeString("fa-IR")}</div></div>
      <button class="btn btn-primary btn-sm" data-action="refresh">بروزرسانی</button>
    </div>
    <div class="grid grid-4 mt">
      <div class="stat"><div class="num">${SHA(STATS.total_traffic_mb)}</div><div class="lbl">ترافیک کل (MB)</div></div>
      <div class="stat"><div class="num">${SHfa(STATS.active_connections)}</div><div class="lbl">اتصال فعال</div></div>
      <div class="stat"><div class="num">${SHfa(STATS.active_links)}</div><div class="lbl">کانفیگ فعال</div></div>
      <div class="stat"><div class="num">${SHfa(STATS.total_requests)}</div><div class="lbl">درخواست کل</div></div>
    </div>
    <div class="grid grid-2 mt">
      <div class="card">${hourlyChart()}</div>
      <div class="card">
        <h4>خطاهای اخیر</h4>
        <div class="list mt">${(STATS.recent_errors || []).map(renderError).join("") || '<div class="muted">موردی نیست</div>'}</div>
      </div>
    </div>
    <div class="grid grid-2 mt">
      <div class="card">
        <h4>آدرس سرور (Host)</h4>
        <div class="code mt">${esc(HOST)}</div>
        <p class="muted mt">این آدرس در لینک‌های اتصال استفاده می‌شود.</p>
      </div>
      <div class="card">
        <h4>اتصال از طریق Telegram</h4>
        <p class="muted">ربات تلگرام را از بخش «ربات» وصل کنید تا کانفیگ‌ها را مستقیم از تلگرام مدیریت کنید.</p>
        <button class="btn btn-primary btn-sm mt" data-nav="bot">رفتن به تنظیمات ربات</button>
      </div>
    </div>`;
}

function SHfa(v) { return v.toLocaleString("fa-IR"); }

function renderError(e) {
  return `<div class="row"><div class="info"><p>${esc(e.error)}</p></div><span class="muted">${esc(e.time)}</span></div>`;
}

function hourlyChart() {
  const h = STATS.hourly || {};
  const keys = Object.keys(h);
  if (!keys.length) return "<h4>ترافیک ساعتی</h4><div class='muted mt'>هنوز داده‌ای نیست</div>";
  const max = Math.max(...Object.values(h), 1);
  const bars = keys.slice(-12).map(k => {
    const pct = Math.round((h[k] / max) * 100);
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px">
      <div style="height:${Math.max(3, pct)}px;width:14px;background:var(--accent);border-radius:4px"></div>
      <span class="muted" style="font-size:9.5px">${esc(k)}</span></div>`;
  }).join("");
  return `<h4>ترافیک ساعتی</h4><div style="display:flex;align-items:flex-end;gap:2px;height:90px;margin-top:12px">${bars}</div>`;
}

function renderLinks() {
  const el = $("#page-links");
  el.innerHTML = `
    <div class="row-between">
      <div><div class="page-title">کانفیگ‌ها</div><div class="subtitle">${LINKS.length} کانفیگ</div></div>
      <button class="btn btn-primary" data-action="new-link">+ جدید</button>
    </div>
    <div class="list mt">
      ${LINKS.map(renderLinkRow).join("") || '<div class="empty">کانفیگی وجود ندارد.</div>'}
    </div>`;
}

function renderLinkRow(l) {
  const pct = l.limit_bytes > 0 ? Math.min(100, Math.round((l.used_bytes / l.limit_bytes) * 100)) : 0;
  const remain = l.limit_bytes > 0 ? `و مانده: ${fmtBytes(l.remaining_bytes)}` : "";
  const expire = l.is_expired ? "<span class='badge off'>منقضی</span>"
    : l.expires_at ? `<span class='badge warn'>${esc(l.expires_at.slice(0, 10))}</span>`
    : "<span class='badge'>بدون انقضا</span>";
  return `
    <div class="row" data-uid="${l.uuid}">
      <label class="switch"><input type="checkbox" ${l.active ? "checked" : ""} data-action="toggle"><span class="track"></span></label>
      <div class="info">
        <h4>${esc(l.label)} ${l.is_default ? "<span class='badge warn'>پیش‌فرض</span>" : ""} ${l.active ? '<span class="badge on">فعال</span>' : '<span class="badge off">غیرفعال</span>'}
          <span style="float:left">${expire}</span></h4>
        <p>vless://${esc(l.uuid.slice(0, 8))}… · ${fmtBytes(l.used_bytes)} ${remain}</p>
        ${l.limit_bytes > 0 ? `<div class="progress"><i style="width:${pct}%"></i></div>` : ""}
      </div>
      <div class="row-actions">
        <button class="btn btn-ghost btn-sm" data-action="copy" data-vless="${esc(l.vless)}">کپی</button>
        <button class="btn btn-ghost btn-sm" data-action="edit">ویرایش</button>
        <button class="btn btn-danger btn-sm" data-action="del">حذف</button>
      </div>
    </div>`;
}

function linkForm(l = null) {
  const isNew = !l;
  openModal(`
    <h3>${isNew ? "کانفیگ جدید" : "ویرایش کانفیگ"}</h3>
    <div class="field"><label>نام (برچسب)</label><input id="f-label" value="${esc(l ? l.label : "")}" placeholder="مثلاً گوشی علی"></div>
    <div class="grid grid-2">
      <div class="field"><label>محدودیت حجم</label><input id="f-limit" type="number" value="${l && l.limit_bytes ? (l.limit_bytes / 1024 ** 3).toFixed(2) : 0}" min="0"></div>
      <div class="field"><label>واحد</label><select id="f-limit-unit"><option value="MB">مگابایت</option><option value="GB" ${l && l.limit_bytes >= 1024 ** 3 ? "selected" : ""}>گیگابایت</option></select></div>
    </div>
    <div class="grid grid-2">
      <div class="field"><label>انقضا (روز؛ ۰ = نامحدود)</label><input id="f-expire" type="number" value="0" min="0"></div>
      <div class="field"><label>محدودیت تعداد آی‌پی</label><input id="f-ip" type="number" value="${l ? l.ip_limit : 0}" min="0"></div>
    </div>
    <div class="grid grid-2">
      <div class="field"><label>محدودیت سرعت</label><input id="f-speed" type="number" value="0" min="0"></div>
      <div class="field"><label>واحد سرعت</label><select id="f-speed-unit"><option value="MBIT">مگابیت</option><option value="MB">MB/s</option></select></div>
    </div>
    <div class="grid grid-2">
      <div class="field"><label>پروتکل</label><select id="f-protocol">
        <option value="vless-ws" ${l && l.protocol === "vless-ws" ? "selected" : ""}>VLESS + WebSocket</option>
        <option value="xhttp-packet-up" ${l && l.protocol === "xhttp-packet-up" ? "selected" : ""}>XHTTP Packet-Up</option>
        <option value="xhttp-stream-up" ${l && l.protocol === "xhttp-stream-up" ? "selected" : ""}>XHTTP Stream-Up</option>
      </select></div>
      <div class="field"><label>گروه</label><select id="f-sub">
        <option value="">بدون گروه</option>
        ${SUBS.map(s => `<option value="${s.sub_id}" ${l && l.sub_id === s.sub_id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}
      </select></div>
    </div>
    <div class="field"><label>یادداشت</label><input id="f-note" value="${esc(l ? l.note : "")}"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" data-close>انصراف</button>
      <button class="btn btn-primary" id="f-save">${isNew ? "ساخت" : "ذخیره"}</button>
    </div>`);
  $("#f-save").addEventListener("click", async () => {
    const body = {
      label: $("#f-label").value.trim() || "لینک جدید",
      limit_value: parseFloat($("#f-limit").value || 0),
      limit_unit: $("#f-limit-unit").value,
      expires_days: parseInt($("#f-expire").value || 0),
      ip_limit: parseInt($("#f-ip").value || 0),
      speed_limit_value: parseFloat($("#f-speed").value || 0),
      speed_limit_unit: $("#f-speed-unit").value,
      protocol: $("#f-protocol").value,
      sub_id: $("#f-sub").value || null,
      note: $("#f-note").value,
    };
    try {
      if (isNew) await api("/api/links", { method: "POST", body });
      else await api(`/api/links/${l.uuid}/update`, { method: "POST", body });
      toast(isNew ? "کانفیگ ساخته شد" : "ذخیره شد");
      await refresh();
      $(".modal-backdrop")?.remove();
    } catch (e) { toast(e.message, "err"); }
  });
}

function renderGroups() {
  const el = $("#page-groups");
  el.innerHTML = `
    <div class="row-between">
      <div><div class="page-title">گروه‌ها</div><div class="subtitle">برای انتشار چند کانفیگ با یک لینک اشتراک</div></div>
      <button class="btn btn-primary" data-action="new-group">+ گروه</button>
    </div>
    <div class="list mt">
      ${SUBS.map(s => `
        <div class="row">
          <div class="info">
            <h4>${esc(s.name)} <span class="badge">${esc(String(s.links_count))} کانفیگ</span>
              ${s.has_password ? '<span class="badge warn">رمزدار</span>' : ""}</h4>
            <p>sub://${esc(s.sub_id.slice(0, 8))}…</p>
          </div>
          <div class="row-actions">
            <button class="btn btn-ghost btn-sm" data-action="sub-link" data-sid="${s.sub_id}">لینک</button>
            <button class="btn btn-ghost btn-sm" data-action="edit-group" data-sid="${s.sub_id}" data-js='${esc(JSON.stringify(s))}'>ویرایش</button>
            <button class="btn btn-danger btn-sm" data-action="del-group" data-sid="${s.sub_id}">حذف</button>
          </div>
        </div>`).join("") || '<div class="empty">گروهی وجود ندارد.</div>'}
    </div>`;
}

function groupForm(s = null) {
  openModal(`
    <h3>${s ? "ویرایش گروه" : "گروه جدید"}</h3>
    <div class="field"><label>نام گروه</label><input id="g-name" value="${esc(s ? s.name : "")}"></div>
    <div class="field"><label>توضیحات</label><input id="g-desc" value="${esc(s ? s.desc : "")}"></div>
    <div class="field"><label>رمز (اختیاری)</label><input id="g-pass" type="password" placeholder="${s && s.has_password ? "رمز فعلی وجود دارد — خالی بماند تا تغییر نکند" : "بدون رمز"}"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" data-close>انصراف</button>
      <button class="btn btn-primary" id="g-save">ذخیره</button>
    </div>`);
  $("#g-save").addEventListener("click", async () => {
    const body = { name: $("#g-name").value.trim(), desc: $("#g-desc").value.trim(), password: $("#g-pass").value };
    try {
      const sid = s ? s.sub_id : (await api("/api/subs", { method: "POST", body })).sub_id;
      if (s) await api(`/api/subs/${sid}/update`, { method: "POST", body });
      toast("ذخیره شد");
      await refresh();
      $(".modal-backdrop")?.remove();
    } catch (e) { toast(e.message, "err"); }
  });
}

function subLink(sid) {
  openModal(`
    <h3>لینک اشتراک</h3>
    <p class="muted">این لینک تمام کانفیگ‌های گروه را در اختیار کاربر می‌گذارد.</p>
    <div class="code mt">${originUrl()}${esc(`/api/sub/${sid}`)}</div>
    <div class="modal-actions"><button class="btn btn-primary btn-block" data-action="copy-text" data-text="${esc(originUrl() + `/api/sub/${sid}`)}">کپی</button></div>`);
}

function originUrl() { return location.origin; }

function renderConnections() {
  const el = $("#page-connections");
  el.innerHTML = `
    <div class="row-between">
      <div><div class="page-title">اتصالات فعال</div><div class="subtitle"><span id="conn-count">0</span> اتصال</div></div>
      <button class="btn btn-ghost btn-sm" data-action="refresh">بروزرسانی</button>
    </div>
    <div class="list mt" id="conn-list"><div class="empty">در حال بارگذاری…</div></div>`;
  loadConnections();
}

async function loadConnections() {
  try {
    const d = await api("/api/connections");
    $("#conn-count").textContent = d.count.toLocaleString("fa-IR");
    $("#conn-list").innerHTML = d.connections.map(c => `
      <div class="row">
        <div class="info">
          <h4>${esc(c.label)} <span class="badge">${esc(c.transport)}</span> <span class="badge" style="direction:ltr">${esc(c.ip)}</span></h4>
          <p>${fmtBytes(c.bytes)} داده · ${esc(c.connected_at)}</p>
        </div>
        <button class="btn btn-danger btn-sm" data-action="close-conn" data-id="${c.id}">قطع</button>
      </div>`).join("") || '<div class="empty">اتصال فعالی نیست.</div>';
    $$("[data-action=close-conn]").forEach(b => b.addEventListener("click", async () => {
      try { await api(`/api/connections/${b.dataset.id}/close`, { method: "POST", body: {} }); toast("قطع شد"); loadConnections(); }
      catch (e) { toast(e.message, "err"); }
    }));
  } catch (e) { $("#conn-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

function renderBot() {
  const el = $("#page-bot");
  el.innerHTML = `
    <div class="page-title">ربات تلگرام</div>
    <div class="card">
      <div class="row-between"><h4>اتصال به ربات</h4>${$("#bot-status") ? "" : ""}<span id="bot-status" class="badge off">نامشخص</span></div>
      <p class="muted mt">توکن را از <b>@BotFather</b> بگیرید. ربات، کانفیگ‌ها را در خود تلگرام مدیریت می‌کند.</p>
      <div class="field mt"><label>توکن ربات</label><input id="b-token" dir="ltr" style="text-align:left" placeholder="123456:ABC-DEF…"></div>
      <div class="field"><label>شناسه‌های مدیر (چند عدد با فاصله)</label><input id="b-admins" dir="ltr" style="text-align:left" placeholder="123456789 987654321"></div>
      <button class="btn btn-primary btn-block mt" id="b-save">ذخیره و راه‌اندازی مجدد</button>
      <p class="muted mt" id="b-hint"></p>
    </div>`;
  $("#b-save").addEventListener("click", async () => {
    const token = $("#b-token").value.trim();
    const admins = $("#b-admins").value.split(/[\s,]+/).map(x => parseInt(x)).filter(n => Number.isInteger(n));
    try {
      const d = await api("/api/bot", { method: "POST", body: { token, admins } });
      $("#bot-status").textContent = d.running ? "فعال" : "غیرفعال";
      $("#bot-status").className = "badge " + (d.running ? "on" : "off");
      toast("ربات ذخیره شد");
    } catch (e) { toast(e.message, "err"); }
  });
}

function renderSettings() {
  const el = $("#page-settings");
  el.innerHTML = `
    <div class="page-title">تنظیمات</div>
    <div class="card">
      <h4>تغییر رمز پنل</h4>
      <div class="field mt"><label>رمز فعلی</label><input type="password" id="p-cur"></div>
      <div class="field"><label>رمز جدید (حداقل ۴ کاراکتر)</label><input type="password" id="p-new"></div>
      <button class="btn btn-primary btn-block" id="p-save">تغییر رمز</button>
    </div>
    <div class="card mt">
      <h4>راهنمای اتصال کلاینت</h4>
      <p class="muted">هر کانفیگ یک لینک vless دارد که با کلیک روی «کپی» آماده استفاده در
        <b>V2rayN</b>، <b>v2rayNG</b> یا <b>Hiddify</b> می‌شود. حالت XHTTP برای محیط‌های با محدودیت شدید بهتر است.</p>
      <div class="code mt">${esc(HOST)}</div>
    </div>`;
  $("#p-save").addEventListener("click", async () => {
    try {
      await api("/api/password", { method: "POST", body: { current: $("#p-cur").value, new: $("#p-new").value } });
      toast("رمز تغییر کرد");
      $("#p-cur").value = ""; $("#p-new").value = "";
    } catch (e) { toast(e.message, "err"); }
  });
}

async function refresh(section) {
  const [l, s, subs] = await Promise.all([
    api("/api/links"), api("/api/stats"), api("/api/subs"),
  ]);
  LINKS = l.links; STATS = s; SUBS = subs.subs; HOST = l.host;
  renderOverview(); renderLinks(); renderGroups(); renderSettings();
  if (section === "connections") renderConnections();
}

/* ── Wire-up dashboard ── */
async function initDashboard() {
  document.addEventListener("click", async e => {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const a = el.dataset.action;
    if (a === "refresh") return refresh();
    if (a === "new-link") return linkForm();
    if (a === "edit") {
      const l = LINKS.find(x => x.uuid === el.closest("[data-uid]").dataset.uid);
      return linkForm(l);
    }
    if (a === "del") {
      const uid = el.closest("[data-uid]").dataset.uid;
      if (!confirm("حذف این کانفیگ؟")) return;
      try { await api(`/api/links/${uid}/delete`, { method: "POST", body: {} }); toast("حذف شد"); await refresh(); }
      catch (e) { toast(e.message, "err"); }
    }
    if (a === "toggle") {
      const uid = el.closest("[data-uid]").dataset.uid;
      try {
        await api(`/api/links/${uid}/toggle`, { method: "POST", body: { active: el.checked } });
        await refresh();
      } catch (e) { toast(e.message, "err"); el.checked = !el.checked; }
    }
    if (a === "copy") {
      navigator.clipboard?.writeText(el.dataset.vless);
      toast("لینک کپی شد");
    }
    if (a === "new-group") return groupForm();
    if (a === "edit-group") {
      const s = JSON.parse(el.dataset.js);
      return groupForm(s);
    }
    if (a === "del-group") {
      if (!confirm("حذف این گروه؟ کانفیگ‌های داخل آن بدون گروه می‌مانند.")) return;
      try { await api(`/api/subs/${el.dataset.sid}/delete`, { method: "POST", body: {} }); toast("حذف شد"); await refresh(); }
      catch (e) { toast(e.message, "err"); }
    }
    if (a === "sub-link") return subLink(el.dataset.sid);
    if (a === "copy-text") {
      navigator.clipboard?.writeText(el.dataset.text);
      toast("کپی شد");
    }
  });

  const hash = location.hash.replace("#", "");
  try {
    await refresh(hash === "connections" ? hash : null);
    if (hash && ["links", "groups", "connections", "bot", "settings"].includes(hash)) navTo(hash);
    else navTo("overview");

    if (hash === "bot" || hash === "overview") {
      try {
        const b = await api("/api/bot");
        if ($("#bot-status")) {
          $("#bot-status").textContent = b.running ? "فعال" : "غیرفعال";
          $("#bot-status").className = "badge " + (b.running ? "on" : "off");
        }
        if ($("#b-token")) $("#b-token").value = b.token || "";
        if ($("#b-admins")) $("#b-admins").value = (b.admins || []).join(" ");
        $("#b-hint").textContent = b.token ? "ربات متصل است." : "ربات هنوز وصل نیست!";
      } catch (e) { /* bot fetch is non-critical */ }
    }
  } catch (e) {
    toast(e.message, "err");
  }
}

function hideDashboard() {
  document.body.innerHTML = "";
}