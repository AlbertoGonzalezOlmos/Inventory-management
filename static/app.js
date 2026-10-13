/* HCRM frontend — Vue 3 SPA (no build step, served by FastAPI). */

const API_BASE = "/api";
const TOKEN_KEY = "hcrm_token";
const USER_KEY = "hcrm_user";
const PURCHASE_KEY = "hcrm_purchase";      // legacy: consumed once into the server cart
const STAGING_KEY = "hcrm_staged_scans";   // anonymous scans awaiting attribution (codes only)
const PAGE_SIZE = 24;

// Global HID scan capture (round 3, Task B): the decision logic — shape
// gates, timing constants and the HOLD/ARMED/WARM state machine — lives in
// static/scancapture.js (HcrmScanCapture, loaded BEFORE this file; pure,
// node-tested in tests/js/). This file keeps only the staging cap.
const STAGING_CAP = 50;

/** Minimal fetch wrapper with token + error handling.
 *
 * Options: {method, body, raw, token}.
 * - raw: true → resolves {status, data} and NEVER throws and NEVER runs the
 *   global 401 cleanup — the caller owns error handling entirely (used by
 *   the scan flow's status branching and the switch flow's best-effort
 *   foreign-token calls; a raw 401 is the caller's to act on).
 * - token: override the bearer (e.g. revoking the OLD session after a
 *   switch; foreign tokens never trigger the global 401 redirect either).
 * The default (throwing) path attaches err.status and err.detail (the
 * parsed JSON body) so structured 404/409 details reach the flows.
 */
