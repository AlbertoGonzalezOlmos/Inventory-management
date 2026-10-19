const brief = `Objective: analyze and recommend the correct behavior for HCRM's shopping list (cart) when a user logs off or another user logs in. Read-only analysis — do NOT modify files, do NOT run the full test suite.

Repo: /home/pentacosiarca/Documents/code/Inventory-management (FastAPI + Vue 3 SPA).

Background: the cart is SERVER-SIDE and PER-USER (app/models.py CartItem.user_id; app/routers/cart.py; every mutation committed server-side). The owner reported during a hardware test: "when the item was in the purchase list and I changed users, the item disappeared. We need to save the state of the shopping list before logging off or changing the user."

Current code (static/app.js):
- switchAccount(code) ~line 1704: on a different member's badge scan, does flushCart() (drain mutation chain), qr-login as new user, saveSession, close overlays, revoke old token, then loadCart() (loads the INCOMING user's cart), toast "Switched to X — Y's list was saved".
- logout() ~line 603: clears in-memory cart + session, calls /auth/logout; server cart untouched.
- loadCart() ~line 766: GET /api/cart for current user.
- handleBarcodeScan ~line 1470: POST /api/cart/scan (committed write).

Verified facts (against a live server): the cart IS persisted per-user; Alice's cart survives Bob logging in AND survives Alice logout + re-login. So the item is NOT lost — it stays in the previous user's cart and the new user sees their own (empty) cart.

Decision needed: is the owner's report (1) a bug, (2) expected per-user behavior needing only a UX improvement, or (3) a request to change the model (e.g. a shared counter-level list that persists across user changes)? Recommend the concrete behavior and a MINIMAL implementation plan (specific file + method).

Read: static/app.js (switchAccount, logout, loadCart, handleBadgeScan, handleBarcodeScan, afterLogin), app/routers/cart.py, app/models.py (CartItem), docs/plans/COORDINATOR.md (round-1 "save then switch" intent).

Output (<= 450 words): (a) verdict (bug vs UX vs model-change), (b) concrete behavior to implement, (c) minimal code changes (file + method + what changes), (d) risks/edge cases.`;

const results = await runs.all([
  {
    key: "oracle-shoppinglist",
    agent: "oracle",
    context: "fork",
    phase: "Council pass 1",
    label: "Decide shopping-list persistence behavior",
    task: brief + "\n\nAs the decision-consistency oracle, weight the PRODUCT decision and consistency with the documented round-1 intent (docs/plans/COORDINATOR.md) above all.",
  },
  {
    key: "reviewer-shoppinglist",
    agent: "reviewer",
    phase: "Council pass 1",
    label: "Review shopping-list persistence behavior",
    task: brief + "\n\nAs the reviewer, weight code correctness, edge cases, and the minimal-change risk. State explicitly whether the current code already satisfies the owner's request.",
  },
]);

return results.map((r) => ({ key: r.key, ok: r.ok, output: r.output }));
