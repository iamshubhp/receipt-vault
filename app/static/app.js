"use strict";
/* Receipt Vault front end. One page, hash routes, no framework. */

const view = document.getElementById("view");
const tabs = document.getElementById("tabs");
let brands = [];
let me = { logged_in: false, scan_ready: false };

// ---------- small helpers ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (cents) => new Intl.NumberFormat("en-CA", { style: "currency", currency: "CAD" }).format((cents || 0) / 100);
const plain = (cents) => ((cents || 0) / 100).toFixed(2);
const toCents = (text) => {
  const n = parseFloat(String(text ?? "").replace(/[^0-9.]/g, ""));
  return Number.isFinite(n) ? Math.round(n * 100) : 0;
};
const thisMonth = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; };
const today = () => { const d = new Date(); return `${thisMonth()}-${String(d.getDate()).padStart(2, "0")}`; };
const monthName = (m) => new Date(+m.slice(0, 4), +m.slice(5, 7) - 1, 1).toLocaleDateString("en-CA", { month: "long", year: "numeric" });
const shiftMonth = (m, by) => { const d = new Date(+m.slice(0, 4), +m.slice(5, 7) - 1 + by, 1); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; };
const niceDate = (iso) => iso ? new Date(iso + "T12:00:00").toLocaleDateString("en-CA", { day: "numeric", month: "short", year: "numeric" }) : "";
const rateText = (r) => `${+(+r).toFixed(3)}%`;

function toast(text) {
  const t = document.getElementById("toast");
  t.textContent = text; t.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  if (opts.json !== undefined) { opts.body = JSON.stringify(opts.json); opts.headers = { "Content-Type": "application/json" }; opts.method = opts.method || "POST"; }
  let res;
  try { res = await fetch(path, opts); }
  catch { throw Object.assign(new Error("No connection. Check your signal and try again."), { status: 0 }); }
  if (res.status === 401 && path !== "/api/login") { me.logged_in = false; showLogin(); throw Object.assign(new Error("Log in first."), { status: 401 }); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.message || "That didn't work. Try again."), { status: res.status, data });
  return data;
}

