// Shared across every page of the HomeShop storefront.

const API = ""; // same origin

function getUserId() {
  return localStorage.getItem("homeshop_user") || "alice";
}
function setUserId(id) {
  const normalized = (id || "alice").trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(normalized)) {
    throw new Error("Use 1-32 lowercase letters, numbers, _ or -");
  }
  localStorage.setItem("homeshop_user", normalized);
}

function money(cents) {
  return "$" + (cents / 100).toFixed(2);
}

function showToast(msg) {
  let t = document.getElementById("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    t.className = "toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._hideTimer);
  t._hideTimer = setTimeout(() => t.classList.remove("show"), 2200);
}

async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-HomeShop-User": getUserId(),
      ...(options.headers || {}),
    },
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    const msg = (data && data.error && data.error.message) || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

async function cartCount() {
  try {
    const items = await api(`/cart/${getUserId()}`);
    return items.reduce((sum, i) => sum + i.quantity, 0);
  } catch (e) {
    return 0;
  }
}

// Renders the shared nav bar into <header id="site-nav"></header>,
// present on every page. `active` is the current page's nav key.
async function renderNav(active) {
  const el = document.getElementById("site-nav");
  if (!el) return;

  const count = await cartCount();
  const link = (key, href, label) =>
    `<a href="${href}" class="${active === key ? "active" : ""}">${label}</a>`;

  el.outerHTML = `
    <nav class="site-nav">
      <a href="/index.html" class="brand">HomeShop</a>
      <div class="links">
        ${link("shop", "/shop.html", "Shop")}
        ${link("cart", "/cart.html", `Cart${count ? `<span class="cart-badge">${count}</span>` : ""}`)}
        ${link("orders", "/orders.html", "My Orders")}
        ${link("admin", "/admin.html", "My seller dashboard")}
      </div>
      <div class="user-box">
        Shopping as
        <input id="navUserId" value="${getUserId()}" spellcheck="false">
      </div>
    </nav>
  `;

  document.getElementById("navUserId").addEventListener("change", (e) => {
    try {
      setUserId(e.target.value);
      location.reload();
    } catch (error) {
      showToast(error.message);
      e.target.value = getUserId();
    }
  });
}