async function api(path, { method = "GET", body, raw = false, token } = {}) {
  const foreign = token !== undefined;
  const bearer = foreign ? token : localStorage.getItem(TOKEN_KEY);
  const res = await fetch(API_BASE + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (raw) {
    let data = null;
    if (res.status !== 204) {
      try { data = await res.json(); } catch (_) { data = null; }
    }
    return { status: res.status, data };
  }

  if (res.status === 401 && !foreign && !path.startsWith("/auth/login") && !path.startsWith("/auth/qr-login")) {
    // Token expired or revoked: clear the session and force re-login.
    // (Login failures also return 401 but must surface their own message.)
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    window.dispatchEvent(new Event("hcrm-session-expired"));
    if (location.hash !== "#/login") location.hash = "#/login";
    throw Object.assign(new Error("Session expired — please log in again."), { status: 401 });
  }
  let data = null;
  if (res.status !== 204) {
    try { data = await res.json(); } catch (_) { data = null; }
  }
  if (res.status === 401) {
    let detail = "Invalid email or password";
    if (data && typeof data.detail === "string") detail = data.detail;
    throw Object.assign(new Error(detail), { status: 401, detail: data && data.detail });
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    if (data && typeof data.detail === "string") {
      detail = data.detail;
    } else if (data && Array.isArray(data.detail) && data.detail[0] && data.detail[0].msg) {
      // FastAPI validation errors: detail is a list of {loc, msg, ...}.
      const d = data.detail[0];
      const field = Array.isArray(d.loc) && d.loc.length > 1 ? d.loc[d.loc.length - 1] : null;
      detail = field ? `${field}: ${d.msg}` : d.msg;
    }
    throw Object.assign(new Error(detail), { status: res.status, detail: data ? data.detail : undefined });
  }
  if (res.status === 204) return null;
  return data;
}

const emptyItemForm = () => ({
  sku: "", name: "", description: "", category: "general",
  price: 0, stock: 0, image_url: "",
});

/** Columns of the data explorer table. */
const DATA_COLUMNS = [
  { key: "id", label: "ID" },
  { key: "sku", label: "SKU" },
  { key: "name", label: "Name" },
  { key: "category", label: "Category" },
  { key: "price_cents", label: "Price" },
  { key: "stock", label: "Stock" },
  { key: "description", label: "Description" },
  { key: "has_embedding", label: "Embedded" },
  { key: "created_at", label: "Created" },
];

Vue.createApp({
  data() {
    return {
      user: JSON.parse(localStorage.getItem(USER_KEY) || "null"),
      route: location.hash || "#/catalogue",
      busy: false,

      // auth forms
      loginForm: { email: "", password: "" },
      loginError: "",
      greeting: "",   // the "Hi <name>" login banner (round 3, Task C)
      badgeCode: "",  // the collapsed manual fallback field (Task E: the
                       // global capture is the primary no-click path)
      showBadgeField: false,  // "Use a badge code instead" toggle
      registerForm: { name: "", email: "", password: "" },
      registerError: "",

      // catalogue
      items: [],
      categories: [],
      search: "",
      category: "",
      semantic: false,
      loading: false,
      hasMore: false,
      offset: 0,
      // The server-side cart (Task 1): every mutation is its own committed
      // request, so the list is durable at every instant — the foundation
      // the badge account-switch flow relies on.
      cart: { lines: [], total_qty: 0, total_cents: 0 },
      detailItem: null,
      similar: [],

      // global scan capture (Task 5)
      scanCapture: false,        // flipped on when the keydown listener installs
      scanFlash: "",             // the scan-status pill
      scanQueue: [],             // scans drain one at a time
      stagedScans: [],           // anonymous scans awaiting attribution
      intakeQueue: [],           // unknown scans waiting for the intake modal
      scanIntake: null,          // the open intake modal's payload

      // kiosk logoff countdown (Task 4's server deadline, rendered)
      logoff: null,              // {name, deadline, remaining}

      // data explorer
      allColumns: DATA_COLUMNS,
      dataRows: [],
      dataPage: 0,
      dataLimit: 100,
      dataLoading: false,
      selectedRowIds: [],
      selectedCols: ["id", "sku", "name", "category", "price_cents", "stock"],
      rowAnchor: null,
      colAnchor: null,

      // admin
      adminTab: "items",
      itemForm: emptyItemForm(),
      itemEditingId: null,
      itemError: "",
      adminItems: [],
      memberForm: { name: "", email: "", password: "", role: "member" },
      memberError: "",
      members: [],
      scanFieldCode: "",       // the visible scan field (explicit operator intent)

      // account
      pwForm: { current_password: "", new_password: "" },
      pwError: "",
      pwOk: false,
      badgeSvg: "",      // freshly generated own badge (inline SVG markup)
      badgePayload: "",   // its one-time payload (shown only via the print modal)
      myRequests: [],    // "My scan requests" (GET /api/scan-requests/mine)

      // Task D — the tabbed "My profile" (#/account, no route churn):
      // profile | security | badge | purchases | requests. A flagged
      // account (must_change_password) is steered to "security" on arrival
      // — the redirect keeps working and lands one click from the fix.
      profileTab: "profile",
      emailForm: { new_email: "", current_password: "" },
      emailError: "",
      emailOk: false,
      phoneForm: { phone: "" },
      phoneError: "",
      myOrders: [],        // GET /api/orders/mine, newest first
      ordersLimit: 10,     // the single pagination knob (loadMoreOrders)
      expandedOrders: [],  // ids of receipts opened inline
      lastCheckout: null,  // the just-completed order id ("new" chip + pre-expand)
      lastReceipt: null,   // the checkout result modal { purchase, skipped }

      // Round 3, Task C — the purchase-history calendar (purchases tab):
      // per-UTC-day aggregates from GET /api/orders/calendar. year/month are
      // the ENDPOINT's truth (it defaults to the current UTC month); 0 = not
      // loaded. days maps "YYYY-MM-DD" → {cents, count}; only purchase days.
      calYear: 0,
      calMonth: 0,
      calDays: {},
      calTotalCents: 0,
      calLoading: false,

      // members: badge being displayed for printing
      memberBadge: null,  // { id, name, email, svg }

      // The badge print modal (creation, rotation, re-view): the payload
      // lives in memory only — never localStorage. Re-viewable since Task A:
      // a re-view re-fetches it via GET, so nothing here is "one-shot"
      // except the creation moment's urgency.
      badgePrint: null,   // { title, subtitle, svg, payload, warning }

      // admin: scan requests queue
      scanReqFilter: "open",
      scanRequests: [],

      // members: password reset is a modal (never window.prompt() — the
      // temporary password must not be typed/echoed in plaintext)
      pwResetMember: null,  // the member whose password is being reset
      pwResetValue: "",
      pwResetError: "",

      toast: "",
    };
  },

  computed: {
    isStaff() {
      return !!this.user && (this.user.role === "staff" || this.user.role === "admin");
    },
    currentView() {
      if (!this.user) return this.route === "#/register" ? "register" : "login";
      if (this.route.startsWith("#/admin") && this.isStaff) return "admin";
      if (this.route.startsWith("#/account")) return "account";
      if (this.route.startsWith("#/table") && this.isStaff) return "table";
      // The checkout review screen (round 3, item 4): the catalogue section
      // with the aside swapped to the review panel.
      if (this.route.startsWith("#/checkout")) return "checkout";
      return "catalogue";
    },
    purchaseTotalCents() {
      return this.cart.total_cents;
    },
    // The purchase-panel entries — the server cart's lines have the exact
    // shape the template always bound ({item, qty}), so the panel survives
    // the localStorage → API migration almost untouched.
    purchase() {
      return this.cart.lines;
    },
    purchaseCount() {
      return this.cart.total_qty;
    },

    /* ---------- Round 3, Task C: the purchase-history calendar ---------- */
    calendarCells() {
      // A Date.UTC month matrix, Monday-first, with leading blanks — the
      // keys are UTC dates ("YYYY-MM-DD") so they can never drift from the
      // endpoint's UTC-day bucketing (a local-midnight construction shifts
      // keys for users west of UTC).
      if (!this.calYear || !this.calMonth) return [];
      const y = this.calYear, m = this.calMonth - 1;
      const daysInMonth = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
      let lead = new Date(Date.UTC(y, m, 1)).getUTCDay() - 1;   // Monday = 0
      if (lead < 0) lead += 7;
      const cells = [];
      for (let i = 0; i < lead; i++) cells.push(null);
      const pad = (n) => String(n).padStart(2, "0");
      for (let d = 1; d <= daysInMonth; d++) {
        cells.push({ day: d, key: y + "-" + pad(m + 1) + "-" + pad(d) });
      }
      return cells;
    },
    calendarWeekdays() {
      // 2024-01-01 was a Monday: UTC-stable short labels, Monday-first.
      return Array.from({ length: 7 }, (_, i) =>
        new Date(Date.UTC(2024, 0, 1 + i))
          .toLocaleDateString(undefined, { weekday: "short", timeZone: "UTC" }));
    },
    calendarMinMax() {
      // min/max over the month's PER-DAY totals (one dot = one value).
      const vals = Object.values(this.calDays).map((d) => d.cents);
      if (!vals.length) return { min: 0, max: 0 };
      return { min: Math.min(...vals), max: Math.max(...vals) };
    },
    calIsCurrent() {
      if (!this.calYear) return false;
      const now = new Date();
      return this.calYear === now.getUTCFullYear()
        && this.calMonth === now.getUTCMonth() + 1;
    },
    calendarMonthLabel() {
      if (!this.calYear) return "";
      return new Date(Date.UTC(this.calYear, this.calMonth - 1, 1))
        .toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
    },
    calendarTotalLabel() {
      return this.calIsCurrent ? "Spent this month" : "Spent in " + this.calendarMonthLabel;
    },
    // The intake modal's suggestion groups (labelled pick-lists).
    intakeGroups() {
      if (!this.scanIntake) return {};
      const s = this.scanIntake.suggestions || {};
      return {
        by_prefix: { label: "Same barcode family", items: s.by_prefix || [] },
        by_keyword: { label: "Similar code or name", items: s.by_keyword || [] },
        by_semantic: { label: "Similar meaning", items: s.by_semantic || [] },
      };
    },

    /* ---------- data explorer ---------- */
    exportRows() {
      return this.selectedRowIds.length
        ? this.dataRows.filter((r) => this.selectedRowIds.includes(r.id))
        : this.dataRows;
    },
    exportCols() {
      return this.selectedCols.length
        ? this.allColumns.filter((c) => this.selectedCols.includes(c.key))
        : this.allColumns;
    },
    analysis() {
      const rows = this.exportRows;
      const prices = rows.map((r) => r.price_cents / 100);
      const stocks = rows.map((r) => r.stock);
      const sum = (a) => a.reduce((x, y) => x + y, 0);
      const mean = (a) => (a.length ? sum(a) / a.length : 0);
      const median = (a) => {
        if (!a.length) return 0;
        const s = [...a].sort((x, y) => x - y);
        const m = Math.floor(s.length / 2);
        return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
      };
      const std = (a) =>
        a.length ? Math.sqrt(sum(a.map((x) => (x - mean(a)) ** 2)) / a.length) : 0;

      const byCat = {};
      for (const r of rows) {
        byCat[r.category] = byCat[r.category] || { count: 0, valueCents: 0 };
        byCat[r.category].count += 1;
        byCat[r.category].valueCents += r.price_cents * r.stock;
      }
      const categories = Object.entries(byCat)
        .map(([name, v]) => ({ name, ...v }))
        .sort((a, b) => b.count - a.count);

      return {
        count: rows.length,
        priceMin: prices.length ? Math.min(...prices) * 100 : 0,
        priceMax: prices.length ? Math.max(...prices) * 100 : 0,
        priceMean: mean(prices) * 100,
        priceMedian: median(prices) * 100,
        priceStd: std(prices) * 100,
        stockSum: sum(stocks),
        stockMean: mean(stocks),
        stockMin: stocks.length ? Math.min(...stocks) : 0,
        stockMax: stocks.length ? Math.max(...stocks) : 0,
        inventoryValueCents: sum(rows.map((r) => r.price_cents * r.stock)),
        categories,
        maxCatCount: categories.length ? categories[0].count : 1,
      };
    },
  },

  watch: {
    search() {
      this.debouncedReload();
    },
    category() {
      this.reloadItems();
    },
    // The print-isolation body class: while a badge modal is open, printing
    // yields the badge card alone (the repo's first @media print block).
    badgePrint(on) { this._printBadge(!!on); },
    memberBadge(on) { this._printBadge(!!on); },
    currentView(view) {
      if (view === "catalogue" || view === "checkout") {
        if (this.items.length === 0) this.reloadItems();
        // The sentinel is recreated when the catalogue section re-mounts,
        // so the observer must be (re)attached every time we come back.
        this.$nextTick(() => this.observeSentinel());
      }
      if (view === "admin") this.loadAdminData();
      if (view === "table") this.loadDataPage();
      if (view === "account") {
        // A flagged account's mandatory change is the point of the visit:
        // land on the Security tab, one click from the fix. Everyone else
        // keeps their last tab.
        if (this.user && this.user.must_change_password) this.profileTab = "security";
        this.selectProfileTab(this.profileTab);
      }
    },
    profileTab(tab) {
      // The phone field always reflects the server truth on entry ("" = none).
      if (tab === "profile") this.phoneForm.phone = (this.user && this.user.phone) || "";
    },
  },

  methods: {
    /* ---------- helpers ---------- */
    formatPrice(cents) {
      return new Intl.NumberFormat(undefined, {
        style: "currency", currency: "EUR",
      }).format((cents || 0) / 100);
    },
    formatDate(iso) {
      return new Date(iso).toLocaleDateString();
    },
    showToast(msg) {
      this.toast = msg;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => (this.toast = ""), 3000);
    },
    debouncedReload() {
      clearTimeout(this._searchTimer);
      this._searchTimer = setTimeout(() => this.reloadItems(), 300);
    },
    observeSentinel() {
      const el = document.getElementById("sentinel");
      if (el && this._observer) this._observer.observe(el);
    },
    _printBadge(on) {
      document.body.classList.toggle("printing-badge", on);
    },

    /* ---------- auth ---------- */
    async doLogin() {
      this.loginError = "";
      this.busy = true;
      try {
        const data = await api("/auth/login", { method: "POST", body: this.loginForm });
        await this.afterLogin(data);
      } catch (e) {
        this.loginError = e.message;
      } finally {
        this.busy = false;
      }
    },

    /* Shared post-login wiring for EVERY session entry point (password
     * login, badge login, register, and the badge switch): save the
     * session, load the server cart (+ one-time legacy import), replay any
     * anonymously staged scans, then route. */
    async afterLogin(data) {
      this.saveSession(data);
      if (data.user.must_change_password) {
        // Server-side enforced: every other endpoint returns 403 until the
        // password is changed (the cart included — skip loading it).
        location.hash = "#/account";
        this.route = "#/account";
        this.showToast("Please change your password before continuing");
        return;
      }
      await this.loadCart();
      await this.replayStagedScans();
      location.hash = "#/catalogue";
      this.route = "#/catalogue";
      this.reloadItems();
      this.loadCategories();
    },

    async doBadgeLogin() {
      // QR badge login: the scanner (USB-HID mode) types the badge payload
      // into the field and its CR suffix submits the form. The global scan
      // bus (logged-out badge scans from anywhere) calls badgeLogin()
      // directly — this field keeps its dedicated behaviour.
      await this.badgeLogin(this.badgeCode.trim());
    },

    async badgeLogin(code) {
      if (!code) return;
      // Busy double-fire guard (Task E): a fast double-Enter (the collapsed
      // fallback + the global capture, or a shaky scan) must not fire two
      // qr-login requests — the second would mint a throwaway session.
      if (this.busy) return;
      this.loginError = "";
      this.busy = true;
      try {
        const data = await api("/auth/qr-login", {
          method: "POST", body: { token: code },
        });
        this.badgeCode = "";
        await this.afterLogin(data);
      } catch (e) {
        this.loginError = e.message;
      } finally {
        this.busy = false;
      }
    },

    async doRegister() {
      this.registerError = "";
      this.busy = true;
      try {
        const data = await api("/auth/register", {
          method: "POST", body: this.registerForm,
        });
        await this.afterLogin(data);
        // Task 2: the 201 carries the account's FIRST badge. Re-viewable
        // forever (Task A), so no "cannot be retrieved" claim — but keep
        // the creation-moment urgency to print it.
        if (data.qr_badge) {
          this.badgePrint = {
            title: "Your QR login badge",
            subtitle: "Welcome, " + data.user.name + "!",
            svg: data.qr_badge.svg,
            payload: data.qr_badge.payload,
            warning: "Anyone holding this code can log in as you — keep it " +
                     "private. Print it now while you have it.",
          };
        }
        // No "Welcome" toast here: the greeting banner from saveSession
        // (via afterLogin) carries it — a second toast would overwrite it.
      } catch (e) {
        this.registerError = e.message;
      } finally {
        this.busy = false;
      }
    },

    saveSession({ token, user }) {
      localStorage.setItem(TOKEN_KEY, token);
      localStorage.setItem(USER_KEY, JSON.stringify(user));
      this.user = user;
      // The login greeting (round 3, Task C): set at the ONE funnel every
      // session entry passes through (password, badge, register, switch).
      // Deliberately a banner, not a toast — afterLogin's legacy-cart import
      // and staged-scan replay TOAST, and would clobber it within
      // milliseconds on exactly the returning-kiosk paths where it matters.
      // It is NOT cleared in _closeOverlays (switchAccount calls saveSession
      // and _closeOverlays on adjacent lines — clearing there would kill it
      // on the one path that most needs it); logout and session-expiry clear
      // it explicitly, and the next saveSession overwrites it.
      this.greeting = "Hi " + user.name;
      clearTimeout(this._greetingTimer);
      this._greetingTimer = setTimeout(() => (this.greeting = ""), 4000);
    },

    dismissGreeting() {
      clearTimeout(this._greetingTimer);
      this.greeting = "";
    },

    _closeOverlays() {
      // Modals whose context belongs to a session that just ended (logout,
      // countdown expiry, account switch, session expiry), or to a view the
      // user just navigated away from: leaving them up strands a stale,
      // user-specific dialog over the login screen or the new account.
      // badgeSvg/badgePayload are the account view's rotated-badge display —
      // a credential, and the first thing to die with its session.
      this.detailItem = null;
      this.scanIntake = null;
      this.memberBadge = null;
      this.badgePrint = null;
      this.pwResetMember = null;
      this.lastReceipt = null;   // the checkout result — same stranding class
      this.badgeSvg = "";
      this.badgePayload = "";
      this.myRequests = [];
      // Purchase history is the PREVIOUS user's data (follow-up finding 1):
      // a shared-kiosk logout/switch must never show it, not even as a
      // flash before the tab's lazy load.
      this.myOrders = [];
      this.lastCheckout = null;
      this.expandedOrders = [];
      this.ordersLimit = 10;
      // The calendar is the previous user's data too (the myOrders rule):
      // never let one user's spend dots survive into the next session's tab.
      this.calYear = 0;
      this.calMonth = 0;
      this.calDays = {};
      this.calTotalCents = 0;
      // Sensitive form fields die with the session too (follow-up finding
      // 5): a password typed into the email-change form must not survive a
      // logout for the next kiosk user to submit.
      this.emailForm = { new_email: "", current_password: "" };
      this.emailError = "";
      this.emailOk = false;
      this.phoneForm = { phone: "" };
      this.phoneError = "";
      this.pwForm = { current_password: "", new_password: "" };
      this.loginForm = { email: "", password: "" };
    },

    async logout() {
      this.clearLogoff();
      this.dismissGreeting();   // the banner dies with its session
      this._closeOverlays();
      try { await api("/auth/logout", { method: "POST" }); } catch (_) { /* ignore */ }
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      this.user = null;
      this.cart = { lines: [], total_qty: 0, total_cents: 0 };
      location.hash = "#/login";
      this.route = "#/login";
    },

    /* ---------- account ---------- */
    async changePassword() {
      this.pwError = "";
      this.pwOk = false;
      this.busy = true;
      try {
        await api("/auth/change-password", { method: "POST", body: this.pwForm });
        this.pwForm = { current_password: "", new_password: "" };
        this.pwOk = true;
        if (this.user && this.user.must_change_password) {
          this.user.must_change_password = false;
          localStorage.setItem(USER_KEY, JSON.stringify(this.user));
        }
        this.showToast("Password updated — other sessions were signed out");
      } catch (e) {
        this.pwError = e.message;
      } finally {
        this.busy = false;
      }
    },

    async generateMyBadge() {
      // (Re)generate the own QR login badge; replaces any existing one.
      this.busy = true;
      try {
        const data = await api("/auth/qr-badge", { method: "POST" });
        this.badgeSvg = data.svg;
        // The rotation's one-time payload: kept so Print opens the isolated
        // badge modal with it (never rendered inline on the account page).
        this.badgePayload = data.payload;
        if (this.user) {
          this.user.has_qr_badge = true;
          localStorage.setItem(USER_KEY, JSON.stringify(this.user));
        }
        this.showToast("QR badge generated — print it and keep it safe");
      } catch (e) {
        this.showToast(e.message);
      } finally {
        this.busy = false;
      }
    },

    printAccountBadge() {
      // Print isolation for the account view's rotated badge (Task 6): route
      // it through the badgePrint modal, which carries the
      // .printing-badge body class + @media print isolation — printing the
      // account page directly would print the whole page around the card.
      if (!this.badgeSvg) return;
      this.badgePrint = {
        title: "Your QR login badge",
        subtitle: "Rotated just now — the previous badge stopped working",
        svg: this.badgeSvg,
        payload: this.badgePayload || "",
        warning: "Anyone holding this code can log in as you — keep it private.",
      };
    },

    async revokeMyBadge() {
      if (!confirm("Revoke your QR badge? It will stop working immediately.")) return;
      try {
        await api("/auth/qr-badge", { method: "DELETE" });
        this.badgeSvg = "";
        this.badgePayload = "";
        if (this.user) {
          this.user.has_qr_badge = false;
          localStorage.setItem(USER_KEY, JSON.stringify(this.user));
        }
        this.showToast("QR badge revoked");
      } catch (e) {
        this.showToast(e.message);
      }
    },

    /* ---------- catalogue ---------- */
    async loadCategories() {
      try {
        this.categories = await api("/items/categories");
      } catch (_) { /* non-critical */ }
    },

    toggleSemantic() {
      this.semantic = !this.semantic;
      this.reloadItems();
    },

    async reloadItems() {
      this.offset = 0;
      this.items = [];
      this.hasMore = true;
      await this.loadMore();
    },

    async loadMore() {
      if (this.loading || !this.hasMore) return;
      this.loading = true;
      try {
        const params = new URLSearchParams({
          limit: PAGE_SIZE, offset: this.offset,
        });
        const term = this.search.trim();
        // The backend requires q to be at least 2 characters for semantic
        // search; a single character falls back to keyword search.
        if (this.semantic && term.length >= 2) {
          // Semantic (vector) search — ranked by meaning similarity.
          params.set("q", term);
          if (this.category) params.set("category", this.category);
          const page = await api("/items/vector-search?" + params.toString());
          this.items.push(...page);
          this.offset += page.length;
          this.hasMore = page.length === PAGE_SIZE;
        } else {
          if (term) params.set("q", term);
          if (this.category) params.set("category", this.category);
          const page = await api("/items?" + params.toString());
          this.items.push(...page);
          this.offset += page.length;
          this.hasMore = page.length === PAGE_SIZE;
        }
      } catch (e) {
        this.showToast(e.message);
        this.hasMore = false;
      } finally {
        this.loading = false;
      }
    },

    async openDetail(item) {
      this.detailItem = item;
      this.similar = [];
      try {
        const res = await api(
          "/items/vector-search?q=" +
          encodeURIComponent(item.name + " " + (item.description || "")) +
          "&limit=5"
        );
        this.similar = res.filter((s) => s.id !== item.id).slice(0, 3);
      } catch (_) { /* non-critical */ }
    },
    closeDetail() { this.detailItem = null; this.similar = []; },

    /* ---------- server cart (Task 1: every mutation returns the full
     * CartOut — one payload, no read-after-write; ops run through a
     * serialization chain so rapid clicks/scans cannot interleave) ---------- */

    _cartOp(fn) {
      this._cartChain = (this._cartChain || Promise.resolve()).then(fn, fn);
      return this._cartChain;
    },

    async flushCart() {
      // The save-then-switch flush: every mutation is already a committed
      // server write — flushing is draining the chain, not persisting.
      await this._cartChain;
    },

    async loadCart() {
      if (!this.user || this.user.must_change_password) return; // 403-gated
      this.cart = await api("/cart");
      await this.importLegacyPurchase();
    },

    async importLegacyPurchase() {
      // ONE-TIME migration of the pre-server-cart localStorage list: PUT its
      // lines (only into an empty server cart — a returning user's durable
      // cart must never be clobbered by a stale browser copy), then the key
      // dies regardless, so this can never double-run.
      const raw = localStorage.getItem(PURCHASE_KEY);
      if (!raw) return;
      localStorage.removeItem(PURCHASE_KEY);
      if (this.cart.lines.length) return;
      try {
        const entries = JSON.parse(raw);
        const lines = (Array.isArray(entries) ? entries : [])
          .filter(e => e && e.item && Number.isInteger(e.item.id) && e.qty >= 1)
          .map(e => ({ item_id: e.item.id, qty: Math.min(10000, e.qty | 0) }));
        if (lines.length) {
          try {
            this.cart = await api("/cart", { method: "PUT", body: { lines } });
            this.showToast(`Imported ${lines.length} item(s) from your old purchase list`);
          } catch (e) {
            if (e.status === 404 || e.status === 422) {
              // A deleted catalogue item poisons the all-or-nothing PUT:
              // fall back to per-line adds so the VALID lines survive,
              // skipping only what no longer exists.
              for (const line of lines) {
                try {
                  this.cart = await api("/cart/items", { method: "POST", body: line });
                } catch (_) { /* item gone from the catalogue */ }
              }
              this.showToast("Imported your old purchase list (some items are gone)");
            } // transient errors: the import is one-time by design
          }
        }
      } catch (_) { /* malformed legacy data: the key is already removed */ }
    },

    _stockWarn(line) {
      if (line && line.qty > line.item.stock) {
        this.showToast("Only " + line.item.stock + " in stock");
      }
    },

    addToPurchase(item) {
      return this._cartOp(async () => {
        this.cart = await api("/cart/items", {
          method: "POST", body: { item_id: item.id, qty: 1 },
        });
        const line = this.cart.lines.find(l => l.item.id === item.id);
        this._stockWarn(line);
        this.showToast(item.name + " added to your purchase list");
        this.closeDetail();
      });
    },
    incPurchase(id) {
      return this._cartOp(async () => {
        this.cart = await api("/cart/items", {
          method: "POST", body: { item_id: id, qty: 1 },
        });
        this._stockWarn(this.cart.lines.find(l => l.item.id === id));
      });
    },
    decPurchase(id) {
      return this._cartOp(async () => {
        const line = this.cart.lines.find(l => l.item.id === id);
        if (!line) return;
        if (line.qty <= 1) {
          this.cart = await api("/cart/items/" + id, { method: "DELETE" });
        } else {
          this.cart = await api("/cart/items/" + id, {
            method: "PATCH", body: { qty: line.qty - 1 },
          });
        }
      });
    },
    removeFromPurchase(id) {
      return this._cartOp(async () => {
        this.cart = await api("/cart/items/" + id, { method: "DELETE" });
      });
    },
    clearPurchase() {
      return this._cartOp(async () => {
        this.cart = await api("/cart", { method: "DELETE" });
      });
    },
    /* ---------- Task D: checkout + profile tab ---------- */

    selectProfileTab(tab) {
      this.profileTab = tab;
      if (tab === "purchases") {
        this.loadMyOrders();
        this.loadCalendar();   // round 3, Task C: the spend calendar's data
      }
      if (tab === "requests") this.loadMyRequests();
      if (tab === "profile")
        this.phoneForm.phone = (this.user && this.user.phone) || "";
    },

    async submitEmailChange() {
      this.emailError = "";
      this.emailOk = false;
      this.busy = true;
      try {
        const user = await api("/auth/change-email", {
          method: "POST", body: this.emailForm,
        });
        // Refresh the cached session user (the changePassword pattern) so
        // the SPA never shows a stale login identifier.
        this.user = user;
        localStorage.setItem(USER_KEY, JSON.stringify(user));
        this.emailForm = { new_email: "", current_password: "" };
        this.emailOk = true;
        this.showToast("Email updated — you were signed out on other devices");
      } catch (e) {
        this.emailError = e.message;   // 400 wrong-password/no-op, 409 taken, 422 shape
      } finally {
        this.busy = false;
      }
    },

    async submitPhoneChange() {
      this.phoneError = "";
      this.busy = true;
      try {
        const user = await api("/auth/change-phone", {
          method: "POST", body: { phone: this.phoneForm.phone },
        });
        this.user = user;
        localStorage.setItem(USER_KEY, JSON.stringify(user));
        this.showToast(user.phone ? "Phone saved" : "Phone cleared");
      } catch (e) {
        this.phoneError = e.message;   // 422: the normaliser's shape message
      } finally {
        this.busy = false;
      }
    },

    _handleBadgeView(r, title, subtitle, isSelf = true) {
      /* ONE envelope handler for the self re-view and the staff view
       * (Task A's status convention — raw mode, branch on the
       * discriminator). Returns true when the modal was shown. The messages
       * are parameterised for self vs staff (follow-up finding 4): "you"
       * guidance would be wrong when staff are looking at a member. */
      if (r.status === 401) {
        // Raw mode skips the wrapper's global 401 cleanup by design — the
        // caller owns it (the Task-5 contract every raw site follows).
        this.handleSessionExpired();
        return false;
      }
      if (r.status === 403) {
        // Self: the flagged-account gate (change the password first).
        // Staff: the manage rule (staff never view an admin's badge).
        this.showToast(isSelf
          ? "Change your password before using the scanner"
          : "Cannot view this member's badge");
        return false;
      }
      if (r.status === 200 && r.data) {
        this.badgePrint = {
          title, subtitle,
          svg: r.data.svg,
          payload: r.data.payload,
          warning: r.data.migrated
            ? "Your PREVIOUS printed badge stopped working when you viewed " +
              "this one — print the new card now and replace it."
            : "Anyone holding this code can log in as you — keep it private.",
        };
        return true;
      }
      const s = r.data && r.data.detail && r.data.detail.status;
      if (r.status === 404 || s === "none") {
        this.showToast(isSelf
          ? "You have no badge yet — generate one below"
          : "This member has no badge yet — they can generate one from their profile");
      } else if (s === "no_key") {
        this.showToast("Badge viewing is unavailable (no key) — logins are " +
                       "unaffected; ask an admin to restore the badge key");
      } else if (s === "key_mismatch") {
        this.showToast("The badge key changed — regenerate your badge to " +
                       "re-enable viewing (logins are unaffected)");
      } else {
        this.showToast("Could not view the badge");
      }
      return false;
    },

    async showMyBadge() {
      const r = await api("/auth/qr-badge", { raw: true });
      this._handleBadgeView(r, "Your QR badge", this.user ? this.user.name : "");
    },

    async viewMemberBadge(member) {
      const r = await api("/members/" + member.id + "/qr-badge", { raw: true });
      this._handleBadgeView(r, "QR badge — " + member.name, member.email, false);
    },

    async loadMyOrders() {
      if (!this.user || this.user.must_change_password) {
        this.myOrders = [];   // never the previous user's history, even briefly
        return;
      }
      try {
        this.myOrders = await api("/orders/mine?limit=" + this.ordersLimit);
      } catch (_) { /* non-critical */ }
    },

    /* ---------- Round 3, Task C: the spend calendar's data ---------- */

    async loadCalendar(year, month) {
      // Bare call = the endpoint's default (the current UTC month), and the
      // RESPONSE's year/month is the truth the grid renders — the keys and
      // the bucketing can never disagree.
      if (!this.user || this.user.must_change_password) {
        this.calYear = 0; this.calMonth = 0;
        this.calDays = {}; this.calTotalCents = 0;
        return;
      }
      this.calLoading = true;
      try {
        const params = new URLSearchParams();
        if (year !== undefined) { params.set("year", year); params.set("month", month); }
        const qs = params.toString();
        const data = await api("/orders/calendar" + (qs ? "?" + qs : ""));
        this.calYear = data.year;
        this.calMonth = data.month;
        this.calDays = data.days || {};
        this.calTotalCents = data.total_cents || 0;
      } catch (_) {
        // non-critical: an empty calendar, never a broken tab
        this.calDays = {};
        this.calTotalCents = 0;
      } finally {
        this.calLoading = false;
      }
    },

    shiftCalendarMonth(delta) {
      let y = this.calYear, m = this.calMonth + delta;
      if (m < 1) { m = 12; y -= 1; }
      if (m > 12) { m = 1; y += 1; }
      this.loadCalendar(y, m);
    },

    calendarDay(key) {
      return this.calDays[key] || null;
    },

    dayColor(cents) {
      // The month's min-max ramp: t=0 (the min-spend day — the first
      // purchase of a single-purchase month included) is light green,
      // t=1 is red; the hue path runs through yellow/orange, which reads
      // as a heat ramp (a 2-stop RGB lerp goes muddy brown in the middle).
      const { min, max } = this.calendarMinMax;
      const t = max > min ? (cents - min) / (max - min) : 0;
      return "hsl(" + Math.round(120 * (1 - t)) + ", 73%, " +
        Math.round(75 - 25 * t) + "%)";
    },

    isCalendarToday(key) {
      // UTC today — the same clock the endpoint buckets by.
      return key === new Date().toISOString().slice(0, 10);
    },

    async loadMoreOrders() {
      // The single pagination knob (Task D spec): one method, one bound —
      // capped at the backend's Query(le=100): asking for more would 422
      // into the silent catch and leave the button visibly dead.
      if (this.ordersLimit >= 100) return;
      this.ordersLimit = Math.min(100, this.ordersLimit + 10);
      await this.loadMyOrders();
    },

    toggleOrder(id) {
      this.expandedOrders = this.expandedOrders.includes(id)
        ? this.expandedOrders.filter((i) => i !== id)
        : this.expandedOrders.concat([id]);
    },

    /* ---------- Round 3, Task C: the checkout review screen ----------
     * No confirm() anywhere: the review screen IS the confirmation. The
     * server cart is the review state — every edit is an ordinary committed
     * _cartOp mutation, exactly as durable as in the sidebar. */

    goToCheckout() {
      // Both assignments in one synchronous turn so the hashchange handler's
      // `navigated` guard stays false and does not fire _closeOverlays.
      this.route = "#/checkout";
      location.hash = "#/checkout";
    },

    keepShopping() {
      // The cart is durable server-side — leaving needs no save, no prompt.
      this.route = "#/catalogue";
      location.hash = "#/catalogue";
    },

    setLineQty(id, ev) {
      // The review screen's direct quantity box: an ABSOLUTE set via PATCH
      // (qty 0 removes the line — the endpoint's semantics). Invalid input
      // or a rejected edit reverts the field to the server truth.
      const el = ev && ev.target;
      const raw = el ? el.value : ev;
      return this._cartOp(async () => {
        const n = parseInt(raw, 10);
        if (!Number.isFinite(n) || !Number.isInteger(Number(raw)) || n < 0) {
          this.showToast("Enter a whole number of units (0 removes the line)");
          await this.loadCart();
        } else {
          try {
            this.cart = await api("/cart/items/" + id, {
              method: "PATCH", body: { qty: n },
            });
            if (n > 0) this._stockWarn(this.cart.lines.find(l => l.item.id === id));
          } catch (e) {
            this.showToast(e.message);   // 422 (qty > 10000), 404 (line gone)
            await this.loadCart();
          }
        }
        // Re-sync the box explicitly: Vue's :value binding only re-patches
        // when the qty CHANGED, so a rejected edit that leaves the server
        // qty as displayed would keep the typed garbage visible.
        const line = this.cart.lines.find(l => l.item.id === id);
        if (el && el.isConnected && line) el.value = line.qty;
      });
    },

    async donePurchase() {
      if (this.busy) return;
      if (!this.purchase.length) { this.showToast("Your purchase list is empty"); return; }
      this.busy = true;   // also drives the button's :disabled binding —
      try {               // without it a double-click fires a second POST
        // Serialized with every other cart mutation (the _cartOp chain):
        // a scan landing mid-checkout cannot interleave between the
        // server-side cart read and the cart clear.
        await this._cartOp(async () => {
          const r = await api("/orders", { method: "POST", raw: true });
          if (r.status === 401) { this.handleSessionExpired(); return; }
          if (r.status === 201 && r.data) {
            this.cart = r.data.cart;                    // the emptied cart, one payload
            const pid = r.data.purchase.id;
            this.lastCheckout = pid;                    // "new" chip in history
            // Land on the purchases tab. route + hash in one synchronous
            // turn (the hashchange `navigated` guard); the currentView
            // watcher then fires selectProfileTab("purchases"), which loads
            // the orders AND the calendar (today's dot appears at once).
            this.profileTab = "purchases";
            this.route = "#/account";
            location.hash = "#/account";
            this.expandedOrders = [pid];                 // pre-expand the receipt
            this.showToast("Purchase complete — " +
              this.formatPrice(r.data.purchase.total_cents));
            // The receipt modal is EXCEPTION-ONLY (clamp-not-veto must stay
            // reportable, but a clean checkout gets no blocking dialog):
            // skipped orphans and clamped lines are the cases the history
            // row alone does not surface loudly enough.
            const skipped = r.data.skipped || [];
            const clamped = r.data.purchase.lines.some((l) => l.clamped);
            if (skipped.length || clamped) this.lastReceipt = r.data;
            return;
          }
          if (r.status === 400) { this.showToast("Cart is empty"); return; }
          this.showToast("Checkout failed (" + r.status + ")");
        });
      } finally {
        this.busy = false;
      }
    },

    /* ---------- global HID scan capture + the handleScan bus ----------
     * The DECISIONS live in the pure reducer (HcrmScanCapture.burstStep,
     * static/scancapture.js — node-tested). This adapter owns everything
     * DOM: preventDefault, the SCAN_DEFER_MS timer (re-armed on every held
     * key), the per-type snapshots, the splice/replay write paths, and the
     * routing bus (enqueueScan — completely unchanged). Invariants: a
     * shape-gated burst never writes to any field; human typing is native
     * after the first char of a run; sinks are untouched; unshaped bursts
     * are inert; no keystroke is ever lost (prevented chars are routed or
     * replayed). */

    installScanCapture() {
      this._scanState = HcrmScanCapture.initialState();
      this._scanSnap = null;       // the live per-type snapshot (retract/restore)
      this._scanHeldEl = null;     // the element a prevented run must write back into
      this._scanTimer = null;      // the SCAN_DEFER_MS human-verdict timer
      document.addEventListener("keydown", this._onScanKey, true);
      // A pointer, a focus change or a paste is a verifiable human signal:
      // anything still held is human text and must be written before the
      // world moves on (the reducer treats these like the timer).
      document.addEventListener("pointerdown", this._onScanExternal, true);
      document.addEventListener("focusin", this._onScanExternal, true);
      document.addEventListener("paste", this._onScanExternal, true);
      // An IME composition is the one human signal the keydown guard can miss:
      // some platforms do not set isComposing/keyCode 229 on the FIRST composing
      // keydown, and Vue's v-model ignores an `input` event while the element is
      // composing (vModelText's handler opens with `if (target.composing)
      // return`) — so a replay landing mid-composition would update the DOM and
      // NOT the model. This listener is on `document` in the CAPTURE phase while
      // Vue's is on the element, so the flush always runs first, before
      // `composing` is ever set. (Round-3 audit fix 1, r3rev-qwen.md §9.4.)
      document.addEventListener("compositionstart", this._onScanExternal, true);
      this.scanCapture = true;     // renders data-scan-capture="on" (ui_check pin)
    },

    _scanNow() {
      return performance.now();
    },

    /** The adapter's target classification — decides the write path.
     * "text": selection-API inputs + textarea (setRangeText splice).
     * "value": number/email/date inputs — selectionStart THROWS there, so
     *          writes are value-only (append + coercion check).
     * "select": typeahead must never be prevented for humans — pass
     *           through, snapshot selectedIndex, restore on armed runs.
     * "plain": non-editable targets and contentEditable — pass-through,
     *           never hijacked (nothing spliceable; nothing to protect). */
    _scanClass(target) {
      const t = target;
      if (!t || !t.tagName) return "plain";
      const tag = t.tagName;
      if (tag === "TEXTAREA") return "text";
      if (tag === "INPUT") {
        const type = (t.getAttribute("type") || "text").toLowerCase();
        if (["text", "search", "tel", "url", "password", ""].includes(type)) return "text";
        if (["email", "number", "date", "time", "month", "week", "datetime-local"].includes(type)) {
          return "value";
        }
        return "plain";   // checkbox, radio, file, button, submit, color, range…
      }
      if (tag === "SELECT") return "select";
      return "plain";
    },

    /** The credential targets of _scanStrictPre: any password field, or the
     * username field of a login form — in this SPA that is the autofocused
     * `type="text" autocomplete="username"` input on the login screen
     * (index.html:55-56), i.e. exactly the field a badge scan lands in when
     * nobody has clicked anywhere. A selector test, so extending the rule is a
     * selector edit and not a code edit. */
    _scanSecretField(el) {
      return !!(el && el.matches &&
        el.matches('input[type="password"], input[autocomplete="username"]'));
    },

    /** Credential fields never enter WARM (round-3 audit fix 2 —
     * r3rev-qwen.md §9.4; plan D1c).
     *
     * WARM lets printable keys land natively and relies on a retract at
     * burst-confirm, which bounds a scan's visible artifact to ONE character
     * for ≤1 frame. Everywhere else that is a non-event; in a password field or
     * in the login username field that character is a fragment of a bearer
     * credential, and the bus's own doctrine is "NEVER render the raw badge
     * payload on screen" (enqueueScan). These targets therefore always take the
     * held path — SCAN_STRICT_DEFER scoped to the two fields where it matters,
     * with no latency cost anywhere else.
     *
     * The reducer cannot know this (it is element-agnostic and node-tested with
     * no DOM), so instead of reimplementing any of its branches the adapter
     * arranges for it to take its OWN strict path:
     *   1. a warm window inherited from ANOTHER field is dropped first — the
     *      reducer's WARM carries no element identity, so without this a key
     *      here could take the `inWarm && !fast` branch and land natively;
     *   2. a pending run is flushed BEFORE a slow printable key, so the reducer
     *      sees IDLE and holds the key. Its `runActive && !fast` branch would
     *      otherwise replay the run and let THIS key land natively — painting
     *      it, and dropping it from the logical buffer, which is precisely how
     *      a scan loses its first character (the failure mode the reducer's own
     *      "Run start" comment warns about).
     * `_scanEvent` then demotes any WARM verdict these two cannot prevent. */
    _scanStrictPre(ev, e) {
      if (!this._scanSecretField(e && e.target)) return;
      const s = this._scanState;
      if (s.mode === "warm") this._scanState = HcrmScanCapture.initialState(ev.t);
      const pending = s.mode === "hold" || s.mode === "armed";
      const printable = typeof ev.key === "string" && ev.key.length === 1;
      const slow = ev.t - s.lastT >= HcrmScanCapture.SCAN_MAX_GAP_MS;
      if (pending && printable && !ev.repeat && !ev.composing && !ev.mod
          && (slow || s.cls !== ev.cls)) {
        // The human verdict, early: identical to the SCAN_DEFER_MS timer firing
        // now (replay what is held, no WARM — see the demotion in _scanEvent).
        this._scanEvent({ type: "timer", t: ev.t }, null);
      }
    },

    _onScanKey(e) {
      // Sinks own their keys entirely (the login badge field and the visible
      // scan field keep their dedicated Enter handlers — no double-fire;
      // focus changes into them flush via the focusin listener).
      if (e.target && e.target.closest && e.target.closest("[data-scan-sink]")) {
        return;
      }
      const ev = {
        type: "key",
        key: e.key,
        t: this._scanNow(),
        cls: this._scanClass(e.target),
        repeat: e.repeat === true,
        composing: e.isComposing === true || e.keyCode === 229,
        mod: e.ctrlKey || e.metaKey || e.altKey,
      };
      this._scanStrictPre(ev, e);
      this._scanEvent(ev, e);
    },

    _onScanExternal() {
      this._scanEvent({ type: "external", t: this._scanNow() });
    },

    _scanEvent(ev, e) {
      const result = HcrmScanCapture.burstStep(this._scanState, ev);
      // Credential-field strict mode, step 2 of 2 (_scanStrictPre is step 1):
      // the reducer's WARM verdict is element-agnostic, so demote it to idle
      // here — the next printable key in this field is then held instead of
      // landing natively. A timer/external event carries no target, so the
      // pending run's own element decides (it is still set: the write-back
      // below is what clears it).
      const secret = ev.type === "key"
        ? this._scanSecretField(e && e.target)
        : this._scanSecretField(this._scanHeldEl);
      this._scanState = (secret && result.state.mode === "warm")
        ? HcrmScanCapture.initialState(ev.t)
        : result.state;
      for (const action of result.actions) this._applyScanAction(action, e);
      // The defer timer: armed exactly while a run is pending, RE-ARMED on
      // every key (a burst whose source dies mid-payload cannot hang the
      // held chars) — and it can never fire mid-scan, because
      // SCAN_DEFER_MS (60) > SCAN_MAX_GAP_MS (50) > scanner inter-key.
      // Read the DEMOTED state: the timer must agree with what we stored.
      if (this._scanTimer) { clearTimeout(this._scanTimer); this._scanTimer = null; }
      if (this._scanState.mode === "hold" || this._scanState.mode === "armed") {
        this._scanTimer = setTimeout(
          () => this._scanEvent({ type: "timer", t: this._scanNow() }),
          HcrmScanCapture.SCAN_DEFER_MS);
      }
    },

    _applyScanAction(action, e) {
      switch (action.type) {
        case "prevent":
          if (e) {
            e.preventDefault();
            // The write-back target of the run being prevented (the focus
            // cannot move mid-run without a focusin flush first).
            this._scanHeldEl = e.target;
          }
          break;
        case "native":
          // No preventDefault: the key lands. The snapshot must be taken
          // NOW — keydown precedes the input event it causes.
          if (action.snap && e) this._scanTakeSnapshot(e.target);
          break;
        case "replay":
        case "flush":
          this._scanWriteBack(action.buffer, action.cls);
          break;
        case "restore":
          this._scanRestoreSelect();
          break;
        case "retract":
          this._scanRetract();
          break;
        case "route": {
          if (e) e.preventDefault();
          if (action.restore) this._scanRestoreSelect();
          this._scanSnap = null;
          this._scanHeldEl = null;
          this.enqueueScan(action.code, action.kind);   // the unchanged bus
          break;
        }
      }
    },

    _scanTakeSnapshot(target) {
      // The pre-key state a retract/restore returns to.
      const cls = this._scanClass(target);
      if (cls === "text") {
        let start = null, end = null;
        try { start = target.selectionStart; end = target.selectionEnd; } catch (_) { /* not reachable for the text class */ }
        this._scanSnap = { el: target, value: target.value, start, end };
      } else if (cls === "value") {
        this._scanSnap = { el: target, value: target.value };
      } else if (cls === "select") {
        this._scanSnap = { el: target, selectedIndex: target.selectedIndex };
      }
    },

    _scanRetract() {
      // A burst confirmed inside a warm window: undo the char(s) that
      // landed natively since the snapshot (the bounded 1-char/1-frame
      // artifact — the price of zero-latency human typing).
      const snap = this._scanSnap;
      this._scanSnap = null;
      if (!snap || !snap.el || !snap.el.isConnected) return;
      if (snap.start !== null && snap.start !== undefined) {
        snap.el.value = snap.value;
        try { snap.el.setSelectionRange(snap.start, snap.end === null ? snap.start : snap.end); } catch (_) { /* keep the value restore */ }
        snap.el.dispatchEvent(new Event("input", { bubbles: true }));
      } else if (snap.selectedIndex !== undefined) {
        snap.el.selectedIndex = snap.selectedIndex;
        snap.el.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        snap.el.value = snap.value;
        snap.el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    },

    _scanRestoreSelect() {
      // The select's undo path: v-model on <select> listens to `change`,
      // not `input` — dispatching the wrong one leaves Vue's model desynced
      // (a latent defect of the old restore code, fixed here).
      const snap = this._scanSnap;
      if (!snap || !snap.el || !snap.el.isConnected || snap.selectedIndex === undefined) return;
      snap.el.selectedIndex = snap.selectedIndex;
      snap.el.dispatchEvent(new Event("change", { bubbles: true }));
    },

    _scanWriteBack(buffer, cls) {
      // The abort path: the held chars were never allowed to land — write
      // them back now, caret-preserving and v-model-safe. The caret is
      // RE-READ at replay time (the stale-snapshot fallback): if the
      // element is gone, the buffer is dropped, never thrown elsewhere.
      const el = this._scanHeldEl;
      this._scanHeldEl = null;
      if (!el || !el.isConnected) return;
      if (cls === "text") {
        let start = null, end = null;
        try { start = el.selectionStart; end = el.selectionEnd; } catch (_) { /* not reachable for the text class */ }
        if (start === null) {
          el.value = (el.value || "") + buffer;
        } else {
          el.setRangeText(buffer, start, end === null ? start : end, "end");
        }
        el.dispatchEvent(new Event("input", { bubbles: true }));
      } else if (cls === "value") {
        // Value-only splice: no selection API on these inputs, so append —
        // with the coercion check ("validate"): a browser rejects a value
        // it cannot represent for the type (number/date) by emptying it.
        const base = el.value || "";
        el.value = base + buffer;
        if (el.value === "" && (base + buffer) !== "") el.value = base;
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
      // select/plain: nothing to splice (the select's undo is the restore;
      // plain targets never had their keys prevented)
    },

    scanFieldSubmit() {
      // The visible scan field (catalogue header): explicit operator intent,
      // no heuristics — also the deterministic path for payloads the global
      // gate deliberately ignores (short internal codes). Routing uses the
      // same badge shapes as the global gate (full AND raw-43 forms — both
      // are accepted by qr-login/qr-identify), but no length/charset floor.
      const code = (this.scanFieldCode || "").trim();
      this.scanFieldCode = "";
      if (!code) return;
      const kind = HcrmScanCapture.isBadgeShape(code) ? "badge" : "barcode";
      this.enqueueScan(code, kind);
    },

    enqueueScan(code, kind) {
      // NEVER render the raw badge payload on screen: it is a bearer
      // credential, shoulder-surfable for the pill's 2 s lifetime. Mask on
      // the ROUTED KIND (a prefix-stripped presentation is just as much a
      // secret as an HCRM1:-prefixed one). Barcodes/SKUs are not secrets
      // and stay visible — silent misroutes must be observable.
      this.scanFlash = kind === "badge" ? "scan: badge received" : "scan: " + code;
      clearTimeout(this._scanFlashTimer);
      this._scanFlashTimer = setTimeout(() => (this.scanFlash = ""), 2000);
      this.scanQueue.push({ code, kind });
      this.drainScanQueue();
    },

    async drainScanQueue() {
      if (this._scanBusy) return;
      this._scanBusy = true;
      try {
        while (this.scanQueue.length) {
          const { code, kind } = this.scanQueue.shift();
          try {
            if (kind === "badge") await this.handleBadgeScan(code);
            else await this.handleBarcodeScan(code);
          } catch (e) {
            this.showToast(e.message);
          }
        }
      } finally {
        this._scanBusy = false;
      }
    },

    _modalOpen() {
      return !!(this.detailItem || this.memberBadge || this.badgePrint
        || this.pwResetMember || this.scanIntake || this.lastReceipt);
    },

    async handleBadgeScan(code) {
      if (!this.user) {
        await this.badgeLogin(code);
        return;
      }
      // "Is this MY badge?" — a pure predicate; the switch mints a session
      // only when it actually wants one.
      const { status, data } = await api("/auth/qr-identify", {
        method: "POST", body: { token: code }, raw: true,
      });
      if (status === 403) {
        this.showToast("Change your password before using the scanner");
        return;
      }
      if (status === 401) { this.handleSessionExpired(); return; }
      if (status !== 200 || !data) { this.showToast("Badge check failed"); return; }
      if (data.same_user) {
        await this.armLogoff();
      } else {
        await this.switchAccount(code);
      }
    },

    async handleBarcodeScan(code) {
      // While a modal is open (except the logoff overlay), product barcodes
      // are dropped — finish the current dialog first. Badges always
      // process: a second person at the counter is never ignored.
      if (this._modalOpen()) {
        this.showToast("Finish the current dialog first");
        return;
      }
      if (!this.user) {
        this.stageScan(code);
        return;
      }
      const { status, data } = await api("/cart/scan", {
        method: "POST", body: { code, symbology: "", qty: 1 }, raw: true,
      });
      if (status === 401) { this.handleSessionExpired(); return; }
      if (status === 200 && data) {
        this.cart = data.cart;
        this.showToast(data.item.name + " added to your purchase list");
        this._stockWarn(this.cart.lines.find(l => l.item.id === data.item.id));
        return;
      }
      const detail = data && data.detail;
      if (status === 422 && detail && detail.status === "misread") {
        this.showToast(detail.message || "Misread — re-scan");
        return;
      }
      if (status === 400 && detail && detail.status === "badge") {
        this.showToast("Badges log in, they are not catalogue items");
        return;
      }
      if (status === 404 && detail && detail.status === "unknown") {
        this.intakeQueue.push(detail);
        this.openNextIntake();
        return;
      }
      this.showToast((detail && detail.message) || `Scan failed (${status})`);
    },

    handleSessionExpired() {
      // Raw-mode 401s land here: the session is dead — mirror the api()
      // wrapper's throwing-path cleanup (raw callers own their errors, so
      // the wrapper skipped it). Any open overlay belonged to the dead
      // session: close it so it cannot strand over the login view.
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      this.user = null;
      this.clearLogoff();
      this.dismissGreeting();   // the banner dies with its session
      this._closeOverlays();
      location.hash = "#/login";
      this.route = "#/login";
      this.showToast("Session expired — please log in again.");
    },

    /* ---------- kiosk logoff countdown (Task 4's server deadline) ---------- */

    async armLogoff() {
      const { status, data } = await api("/auth/logoff-schedule", {
        method: "POST", raw: true,
      });
      if (status === 401) { this.handleSessionExpired(); return; }
      if (status !== 200 || !data) {
        this.showToast("Could not arm the logoff countdown");
        return;
      }
      // The server deadline is the truth (Task 4 enforces it lazily); the
      // overlay is presentation. Naive-UTC ISO from the server — append "Z"
      // or Date.parse reads LOCAL time and skews the countdown.
      const iso = data.logoff_deadline;
      const deadline = new Date(/Z$|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
      this.clearLogoff();
      this.logoff = { name: this.user.name, deadline, remaining: data.grace_seconds };
      this._logoffTimer = setInterval(() => this.tickLogoff(), 250);
      this.tickLogoff();
    },

    tickLogoff() {
      if (!this.logoff) return;
      this.logoff.remaining = Math.max(0, Math.ceil((this.logoff.deadline - Date.now()) / 1000));
      if (this.logoff.remaining <= 0) this.finishLogoff();
    },

    async finishLogoff() {
      this.clearLogoff();
      await this.logout();   // best-effort: the server deadline already killed
      this.showToast("Logged off"); // the token if the browser was throttled
    },

    async cancelLogoff() {
      this.clearLogoff();
      const { status } = await api("/auth/logoff-cancel", { method: "POST", raw: true });
      if (status === 401) { this.handleSessionExpired(); return; }
      this.showToast("Logoff cancelled");
    },

    clearLogoff() {
      if (this._logoffTimer) { clearInterval(this._logoffTimer); this._logoffTimer = null; }
      this.logoff = null;
    },

    /* ---------- save-then-switch (coordinator decision 3: the new session
     * is secured BEFORE the old kiosk token is dropped) ---------- */

    async switchAccount(code) {
      const oldToken = localStorage.getItem(TOKEN_KEY);
      const oldName = this.user ? this.user.name : "";
      await this.flushCart();                      // 1. outgoing work is durable
      let auth;                                     // 2. NEW session first —
      try {                                         //    on failure we stay
        auth = await api("/auth/qr-login", {       //    exactly as we are
          method: "POST", body: { token: code },
        });
      } catch (e) {
        this.showToast("Switch failed: " + e.message);
        return;
      }
      if (this.logoff) {                            // 3. a pending countdown
        this.clearLogoff();                         //    dies with the switch
        await api("/auth/logoff-cancel", { method: "POST", raw: true });
      }
      this.saveSession(auth);                       // 4. new session secured
      this._closeOverlays();                        //    (modals belonged to
                                                    //    the outgoing user)
      await api("/auth/logout", {                   // 5. revoke the OLD kiosk
        method: "POST", raw: true, token: oldToken, //    token (best-effort; a
      });                                           //    foreign token never
                                                    //    trips the 401 redirect)
      if (auth.user.must_change_password) {
        location.hash = "#/account";
        this.route = "#/account";
        this.showToast(`Switched to ${auth.user.name} — change the password to continue`);
        return;
      }
      await this.loadCart();                        // 6. the incoming user's list
      this.reloadItems();
      this.loadCategories();
      location.hash = "#/catalogue";
      this.route = "#/catalogue";
      this.showToast(`Switched to ${auth.user.name} — ${oldName}'s list was saved`);
      await this.replayStagedScans();
    },

    /* ---------- anonymous scan staging + replay (attribution) ---------- */

    stageScan(code) {
      this.stagedScans.push({ code, symbology: "" });
      if (this.stagedScans.length > STAGING_CAP) {
        this.stagedScans = this.stagedScans.slice(-STAGING_CAP);
      }
      this.persistStagedScans();
      this.showToast("Scan saved — log in or scan your badge to assign it");
    },

    persistStagedScans() {
      try {
        localStorage.setItem(STAGING_KEY, JSON.stringify(this.stagedScans));
      } catch (_) { /* storage full: staging is best-effort */ }
    },

    discardStagedScans() {
      this.stagedScans = [];
      this.persistStagedScans();
    },

    async replayStagedScans() {
      if (!this.stagedScans.length || !this.user || this.user.must_change_password) return;
      for (const staged of this.stagedScans) {
        const { status, data } = await api("/cart/scan", {
          method: "POST", body: { code: staged.code, symbology: staged.symbology, qty: 1 },
          raw: true,
        });
        if (status === 401) { this.handleSessionExpired(); return; }
        if (status === 200 && data) {
          this.cart = data.cart;
          this.showToast(data.item.name + " added (assigned scan)");
        } else if (status === 422) {
          this.showToast("A staged scan was a misread — dropped");
        } else if (status === 404 && data && data.detail && data.detail.status === "unknown") {
          this.intakeQueue.push(data.detail);   // unknown: take it through intake
          continue;
        } else {
          this.showToast("A staged scan could not be added");
        }
      }
      this.stagedScans = [];
      this.persistStagedScans();
      this.openNextIntake();
    },

    /* ---------- unknown-item intake modal ---------- */

    openNextIntake() {
      if (this.scanIntake || !this.intakeQueue.length) return;  // one at a time
      const detail = this.intakeQueue.shift();                  // skip = drop
      this.scanIntake = {
        code: detail.code,
        message: detail.message,
        analysis: detail.analysis || {},
        catalogable: !!detail.catalogable,
        suggestions: detail.suggestions || { by_prefix: [], by_keyword: [], by_semantic: [] },
        name: "",
        price: "",
        sku: (detail.analysis && detail.analysis.gtin14) || detail.code || "",
        busy: false,
        // When opened from the staff Requests tab, resolving closes the loop:
        resolveRequestId: null,
        addToCart: true,
        requesterName: null,
      };
    },

    closeIntake() {
      this.scanIntake = null;
      this.openNextIntake();   // the queue keeps draining
    },

    intakeAnalysisLine() {
      const a = (this.scanIntake && this.scanIntake.analysis) || {};
      const bits = [];
      if (a.format) bits.push(a.format);
      if (a.region) bits.push(a.region);
      if (a.usage) bits.push(a.usage);
      if (a.globally_unique === false) bits.push("restricted circulation");
      return bits.join(" · ");
    },

    intakeSuggest() {
      // Live suggestions as the operator types a name (debounced).
      clearTimeout(this._suggestTimer);
      this._suggestTimer = setTimeout(async () => {
        if (!this.scanIntake) return;
        const name = (this.scanIntake.name || "").trim();
        if (name.length < 2) return;
        try {
          const params = new URLSearchParams({ q: name, limit: 5 });
          if (this.scanIntake.catalogable && this.scanIntake.analysis.gtin14) {
            params.set("barcode", this.scanIntake.analysis.gtin14);
          }
          const s = await api("/items/suggest?" + params.toString());
          if (this.scanIntake) this.scanIntake.suggestions = s;
        } catch (_) { /* suggestions are best-effort */ }
      }, 300);
    },

    async addSuggestedItem(item) {
      // "Add that instead": a suggestion click beats the intake form.
      try {
        this.cart = await api("/cart/items", {
          method: "POST", body: { item_id: item.id, qty: 1 },
        });
        this.showToast(item.name + " added to your purchase list");
        this.closeIntake();
      } catch (e) {
        this.showToast(e.message);
      }
    },

    async submitIntake() {
      const intake = this.scanIntake;
      if (!intake || intake.busy) return;
      if (!intake.name.trim()) { this.showToast("Enter the item name first"); return; }
      intake.busy = true;
      try {
        if (this.isStaff) await this.submitIntakeStaff(intake);
        else await this.submitIntakeMember(intake);
      } catch (e) {
        this.showToast(e.message);
      } finally {
        if (this.scanIntake) this.scanIntake.busy = false;
      }
    },

    async _intakeAddToCart(itemId) {
      this.cart = await api("/cart/items", {
        method: "POST", body: { item_id: itemId, qty: 1 },
      });
    },

    async submitIntakeStaff(intake) {
      const price = Math.round(parseFloat(intake.price || "0") * 100) || 0;
      const gtin14 = intake.catalogable ? (intake.analysis.gtin14 || null) : null;

      // Pre-check: the code may have been catalogued since the scan 404'd
      // (or the intake may have been opened from the staff queue).
      if (gtin14) {
        const existing = await api("/items?barcode=" + encodeURIComponent(gtin14));
        if (existing.length) {
          await this._intakeAddToCart(existing[0].id);
          this.showToast(`Already filed as ${existing[0].sku} — added instead`);
          await this._resolveIfRequested(intake, existing[0].id);
          this.closeIntake();
          return;
        }
      }

      let item;
      try {
        item = await api("/items", {
          method: "POST",
          body: {
            sku: intake.sku.trim(),
            name: intake.name.trim(),
            description: "",
            category: "general",
            price_cents: price,
            stock: 0,
            barcode: gtin14,
          },
        });
      } catch (e) {
        if (e.status === 409 && gtin14) {
          // Created concurrently: fetch the conflicting item and add that.
          const existing = await api("/items?barcode=" + encodeURIComponent(gtin14));
          if (existing.length) {
            await this._intakeAddToCart(existing[0].id);
            this.showToast(`Already filed as ${existing[0].sku} — added instead`);
            await this._resolveIfRequested(intake, existing[0].id);
            this.closeIntake();
            return;
          }
        }
        throw e;
      }
      await this._intakeAddToCart(item.id);
      this.showToast(item.name + " added to the catalogue and your purchase list");
      await this._resolveIfRequested(intake, item.id);
      this.closeIntake();
      if (this.currentView === "admin" || this.currentView === "table") {
        this.loadAdminData();
      }
    },

    async _resolveIfRequested(intake, itemId) {
      // Best-effort queue closure (staff-only endpoints; silent no-op
      // otherwise), in two shapes: the modal was opened FROM the staff
      // queue (resolveRequestId set — the checkbox decides add_to_cart),
      // or a member's open request for this same code exists — filing the
      // item resolves it too (add_to_cart: they scanned it wanting it).
      try {
        if (intake.resolveRequestId) {
          await api(`/scan-requests/${intake.resolveRequestId}/resolve`, {
            method: "POST",
            body: { item_id: itemId, add_to_cart: intake.addToCart },
          });
          this.showToast("Scan request resolved");
        } else {
          const open = await api("/scan-requests?status=open");
          const match = open.find(r => r.code === intake.code);
          if (match) {
            await api(`/scan-requests/${match.id}/resolve`, {
              method: "POST",
              body: { item_id: itemId, add_to_cart: true },
            });
            this.showToast(`Also resolved ${match.requester.name}'s request for this code`);
          }
        }
        this.loadScanRequests();
      } catch (e) {
        this.showToast("Created, but resolving the request failed: " + e.message);
      }
    },

    async submitIntakeMember(intake) {
      const price = Math.round(parseFloat(intake.price || "0") * 100) || 0;
      try {
        await api("/scan-requests", {
          method: "POST",
          body: {
            code: intake.code,
            symbology: "",
            name: intake.name.trim(),
            price_cents: price,
          },
        });
        this.showToast("Reported to staff — scan it again once it's in the catalogue");
        this.closeIntake();
      } catch (e) {
        if (e.status === 409 && e.detail && e.detail.status === "already" && e.detail.item) {
          await this._intakeAddToCart(e.detail.item.id);
          this.showToast(`Already filed as ${e.detail.item.sku} — added instead`);
          this.closeIntake();
          return;
        }
        if (e.status === 409 && e.detail && e.detail.status === "duplicate") {
          this.showToast("You already reported this code — staff know");
          this.closeIntake();
          return;
        }
        throw e;
      }
    },

    /* ---------- data explorer ---------- */
    async loadDataPage() {
      this.dataLoading = true;
      try {
        const params = new URLSearchParams({
          limit: this.dataLimit, offset: this.dataPage * this.dataLimit,
        });
        this.dataRows = await api("/items?" + params.toString());
      } catch (e) {
        this.showToast(e.message);
      } finally {
        this.dataLoading = false;
      }
    },

    selectRow(id) {
      this.rowAnchor = id;
      this.selectedRowIds = [id];
    },
    toggleRow(id) {
      if (this.selectedRowIds.includes(id)) {
        this.selectedRowIds = this.selectedRowIds.filter((r) => r !== id);
      } else {
        this.rowAnchor = id;
        this.selectedRowIds.push(id);
      }
    },
    selectRowRange(id) {
      const ids = this.dataRows.map((r) => r.id);
      const from = this.rowAnchor === null ? id : this.rowAnchor;
      let a = ids.indexOf(from), b = ids.indexOf(id);
      if (a === -1 || b === -1) return;
      [a, b] = a <= b ? [a, b] : [b, a];
      this.selectedRowIds = ids.slice(a, b + 1);
    },

    selectColumn(key) {
      this.colAnchor = key;
      this.selectedCols = [key];
    },
    toggleColumn(key) {
      if (this.selectedCols.includes(key)) {
        this.selectedCols = this.selectedCols.filter((c) => c !== key);
      } else {
        this.colAnchor = key;
        this.selectedCols.push(key);
      }
    },
    selectColumnRange(key) {
      const keys = this.allColumns.map((c) => c.key);
      const from = this.colAnchor === null ? key : this.colAnchor;
      let a = keys.indexOf(from), b = keys.indexOf(key);
      if (a === -1 || b === -1) return;
      [a, b] = a <= b ? [a, b] : [b, a];
      this.selectedCols = keys.slice(a, b + 1);
    },

    clearDataSelection() {
      this.selectedRowIds = [];
      this.selectedCols = [];
      this.rowAnchor = this.colAnchor = null;
    },

    cellValue(row, key) {
      switch (key) {
        case "price_cents": return this.formatPrice(row.price_cents);
        case "description": {
          const d = row.description || "";
          return d.length > 60 ? d.slice(0, 57) + "…" : d;
        }
        case "has_embedding": return row.has_embedding ? "✓" : "—";
        case "created_at": return this.formatDate(row.created_at);
        default: return row[key];
      }
    },

    /** Raw value used for CSV/PDF exports. */
    exportValue(row, key) {
      switch (key) {
        case "price_cents": return (row.price_cents / 100).toFixed(2);
        case "has_embedding": return row.has_embedding ? "yes" : "no";
        case "created_at": return new Date(row.created_at).toISOString().slice(0, 10);
        default: return row[key] ?? "";
      }
    },

    csvEscape(value) {
      let s = String(value);
      // Guard against spreadsheet formula injection (=, +, -, @ prefixes are
      // evaluated as formulas when the CSV is opened in Excel/LibreOffice).
      if (/^[=+\-@]/.test(s)) s = "'" + s;
      return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    },

    downloadBlob(content, filename, type) {
      const blob = new Blob([content], { type });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    },

    exportCSV() {
      if (!this.exportRows.length) return this.showToast("Nothing to export");
      const cols = this.exportCols;
      const header = cols.map((c) => this.csvEscape(c.label)).join(",");
      const lines = this.exportRows.map(
        (r) => cols.map((c) => this.csvEscape(this.exportValue(r, c.key))).join(",")
      );
      // BOM prefix so Excel detects the file as UTF-8.
      this.downloadBlob(
        "\ufeff" + [header, ...lines].join("\n"), "items-export.csv", "text/csv;charset=utf-8"
      );
      this.showToast(
        `Exported ${this.exportRows.length} row(s) × ${cols.length} column(s) as CSV`
      );
    },

    exportPDF() {
      if (!this.exportRows.length) return this.showToast("Nothing to export");
      if (!window.jspdf) return this.showToast("PDF library not loaded");
      const cols = this.exportCols;
      const doc = new window.jspdf.jsPDF({ orientation: "landscape" });
      doc.setFontSize(14);
      doc.text("HCRM — Items export", 14, 15);
      doc.setFontSize(9);
      doc.text(
        `${this.exportRows.length} row(s) × ${cols.length} column(s) — ` +
        new Date().toLocaleString(), 14, 22
      );
      doc.autoTable({
        startY: 27,
        head: [cols.map((c) => c.label)],
        body: this.exportRows.map((r) => cols.map((c) => String(this.exportValue(r, c.key)))),
        styles: { fontSize: 8, cellPadding: 2 },
        headStyles: { fillColor: [37, 99, 235] },
      });
      doc.save("items-export.pdf");
      this.showToast(
        `Exported ${this.exportRows.length} row(s) × ${cols.length} column(s) as PDF`
      );
    },

    /* ---------- admin: items ---------- */
    /** Fetch every catalogue item, following the pagination. */
    async fetchAllItems() {
      const all = [];
      const limit = 100;
      for (let offset = 0; ; offset += limit) {
        const page = await api(`/items?limit=${limit}&offset=${offset}`);
        all.push(...page);
        if (page.length < limit) break;
      }
      return all;
    },

    async loadAdminData() {
      if (!this.isStaff) return;
      try {
        this.adminItems = await this.fetchAllItems();
        this.members = await api("/members");
        this.loadCategories();
        this.loadScanRequests();
      } catch (e) {
        this.showToast(e.message);
      }
    },

    /* ---------- staff: the scan-request queue (unknown items) ---------- */

    async loadScanRequests() {
      if (!this.isStaff) return;
      try {
        const params = new URLSearchParams({ status: this.scanReqFilter });
        this.scanRequests = await api("/scan-requests?" + params.toString());
      } catch (e) {
        this.showToast(e.message);
      }
    },

    openRequestIntake(req) {
      // Staff resolution: the intake modal, prefilled from the member's
      // proposal; on submit the item is created, added, and the request
      // resolved (add_to_cart checkbox — default on: the member scanned it
      // wanting it).
      this.intakeQueue = [];
      this.scanIntake = {
        code: req.code,
        message: `Scan request from ${req.requester.name}`,
        analysis: { kind: req.kind, gtin14: req.gtin14, format: req.kind === "gtin" ? "GTIN" : "" },
        catalogable: !!req.gtin14,
        suggestions: { by_prefix: [], by_keyword: [], by_semantic: [] },
        name: req.name || "",
        price: req.price_cents ? String(req.price_cents / 100) : "",
        sku: req.gtin14 || req.code,
        busy: false,
        resolveRequestId: req.id,
        addToCart: true,
        requesterName: req.requester.name,
      };
    },

    async dismissRequest(req) {
      if (!confirm(`Dismiss the scan request from ${req.requester.name} for "${req.name}"?`)) return;
      try {
        await api(`/scan-requests/${req.id}/dismiss`, { method: "POST" });
        this.showToast("Scan request dismissed");
        this.loadScanRequests();
      } catch (e) {
        this.showToast(e.message);
      }
    },

    /* ---------- account: "My scan requests" ---------- */

    async loadMyRequests() {
      if (!this.user) return;
      try {
        this.myRequests = await api("/scan-requests/mine");
      } catch (_) { /* non-critical (403 while flagged) */ }
    },

    async submitItem() {
      this.itemError = "";
      this.busy = true;
      // Explicit payload: PATCH must not carry fields outside ItemPatch
      // (e.g. sku, which is immutable) — the API rejects unknown fields.
      const payload = {
        name: this.itemForm.name,
        description: this.itemForm.description,
        category: this.itemForm.category,
        price_cents: Math.round(this.itemForm.price * 100),
        stock: this.itemForm.stock,
        image_url: this.itemForm.image_url,
      };
      if (!this.itemEditingId) payload.sku = this.itemForm.sku;
      try {
        if (this.itemEditingId) {
          await api("/items/" + this.itemEditingId, { method: "PATCH", body: payload });
          this.showToast("Item updated");
        } else {
          await api("/items", { method: "POST", body: payload });
          this.showToast("Item added to the catalogue");
        }
        this.resetItemForm();
        this.loadAdminData();
      } catch (e) {
        this.itemError = e.message;
      } finally {
        this.busy = false;
      }
    },

    editItem(item) {
      this.itemEditingId = item.id;
      this.itemForm = {
        sku: item.sku,
        name: item.name,
        description: item.description,
        category: item.category,
        price: item.price_cents / 100,
        stock: item.stock,
        image_url: item.image_url,
      };
      window.scrollTo({ top: 0, behavior: "smooth" });
    },

    resetItemForm() {
      this.itemEditingId = null;
      this.itemForm = emptyItemForm();
    },

    async deleteItem(item) {
      if (!confirm(`Delete "${item.name}" from the catalogue?`)) return;
      try {
        await api("/items/" + item.id, { method: "DELETE" });
        this.showToast("Item deleted");
        this.loadAdminData();
        if (this.currentView === "table") this.loadDataPage();
      } catch (e) {
        this.showToast(e.message);
      }
    },

    /* ---------- admin: members ---------- */
    async createMember() {
      this.memberError = "";
      this.busy = true;
      try {
        const data = await api("/members", { method: "POST", body: this.memberForm });
        this.showToast("Account created for " + this.memberForm.name);
        // Task 2: the 201 carries the member's first badge — show it so the
        // counter can print the card immediately (re-viewable after Task A;
        // keep the urgency, drop the old unrecoverability claim).
        if (data.qr_badge) {
          this.badgePrint = {
            title: "QR badge — " + this.memberForm.name,
            subtitle: "Print the membership card now",
            svg: data.qr_badge.svg,
            payload: data.qr_badge.payload,
            warning: "Anyone holding this code can log in as the member — " +
                     "hand it over privately. Print it now while you have it.",
          };
        }
        this.memberForm = { name: "", email: "", password: "", role: "member" };
        this.loadAdminData();
      } catch (e) {
        this.memberError = e.message;
      } finally {
        this.busy = false;
      }
    },

    async deleteMember(member) {
      if (!confirm(`Delete the account of ${member.name}?`)) return;
      try {
        await api("/members/" + member.id, { method: "DELETE" });
        this.showToast("Account deleted");
        this.loadAdminData();
      } catch (e) {
        this.showToast(e.message);
      }
    },

    async showMemberBadge(member) {
      // Generate/replace the member's badge and show it for printing.
      try {
        const data = await api("/members/" + member.id + "/qr-badge", { method: "POST" });
        this.memberBadge = {
          id: member.id, name: member.name, email: member.email, svg: data.svg,
        };
        this.loadAdminData();
      } catch (e) {
        this.showToast(e.message);
      }
    },

    async revokeMemberBadge(member) {
      if (!confirm(`Revoke the QR badge of ${member.name}?`)) return;
      try {
        await api("/members/" + member.id + "/qr-badge", { method: "DELETE" });
        this.showToast("QR badge revoked for " + member.name);
        if (this.memberBadge && this.memberBadge.id === member.id) this.memberBadge = null;
        this.loadAdminData();
      } catch (e) {
        this.showToast(e.message);
      }
    },

    openPwReset(member) {
      this.pwResetMember = member;
      this.pwResetValue = "";
      this.pwResetError = "";
    },

    closePwReset() {
      this.pwResetMember = null;
      this.pwResetValue = "";
      this.pwResetError = "";
    },

    async submitPwReset() {
      const member = this.pwResetMember;
      if (!member) return;
      if (this.pwResetValue.length < 8) {
        this.pwResetError = "Password must be at least 8 characters";
        return;
      }
      this.pwResetError = "";
      try {
        await api("/members/" + member.id, {
          method: "PATCH", body: { password: this.pwResetValue },
        });
        this.showToast(
          "Password reset for " + member.name + " (their sessions were signed out)"
        );
        this.closePwReset();
      } catch (e) {
        this.pwResetError = e.message;
      }
    },
  },

  mounted() {
    window.addEventListener("hashchange", () => {
      const next = location.hash || "#/catalogue";
      // Close overlays only on USER-initiated navigation (links, back/
      // forward, the api() 401 redirect) — those never pre-set this.route,
      // so `next` differs. Programmatic routes (afterLogin, switchAccount,
      // logout) assign this.route BEFORE the queued hashchange task runs,
      // and their follow-up modals are set in microtasks that land before
      // it — an unconditional close here would null the register one-shot
      // badge modal (and a post-switch intake) before it ever rendered
      // (measured: microtask-before-task is guaranteed by the event loop).
      const navigated = next !== this.route;
      this.route = next;
      if (navigated) this._closeOverlays();
    });

    // The api() wrapper broadcasts this when a request comes back 401:
    // drop the in-memory session and show the login screen.
    window.addEventListener("hcrm-session-expired", () => {
      this.user = null;
      this.clearLogoff();   // a pending countdown's timer must die here
      this.dismissGreeting();  // the banner names a user whose session is dead
      this._closeOverlays();  // ...and the dead session's modals with it
      this.route = "#/login";
    });

    // The global HID scan capture: one document-level listener, always on
    // (works on every view, behind modals, logged in or out).
    this.installScanCapture();

    // Anonymous scans staged on a previous page load (codes only, no secrets).
    try {
      this.stagedScans = JSON.parse(localStorage.getItem(STAGING_KEY) || "[]");
    } catch (_) { this.stagedScans = []; }

    if (this.user) {
      this.loadCategories();
      this.reloadItems();
      this.loadCart();
      if (this.currentView === "admin") this.loadAdminData();
      if (this.currentView === "table") this.loadDataPage();
      if (this.currentView === "account") this.loadMyRequests();
    }

    // Infinite scroll: load more items when the sentinel becomes visible.
    this._observer = new IntersectionObserver((entries) => {
      // The checkout review screen renders the same grid — "add" there
      // needs the same infinite scroll (round 3, Task C).
      const gridView = this.currentView === "catalogue" || this.currentView === "checkout";
      if (entries.some((e) => e.isIntersecting) && gridView) {
        this.loadMore();
      }
    });
    this.$nextTick(() => this.observeSentinel());
  },
}).mount("#app");
