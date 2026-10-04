/* HCRM frontend — Vue 3 SPA (no build step, served by FastAPI). */

const API_BASE = "/api";
const TOKEN_KEY = "hcrm_token";
const USER_KEY = "hcrm_user";
const PURCHASE_KEY = "hcrm_purchase";
const PAGE_SIZE = 24;

/** Minimal fetch wrapper with token + error handling. */
async function api(path, { method = "GET", body } = {}) {
  const token = localStorage.getItem(TOKEN_KEY);
  const res = await fetch(API_BASE + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (res.status === 401 && !path.startsWith("/auth/login") && !path.startsWith("/auth/qr-login")) {
    // Token expired or revoked: clear the session and force re-login.
    // (Login failures also return 401 but must surface their own message.)
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    window.dispatchEvent(new Event("hcrm-session-expired"));
    if (location.hash !== "#/login") location.hash = "#/login";
    throw new Error("Session expired — please log in again.");
  }
  if (res.status === 401) {
    let detail = "Invalid email or password";
    try {
      const data = await res.json();
      if (typeof data.detail === "string") detail = data.detail;
    } catch (_) { /* keep default */ }
    throw new Error(detail);
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const data = await res.json();
      if (typeof data.detail === "string") {
        detail = data.detail;
      } else if (Array.isArray(data.detail) && data.detail[0] && data.detail[0].msg) {
        // FastAPI validation errors: detail is a list of {loc, msg, ...}.
        const d = data.detail[0];
        const field = Array.isArray(d.loc) && d.loc.length > 1 ? d.loc[d.loc.length - 1] : null;
        detail = field ? `${field}: ${d.msg}` : d.msg;
      }
    } catch (_) { /* keep default */ }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
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
      badgeCode: "",  // filled by the Opticon M-10 (USB-HID types it + Enter)
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
      purchase: JSON.parse(localStorage.getItem(PURCHASE_KEY) || "[]"),
      detailItem: null,
      similar: [],

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

      // account
      pwForm: { current_password: "", new_password: "" },
      pwError: "",
      pwOk: false,
      badgeSvg: "",      // freshly generated own badge (inline SVG markup)

      // members: badge being displayed for printing
      memberBadge: null,  // { id, name, email, svg }

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
      return "catalogue";
    },
    purchaseTotalCents() {
      return this.purchase.reduce((sum, e) => sum + e.item.price_cents * e.qty, 0);
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
    purchase: {
      deep: true,
      handler(v) {
        localStorage.setItem(PURCHASE_KEY, JSON.stringify(v));
      },
    },
    search() {
      this.debouncedReload();
    },
    category() {
      this.reloadItems();
    },
    currentView(view) {
      if (view === "catalogue") {
        if (this.items.length === 0) this.reloadItems();
        // The sentinel is recreated when the catalogue section re-mounts,
        // so the observer must be (re)attached every time we come back.
        this.$nextTick(() => this.observeSentinel());
      }
      if (view === "admin") this.loadAdminData();
      if (view === "table") this.loadDataPage();
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

    /* ---------- auth ---------- */
    async doLogin() {
      this.loginError = "";
      this.busy = true;
      try {
        const data = await api("/auth/login", { method: "POST", body: this.loginForm });
        this.saveSession(data);
        if (data.user.must_change_password) {
          // Server-side enforced: every other endpoint returns 403 until the
          // password is changed.
          location.hash = "#/account";
          this.route = "#/account";
          this.showToast("Please change your password before continuing");
        } else {
          location.hash = "#/catalogue";
          this.route = "#/catalogue";
          this.reloadItems();
          this.loadCategories();
        }
      } catch (e) {
        this.loginError = e.message;
      } finally {
        this.busy = false;
      }
    },

    async doBadgeLogin() {
      // QR badge login: the scanner (USB-HID mode) types the badge payload
      // into the field and its CR suffix submits the form.
      this.loginError = "";
      this.busy = true;
      try {
        const data = await api("/auth/qr-login", {
          method: "POST", body: { token: this.badgeCode.trim() },
        });
        this.badgeCode = "";
        this.saveSession(data);
        if (data.user.must_change_password) {
          location.hash = "#/account";
          this.route = "#/account";
          this.showToast("Please change your password before continuing");
        } else {
          location.hash = "#/catalogue";
          this.route = "#/catalogue";
          this.reloadItems();
          this.loadCategories();
        }
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
        this.saveSession(data);
        location.hash = "#/catalogue";
        this.route = "#/catalogue";
        this.reloadItems();
        this.loadCategories();
        this.showToast("Welcome, " + data.user.name + "!");
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
    },

    async logout() {
      try { await api("/auth/logout", { method: "POST" }); } catch (_) { /* ignore */ }
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      this.user = null;
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

    async revokeMyBadge() {
      if (!confirm("Revoke your QR badge? It will stop working immediately.")) return;
      try {
        await api("/auth/qr-badge", { method: "DELETE" });
        this.badgeSvg = "";
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

    /* ---------- purchase list ---------- */
    addToPurchase(item) {
      if (item.stock <= 0) {
        this.showToast(item.name + " is out of stock");
        return;
      }
      const entry = this.purchase.find((e) => e.item.id === item.id);
      if (entry) {
        if (entry.qty >= item.stock) {
          this.showToast("Only " + item.stock + " in stock");
          return;
        }
        entry.qty += 1;
      } else {
        this.purchase.push({ item, qty: 1 });
      }
      this.showToast(item.name + " added to your purchase list");
      this.closeDetail();
    },
    incPurchase(id) {
      const e = this.purchase.find((e) => e.item.id === id);
      if (e && e.qty < e.item.stock) e.qty += 1;
    },
    decPurchase(id) {
      const e = this.purchase.find((e) => e.item.id === id);
      if (e && e.qty > 1) e.qty -= 1;
    },
    removeFromPurchase(id) {
      this.purchase = this.purchase.filter((e) => e.item.id !== id);
    },
    clearPurchase() { this.purchase = []; },
    requestPurchase() {
      // Barebones placeholder: the purchase list is kept client-side.
      this.showToast(
        `Request noted: ${this.purchase.length} item type(s), total ` +
        this.formatPrice(this.purchaseTotalCents) + ". (Checkout to be implemented)"
      );
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
      } catch (e) {
        this.showToast(e.message);
      }
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
        await api("/members", { method: "POST", body: this.memberForm });
        this.showToast("Account created for " + this.memberForm.name);
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
      this.route = location.hash || "#/catalogue";
    });

    // The api() wrapper broadcasts this when a request comes back 401:
    // drop the in-memory session and show the login screen.
    window.addEventListener("hcrm-session-expired", () => {
      this.user = null;
      this.route = "#/login";
    });

    if (this.user) {
      this.loadCategories();
      this.reloadItems();
      if (this.currentView === "admin") this.loadAdminData();
      if (this.currentView === "table") this.loadDataPage();
    }

    // Infinite scroll: load more items when the sentinel becomes visible.
    this._observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && this.currentView === "catalogue") {
        this.loadMore();
      }
    });
    this.$nextTick(() => this.observeSentinel());
  },
}).mount("#app");