// ---------- routing ----------
const routes = [
  [/^#\/month(?:\/(\d{4}-\d{2}))?$/, (m) => showMonth(m || thisMonth()), "month"],
  [/^#\/add$/, () => showAdd(), "add"],
  [/^#\/edit\/(\d+)$/, (id) => showEdit(id), "month"],
  [/^#\/r\/(\d+)$/, (id) => showReceipt(id), "month"],
  [/^#\/search$/, () => showSearch(), "search"],
  [/^#\/settings$/, () => showSettings(), "settings"],
];

async function route() {
  if (!me.logged_in) return showLogin();
  const hash = location.hash || "#/month";
  for (const [pattern, handler, tab] of routes) {
    const m = hash.match(pattern);
    if (!m) continue;
    tabs.hidden = false;
    tabs.querySelectorAll("a").forEach((a) => a.dataset.tab === tab ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
    try { await handler(m[1]); }
    catch (e) { if (e.status !== 401) view.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
    window.scrollTo(0, 0);
    return;
  }
  location.hash = "#/month";
}
window.addEventListener("hashchange", route);
// Tapping the tab you're already on starts that screen over (hashchange won't fire).
tabs.addEventListener("click", (ev) => { const a = ev.target.closest("a"); if (a && a.getAttribute("href") === location.hash) route(); });

// ---------- login ----------
function showLogin() {
  tabs.hidden = true;
  view.innerHTML = `
    <form class="login" id="login">
      <h1>Receipt Vault</h1>
      <p class="muted">Your receipts and commission, by month.</p>
      <label class="field">Password<input type="password" name="password" autocomplete="current-password" required></label>
      <p class="notice bad" id="loginError" hidden></p>
      <button class="primary">Log in</button>
    </form>`;
  const form = document.getElementById("login");
  form.password.focus();
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const err = document.getElementById("loginError");
    try {
      await api("/api/login", { json: { password: form.password.value } });
      await boot();
    } catch (e) { err.textContent = e.message; err.hidden = false; }
  };
}

// ---------- month ----------
function receiptRow(r) {
  const first = r.items[0] ? r.items[0].description : "";
  const more = r.items.length > 1 ? ` and ${r.items.length - 1} more` : "";
  const who = [r.passenger_name, r.flight_no].filter(Boolean).join(", ");
  const flag = r.needs_review ? `<span class="flag">Check brand</span>` : r.total_mismatch ? `<span class="flag">Total differs</span>` : "";
  return `<a class="receiptrow" href="#/r/${r.id}">
      <span class="no">No. ${esc(r.receipt_no)} ${flag}</span><span class="total">${money(r.total_cents)}</span>
      <span class="sub">${esc(niceDate(r.sale_date))} ${esc(r.sale_time)}${who ? ", " + esc(who) : ""}</span>
      <span class="earn">${r.commission_sales_cents ? (r.commission_cents ? "earns " + money(r.commission_cents) : "commission") : ""}</span>
      <span class="sub">${esc(first)}${more}</span><span></span>
    </a>`;
}

async function showMonth(month) {
  const data = await api(`/api/month/${month}`);
  const s = data.summary, rate = data.settings.commission_rate;
  const share = s.total_cents ? (s.commission_sales_cents / s.total_cents) * 100 : 0;
  const hasRate = rate > 0 || s.brands.some((b) => b.commission_cents > 0);
  const hero = hasRate
    ? `<p class="muted">Commission earned</p><p class="figure">${money(s.commission_cents)}</p>
       <p class="muted small">${rateText(rate)} of ${data.settings.commission_basis === "list" ? "the price before discount" : "the price paid"}</p>`
    : `<p class="muted">Commission sales</p><p class="figure">${money(s.commission_sales_cents)}</p>
       <p class="small"><a href="#/settings">Add your commission rate</a> <span class="muted">to see what you earned.</span></p>`;
  const alerts = [
    s.needs_review ? `<p class="notice">${s.needs_review} receipt${s.needs_review > 1 ? "s have" : " has"} a brand to check. Open ${s.needs_review > 1 ? "them" : "it"} and confirm the tag.</p>` : "",
    s.total_mismatch ? `<p class="notice bad">${s.total_mismatch} receipt${s.total_mismatch > 1 ? "s don't" : " doesn't"} add up to the printed total.</p>` : "",
  ].join("");
  const body = s.receipts === 0
    ? `<div class="card empty"><p>No receipts saved for ${esc(monthName(month))}.</p><a class="btn primary" href="#/add">Add a receipt</a></div>`
    : `${alerts ? `<div class="stack" style="margin-top:14px">${alerts}</div>` : ""}
       <h2>By brand</h2>
       <div class="list">${s.brands.map((b) => `<div class="brandrow">
           <span>${esc(b.name)} <span class="muted small">${b.units} sold</span></span>
           <span class="amt">${money(b.sales_cents)}${b.commission_cents ? `<br><span class="earn small">earns ${money(b.commission_cents)}</span>` : ""}</span>
         </div>`).join("")}</div>
       <h2>Receipts</h2>
       <div class="list">${data.receipts.map(receiptRow).join("")}</div>
       <p style="margin-top:16px"><a class="btn" href="/api/export/${month}.csv" download>Download ${esc(monthName(month))} as a spreadsheet</a></p>`;
  view.innerHTML = `
    <div class="monthbar">
      <button id="prev" aria-label="Previous month">&lsaquo;</button>
      <h1>${esc(monthName(month))}</h1>
      <button id="next" aria-label="Next month">&rsaquo;</button>
    </div>
    <section class="earned">
      ${hero}
      <div class="split" role="img" aria-label="${Math.round(share)}% of sales were commission brands">
        ${s.total_cents ? `<i class="c" style="width:${share}%"></i><i class="o" style="width:${100 - share}%"></i>` : ""}
      </div>
      <div class="legend">
        <div><span class="key">Commission brands</span><span>${money(s.commission_sales_cents)}</span></div>
        <div><span class="key o">Other brands</span><span>${money(s.other_sales_cents)}</span></div>
        <div class="all"><span>${s.receipts} receipt${s.receipts === 1 ? "" : "s"}</span><span>${money(s.total_cents)}</span></div>
      </div>
    </section>
    ${body}`;
  document.getElementById("prev").onclick = () => (location.hash = `#/month/${shiftMonth(month, -1)}`);
  document.getElementById("next").onclick = () => (location.hash = `#/month/${shiftMonth(month, 1)}`);
}

// ---------- one receipt ----------
async function showReceipt(id) {
  const r = await api(`/api/receipts/${id}`);
  const row = (label, value) => value ? `<dt>${label}</dt><dd>${esc(value)}</dd>` : "";
  view.innerHTML = `
    <div class="row"><a class="btn quiet" href="#/month/${r.month}">&lsaquo; ${esc(monthName(r.month))}</a></div>
    <article class="slip">
      <p class="tag">Receipt no.</p>
      <p class="no">${esc(r.receipt_no)}</p>
      <p>${esc(niceDate(r.sale_date))} ${esc(r.sale_time)}</p>
      <hr>
      <dl>
        ${row("Passenger", r.passenger_name)}${row("Flight", r.flight_no)}${row("Destination", r.destination)}
        ${r.final_dest && r.final_dest !== r.destination ? row("Final dest.", r.final_dest) : ""}
        ${row("Departs", niceDate(r.dep_date))}${row("Terminal", r.terminal)}${row("Store ref", r.store_ref)}
        ${row("Served by", r.served_by)}${row("Paid with", r.payment_method)}${row("Auth no.", r.auth_no)}
        ${r.club_avolta ? row("Club Avolta", "5% off") : ""}
      </dl>
      <hr>
      ${r.items.map((i) => `
        <div class="line"><b>${esc(i.description)}${i.qty > 1 ? " x" + i.qty : ""}</b><b>${plain(i.paid_cents)}</b></div>
        ${i.list_cents !== i.paid_cents ? `<div class="line tag"><span>before discount</span><span>${plain(i.list_cents)}</span></div>` : ""}
        <div class="line tag ${i.commissionable ? "c" : ""}"><span>${i.commissionable ? esc(i.brand_name) : esc(i.brand_text || "Other brand") + ", no commission"}${i.needs_review ? " (check)" : ""}</span>
          <span>${i.commission_cents ? "earns " + plain(i.commission_cents) : ""}</span></div>
        ${i.barcode ? `<div class="tag">${esc(i.barcode)}</div>` : ""}`).join("<br>")}
      <hr>
      <div class="line sum"><span>Total CAD</span><span>${plain(r.total_cents)}</span></div>
      ${r.other_sales_cents && r.commission_sales_cents ? `
        <div class="line"><span>Commission brands</span><span>${plain(r.commission_sales_cents)}</span></div>
        <div class="line"><span>Other brands</span><span>${plain(r.other_sales_cents)}</span></div>` : ""}
      ${r.commission_cents ? `<div class="line tag c"><span>Your commission</span><span>${plain(r.commission_cents)}</span></div>` : ""}
      ${r.total_mismatch ? `<div class="line" style="color:#b3261e"><span>Printed total</span><span>${plain(r.printed_total_cents)}</span></div>` : ""}
      ${r.notes ? `<hr><p>${esc(r.notes)}</p>` : ""}
    </article>
    ${r.needs_review ? `<p class="notice" style="margin-top:14px">A brand on this receipt needs checking. Edit it and confirm the tag.</p>` : ""}
    ${r.total_mismatch ? `<p class="notice bad" style="margin-top:14px">The items add up to ${money(r.total_cents)} but the paper says ${money(r.printed_total_cents)}. Edit the prices so they match.</p>` : ""}
    <div class="grid2" style="margin-top:16px">
      <a class="btn primary" href="#/edit/${r.id}">Edit</a>
      <button class="danger" id="del">Delete</button>
    </div>
    ${r.image_id ? `<h2>Photo of the paper receipt</h2><a href="/api/images/${r.image_id}" target="_blank" rel="noopener"><img class="photo" src="/api/images/${r.image_id}" alt="Photo of receipt ${esc(r.receipt_no)}"></a>` : ""}`;
  document.getElementById("del").onclick = async () => {
    if (!confirm(`Delete receipt ${r.receipt_no} and its photo? This can't be undone.`)) return;
    await api(`/api/receipts/${r.id}`, { method: "DELETE" });
    toast("Deleted"); location.hash = `#/month/${r.month}`;
  };
}

// ---------- add and edit ----------
function shrinkPhoto(file) {
  // Phone photos are several MB; 2000px is plenty to read a receipt.
  return new Promise((resolve) => {
    const img = new Image(), url = URL.createObjectURL(file);
    img.onload = () => {
      const scale = Math.min(1, 2000 / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      c.toBlob((b) => resolve(b || file), "image/jpeg", 0.85);
    };
    img.onerror = () => { URL.revokeObjectURL(url); resolve(file); };
    img.src = url;
  });
}

function showAdd() {
  view.innerHTML = `
    <h1>Add a receipt</h1>
    <p class="muted" style="margin-top:6px">${me.scan_ready
      ? "Lay the receipt flat, fill the frame, and keep the whole slip in shot."
      : "Photo reading is off until the API key is added in Railway. You can still attach a photo and type the details."}</p>
    <div class="pick">
      <label class="btn primary">Take a photo<input type="file" accept="image/*" capture="environment" id="cam" hidden></label>
      <label class="btn">Choose a photo<input type="file" accept="image/*" id="lib" hidden></label>
      <button id="manual">Type it in without a photo</button>
    </div>`;
  const onFile = async (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    view.innerHTML = `<div class="reading"><h1>${me.scan_ready ? "Reading the receipt" : "Saving the photo"}</h1><p class="muted">This takes a few seconds.</p><div class="bar"></div></div>`;
    try {
      const body = new FormData();
      body.append("photo", await shrinkPhoto(file), "receipt.jpg");
      const res = await api("/api/scan", { method: "POST", body });
      showForm({ ...(res.draft || {}), image_id: res.image_id }, { problem: res.problem, scanned: !!res.draft });
    } catch (e) {
      showAdd(); toast(e.message);
    }
  };
  document.getElementById("cam").onchange = onFile;
  document.getElementById("lib").onchange = onFile;
  document.getElementById("manual").onclick = () => showForm({}, {});
}

async function showEdit(id) {
  const r = await api(`/api/receipts/${id}`);
  showForm(r, { editing: true });
}

function showForm(draft, ctx) {
  const d = { receipt_no: "", terminal: "", sale_date: ctx.scanned || ctx.editing ? "" : today(), sale_time: "", store_ref: "", dep_date: "",
    destination: "", final_dest: "", flight_no: "", passenger_name: "", served_by: "", payment_method: "", auth_no: "",
    club_avolta: false, printed_total_cents: null, notes: "", image_id: null, unreadable: [], ...draft };
  let items = (d.items && d.items.length ? d.items : [{}]).map((i) => ({
    barcode: "", description: "", brand_id: null, brand_text: "", qty: 1, list_cents: 0, paid_cents: 0,
    needs_review: false, touched: !!ctx.editing || !!ctx.scanned, ...i,
    hint: i.hint || (i.needs_review ? "Check this brand tag." : "") }));

  const field = (label, name, value, extra = "") => `<label class="field">${label}<input type="text" name="${name}" value="${esc(value)}" ${extra}></label>`;
  view.innerHTML = `
    <div class="row">
      <div class="grow"><h1>${ctx.editing ? "Edit receipt" : ctx.scanned ? "Check and save" : "New receipt"}</h1>
        ${ctx.scanned ? `<p class="muted small" style="margin-top:4px">Compare with the paper before saving. The reader can misread a digit.</p>` : ""}</div>
      ${d.image_id ? `<a href="/api/images/${d.image_id}" target="_blank" rel="noopener"><img class="thumb" src="/api/images/${d.image_id}" alt="Open the receipt photo"></a>` : ""}
    </div>
    ${ctx.problem ? `<p class="notice bad" style="margin-top:14px">${esc(ctx.problem)}</p>` : ""}
    ${d.unreadable.length ? `<p class="notice" style="margin-top:14px">Couldn't read: ${esc(d.unreadable.join(", "))}. Fill these in from the paper.</p>` : ""}
    <form id="form" novalidate>
      <fieldset><legend>Receipt</legend><div class="stack">
        <div class="grid2">${field("Receipt no.", "receipt_no", d.receipt_no, 'inputmode="numeric" required placeholder="Transaction Seq. No."')}
          ${field("Terminal Id", "terminal", d.terminal, 'inputmode="numeric"')}</div>
        <div class="grid2"><label class="field">Sale date<input type="date" name="sale_date" value="${esc(d.sale_date)}" required></label>
          <label class="field">Time<input type="time" name="sale_time" value="${esc(d.sale_time)}"></label></div>
        <div class="grid2">${field("Sales ref", "store_ref", d.store_ref)}${field("Served by", "served_by", d.served_by)}</div>
      </div></fieldset>
      <fieldset><legend>Customer and flight</legend><div class="stack">
        ${field("Passenger name", "passenger_name", d.passenger_name, 'autocapitalize="characters"')}
        <div class="grid2">${field("Flight no.", "flight_no", d.flight_no, 'autocapitalize="characters"')}
          <label class="field">Departure date<input type="date" name="dep_date" value="${esc(d.dep_date)}"></label></div>
        <div class="grid2">${field("Destination", "destination", d.destination, 'autocapitalize="characters"')}
          ${field("Final destination", "final_dest", d.final_dest, 'autocapitalize="characters"')}</div>
      </div></fieldset>
      <fieldset><legend>Perfumes sold</legend><div class="stack">
        <label class="switch"><input type="checkbox" name="club_avolta" ${d.club_avolta ? "checked" : ""}><span>Club Avolta member, 5% off every item</span></label>
        <div class="stack" id="items"></div>
        <button type="button" id="addItem">Add another item</button>
      </div></fieldset>
      <fieldset><legend>Payment</legend><div class="stack">
        <div class="grid2">${field("Paid with", "payment_method", d.payment_method)}${field("Authorization no.", "auth_no", d.auth_no)}</div>
        ${field("Total printed on the receipt", "printed_total", d.printed_total_cents == null ? "" : plain(d.printed_total_cents), 'inputmode="decimal"')}
        <p id="check" class="check"></p>
        <label class="field">Notes<textarea name="notes" placeholder="Anything that might matter in a commission dispute">${esc(d.notes)}</textarea></label>
      </div></fieldset>
      <p class="notice bad" id="formError" hidden style="margin-top:14px"></p>
      <div class="savebar"><button class="primary" id="save">${ctx.editing ? "Save changes" : "Save receipt"}</button></div>
    </form>`;

  const form = document.getElementById("form"), box = document.getElementById("items");
  const club = () => form.club_avolta.checked;

  function drawItems() {
    box.innerHTML = items.map((it, n) => `
      <div class="item ${it.brand_id ? "commission" : ""}" data-n="${n}">
        <label class="field">Perfume<input type="text" data-k="description" value="${esc(it.description)}" autocapitalize="characters"></label>
        <div class="grid2">
          <label class="field">Price<input type="text" inputmode="decimal" data-k="list" value="${it.list_cents ? plain(it.list_cents) : ""}"></label>
          <label class="field">Customer paid<input type="text" inputmode="decimal" data-k="paid" value="${it.paid_cents ? plain(it.paid_cents) : ""}"></label>
        </div>
        <label class="field">Commission<select data-k="brand">
          <option value="">No commission (other brand)</option>
          ${brands.filter((b) => b.active || b.id === it.brand_id).map((b) => `<option value="${b.id}" ${b.id === it.brand_id ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
        </select></label>
        ${it.brand_id ? "" : `<label class="field">Brand name (optional)<input type="text" data-k="brand_text" value="${esc(it.brand_text)}"></label>`}
        ${it.hint && it.needs_review ? `<p class="notice hint">${esc(it.hint)} <button type="button" class="link" data-act="confirm">This tag is right</button></p>` : ""}
        <div class="row"><label class="field grow">Barcode<input type="text" inputmode="numeric" data-k="barcode" value="${esc(it.barcode)}"></label>
          ${items.length > 1 ? `<button type="button" class="quiet danger" data-act="remove" style="align-self:end">Remove</button>` : ""}</div>
      </div>`).join("");
    check();
  }

  function check() {
    const sum = items.reduce((t, i) => t + i.paid_cents, 0), el = document.getElementById("check");
    const printedText = form.printed_total.value.trim();
    if (!printedText) { el.className = "check"; el.textContent = `Items add up to ${money(sum)}.`; return; }
    const ok = toCents(printedText) === sum;
    el.className = "check " + (ok ? "ok" : "flag");
    el.textContent = ok ? `Items add up to ${money(sum)}, matching the printed total.`
                        : `Items add up to ${money(sum)}, but the printed total is ${money(toCents(printedText))}. Check the prices.`;
  }

  async function autoTag(it) {
    if (it.touched || (!it.description.trim() && !it.barcode.trim())) return;
    try {
      const tag = await api("/api/classify", { json: { description: it.description, barcode: it.barcode } });
      if (it.touched) return;
      Object.assign(it, { brand_id: tag.brand_id, brand_text: it.brand_text || tag.brand_text, needs_review: tag.needs_review, hint: tag.hint });
      drawItems();
    } catch { /* tagging is a convenience; the select still works */ }
  }

  box.addEventListener("input", (ev) => {
    const card = ev.target.closest(".item"); if (!card) return;
    const it = items[+card.dataset.n], k = ev.target.dataset.k, v = ev.target.value;
    if (k === "description" || k === "barcode" || k === "brand_text") it[k] = v;
    if (k === "list") { it.list_cents = toCents(v); it.paid_cents = club() ? Math.round(it.list_cents * 0.95) : it.list_cents;
      card.querySelector('[data-k="paid"]').value = it.paid_cents ? plain(it.paid_cents) : ""; }
    if (k === "paid") it.paid_cents = toCents(v);
    check();
  });
  box.addEventListener("change", (ev) => {
    const card = ev.target.closest(".item"); if (!card) return;
    const it = items[+card.dataset.n], k = ev.target.dataset.k;
    if (k === "brand") { it.brand_id = ev.target.value ? +ev.target.value : null; it.touched = true; it.needs_review = false;
      if (it.brand_id) it.brand_text = brands.find((b) => b.id === it.brand_id).name; drawItems(); }
    if (k === "description" || k === "barcode") autoTag(it);
  });
  box.addEventListener("click", (ev) => {
    const act = ev.target.dataset.act; if (!act) return;
    const n = +ev.target.closest(".item").dataset.n;
    if (act === "remove") items.splice(n, 1);
    if (act === "confirm") { items[n].needs_review = false; items[n].touched = true; }
    drawItems();
  });
  document.getElementById("addItem").onclick = () => {
    items.push({ barcode: "", description: "", brand_id: null, brand_text: "", qty: 1, list_cents: 0, paid_cents: 0, needs_review: false, hint: "", touched: false });
    drawItems();
    box.lastElementChild.querySelector("input").focus();
  };
  form.club_avolta.onchange = () => {
    items.forEach((i) => (i.paid_cents = club() ? Math.round(i.list_cents * 0.95) : i.list_cents));
    drawItems();
  };
  form.printed_total.oninput = check;

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const err = document.getElementById("formError"), btn = document.getElementById("save");
    const fail = (msg) => { err.textContent = msg; err.hidden = false; err.scrollIntoView({ block: "center" }); };
    err.hidden = true;
    const kept = items.filter((i) => i.description.trim() || i.paid_cents);
    if (!form.receipt_no.value.trim()) return fail("Enter the receipt number (Transaction Seq. No. on the paper).");
    if (!form.sale_date.value) return fail("Enter the sale date.");
    if (!kept.length) return fail("Add at least one perfume with a price.");
    if (kept.some((i) => !i.description.trim())) return fail("Every item needs a name.");
    const body = { items: kept.map((i) => ({ barcode: i.barcode, description: i.description, brand_id: i.brand_id,
        brand_text: i.brand_text, qty: i.qty || 1, list_cents: i.list_cents || i.paid_cents, paid_cents: i.paid_cents, needs_review: i.needs_review })),
      club_avolta: club(), image_id: d.image_id,
      printed_total_cents: form.printed_total.value.trim() ? toCents(form.printed_total.value) : null };
    for (const k of ["receipt_no", "terminal", "sale_date", "sale_time", "store_ref", "dep_date", "destination", "final_dest",
      "flight_no", "passenger_name", "served_by", "payment_method", "auth_no", "notes"]) body[k] = form[k].value.trim();
    btn.disabled = true;
    try {
      const res = ctx.editing ? await api(`/api/receipts/${d.id}`, { method: "PUT", json: body }) : await api("/api/receipts", { json: body });
      toast(ctx.editing ? "Changes saved" : "Receipt saved");
      location.hash = `#/r/${res.id}`;
    } catch (e) {
      btn.disabled = false;
      if (e.status === 409 && e.data && e.data.existing_id) {
        err.innerHTML = `${esc(e.message)} <a href="#/r/${e.data.existing_id}">Open the saved one</a>`; err.hidden = false; err.scrollIntoView({ block: "center" });
      } else if (e.status !== 401) fail(e.message);
    }
  };
  drawItems();
}

// ---------- search ----------
function showSearch() {
  view.innerHTML = `
    <h1>Find a receipt</h1>
    <label class="field" style="margin:14px 0">Receipt number, passenger, flight, perfume or authorization number
      <input type="search" id="q" autocomplete="off" enterkeyhint="search"></label>
    <div id="results"></div>`;
  const q = document.getElementById("q"), out = document.getElementById("results");
  let timer, latest = 0;
  q.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const term = q.value.trim(), mine = ++latest;
      if (!term) { out.innerHTML = ""; return; }
      try {
        const found = await api(`/api/receipts?q=${encodeURIComponent(term)}`);
        if (mine !== latest) return;
        out.innerHTML = found.length ? `<div class="list">${found.map(receiptRow).join("")}</div>`
          : `<p class="muted">Nothing saved matches "${esc(term)}". Try the receipt number or part of the name.</p>`;
      } catch (e) { if (e.status !== 401) out.innerHTML = `<p class="notice bad">${esc(e.message)}</p>`; }
    }, 250);
  };
  q.focus();
}

// ---------- settings ----------
async function showSettings() {
  const [settings, list] = await Promise.all([api("/api/settings"), api("/api/brands")]);
  brands = list;
  const words = (a) => a.join(", ");
  const brandForm = (b) => `
    <div class="body" data-id="${b.id || ""}">
      <label class="field">Brand name<input type="text" data-k="name" value="${esc(b.name || "")}"></label>
      <label class="field">How it appears on receipts (words, comma separated)<input type="text" data-k="aliases" value="${esc(words(b.aliases || []))}"></label>
      <label class="field">Only this line pays commission: the receipt must also say one of<input type="text" data-k="requires" value="${esc(words(b.requires || []))}" placeholder="Leave empty if the whole brand counts"></label>
      <label class="field">Its own commission rate, % (leave empty to use your usual rate)<input type="text" inputmode="decimal" data-k="rate" value="${b.rate ?? ""}"></label>
      ${b.id ? `<label class="switch"><input type="checkbox" data-k="active" ${b.active ? "checked" : ""}><span>I earn commission on this brand</span></label>` : ""}
      <button type="button" data-act="saveBrand">${b.id ? "Save brand" : "Add brand"}</button>
    </div>`;
  view.innerHTML = `
    <h1>Settings</h1>
    <form id="rateForm"><fieldset><legend>Your commission</legend><div class="stack">
      <label class="field">Commission rate, %<input type="text" inputmode="decimal" name="rate" value="${settings.commission_rate || ""}" placeholder="For example 2.5"></label>
      <label class="switch"><input type="radio" name="basis" value="paid" ${settings.commission_basis === "paid" ? "checked" : ""}><span>Worked out on the price the customer paid (after the 5% off)</span></label>
      <label class="switch"><input type="radio" name="basis" value="list" ${settings.commission_basis === "list" ? "checked" : ""}><span>Worked out on the price before the discount</span></label>
      <button class="primary">Save commission</button>
      <p class="muted small">Changing this recalculates every month, past ones included.</p>
    </div></fieldset></form>
    <h2>Commission brands</h2>
    <div class="list" id="brandList">
      ${list.map((b) => `<details class="brand ${b.active ? "" : "off"}"><summary><span class="name">${esc(b.name)}</span>
        <span class="muted small">${b.requires.length ? "one line only" : ""}${b.rate != null ? " " + rateText(b.rate) : ""}</span></summary>${brandForm(b)}</details>`).join("")}
      <details class="brand"><summary><span class="name">Add a brand</span><span class="muted">+</span></summary>${brandForm({})}</details>
    </div>
    <h2>Account</h2>
    <button id="logout">Log out</button>`;

  document.getElementById("rateForm").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = ev.target, rate = parseFloat(f.rate.value || "0");
    if (!Number.isFinite(rate) || rate < 0 || rate > 100) return toast("Enter a rate between 0 and 100");
    try { await api("/api/settings", { method: "PUT", json: { commission_rate: rate, commission_basis: f.basis.value } }); toast("Commission saved"); }
    catch (e) { toast(e.message); }
  };
  document.getElementById("brandList").onclick = async (ev) => {
    if (ev.target.dataset.act !== "saveBrand") return;
    const box = ev.target.closest(".body"), get = (k) => box.querySelector(`[data-k="${k}"]`);
    const split = (s) => s.split(",").map((w) => w.trim()).filter(Boolean);
    const rateText_ = get("rate").value.trim();
    const body = { name: get("name").value.trim(), aliases: split(get("aliases").value), requires: split(get("requires").value),
      rate: rateText_ === "" ? null : parseFloat(rateText_), active: get("active") ? get("active").checked : true };
    if (!body.name) return toast("Give the brand a name");
    if (body.rate !== null && !Number.isFinite(body.rate)) return toast("The rate must be a number");
    try {
      if (box.dataset.id) await api(`/api/brands/${box.dataset.id}`, { method: "PUT", json: body });
      else await api("/api/brands", { json: body });
      toast(box.dataset.id ? "Brand saved" : "Brand added");
      showSettings();
    } catch (e) { toast(e.message); }
  };
  document.getElementById("logout").onclick = async () => { await api("/api/logout", { method: "POST" }); me.logged_in = false; showLogin(); };
}

// ---------- start ----------
async function boot() {
  me = await api("/api/me");
  if (me.logged_in) brands = await api("/api/brands");
  route();
}
boot().catch((e) => { view.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; });
