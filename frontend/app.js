// Ratio — SPA sin dependencias ni paso de compilación (portabilidad total).
// Estructura: almacenamiento local · api · cola sin conexión · router · vistas (una función por pantalla).

const $app = document.getElementById("app");

// ---------- utilidades ----------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => `${Math.round((x ?? 0) * 100)} %`;
// El servidor guarda las fechas en UTC; SQLite las devuelve sin zona horaria, así que se marcan como UTC.
const asUtc = (iso) => (/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
const fmtDate = (iso) => (iso ? new Date(asUtc(iso)).toLocaleString("es-CO", { dateStyle: "medium", timeStyle: "short" }) : "—");
const STATUS = { fortaleza: "Fortaleza", en_desarrollo: "En desarrollo", vacio: "Vacío conceptual", sin_evidencia: "Sin evidencia" };
const PRIORITY = { alta: "Prioridad alta", media: "Prioridad media", baja: "Prioridad baja" };
const KIND = { norma: "Norma", jurisprudencia: "Jurisprudencia", guia: "Guía oficial" };
const REVIEW = { no_requiere: "Lista", pendiente: "En revisión", en_revision: "En revisión", aprobada: "Aprobada", ajustada: "Aprobada con ajustes" };
const FLAG = {
  subtema_en_transicion: "Subtema en transición normativa", fuente_con_riesgo: "Fuente con riesgo",
  fuente_sin_verificar: "Fuente por verificar", pregunta_con_riesgo: "Pregunta con riesgo",
  pregunta_generada_por_ia: "Pregunta generada por IA", calificacion_automatica: "Calificación automática",
  evidencia_insuficiente: "Evidencia insuficiente", resultado_inconsistente: "Resultado inconsistente",
};
const html = (strings, ...values) => strings.reduce((out, s, i) => out + s + (values[i] ?? ""), "");
const totalQuestions = (n) => (n ? Math.min(45, 15 + 5 * (n - 1)) : 0);

function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast"; el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}
function modal(title, bodyHtml, { actions = "" } = {}) {
  document.querySelector(".modal-backdrop")?.remove();
  const wrap = document.createElement("div");
  wrap.className = "modal-backdrop";
  wrap.innerHTML = html`<div class="modal card" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <h3 id="modal-title">${title}</h3><div class="modal-body">${bodyHtml}</div>
    <div class="row" style="justify-content:flex-end">${actions}<button class="btn" data-close>Entendido</button></div></div>`;
  const close = () => { wrap.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  wrap.addEventListener("click", (e) => { if (e.target === wrap || e.target.closest("[data-close]")) close(); });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(wrap);
  wrap.querySelector("[data-close]").focus();
  return wrap;
}

// Botón del informe: si aún está en verificación muestra el aviso; si ya está verificado se envía al correo.
function reportButton(r, { small = true } = {}) {
  const cls = `btn${small ? " btn-small" : ""}`;
  if (!r.download_allowed) return html`<button class="${cls}" data-report-locked="${esc(r.session_id)}">Descargar PDF</button>`;
  // Verificado: se puede pedir al correo y, como respaldo (si el correo falla), ver e imprimir aquí.
  const print = html`<a class="${cls}${r.email?.enabled ? " btn-ghost" : ""}" href="#/informe/${esc(r.session_id)}">${r.email?.enabled ? "Ver / imprimir" : "Descargar PDF"}</a>`;
  if (r.email?.enabled) return html`<button class="${cls}" data-report-email="${esc(r.session_id)}">Enviar PDF a mi correo</button>${print}`;
  return print;
}
function bindReportButtons(root = $app) {
  root.querySelectorAll("[data-report-locked]").forEach((b) => b.addEventListener("click", () => modal(
    "Tu informe está en verificación",
    html`<p>Tu informe se está verificando. Un administrador académico lo revisa antes de entregarlo.</p>
      <p>Una vez esté verificado, <strong>te lo enviaremos a tu correo electrónico${auth.user() ? html` (${esc(auth.user().email)})` : ""}</strong> en PDF. También verás el cambio de estado en «Mi historial».</p>`)));
  root.querySelectorAll("[data-report-email]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const res = await api(`/sessions/${encodeURIComponent(b.dataset.reportEmail)}/email`, { method: "POST" });
      modal("Informe enviado", html`<p>Estamos enviando tu informe en PDF a <strong>${esc(res.to)}</strong>. Llega en uno o dos minutos; si no lo ves, revisa la carpeta de spam o de promociones.</p>`);
    } catch (e) { modal("No se pudo enviar", html`<p>${esc(e.message)}</p>`); }
    b.disabled = false;
  }));
}

const loading = () => { $app.innerHTML = '<div class="spinner" role="status" aria-label="Cargando"></div>'; };

// ---------- almacenamiento local (con tolerancia a navegadores sin almacenamiento) ----------
const store = {
  get(key, fallback = null) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* sin almacenamiento */ } },
  del(key) { try { localStorage.removeItem(key); } catch { /* sin almacenamiento */ } },
};
const auth = {
  get: () => store.get("ratio_auth"),
  set: (data) => { store.set("ratio_auth", data); renderNav(); },
  clear: () => { store.del("ratio_auth"); store.del("ratio_active"); renderNav(); },
  user: () => auth.get()?.user || null,
  isAdmin: () => auth.user()?.role === "admin",
};

// ---------- api ----------
async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  const token = auth.get()?.token;
  if (token) headers.Authorization = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(`/api${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
  } catch {
    const err = new Error("Sin conexión con el servidor"); err.offline = true; throw err;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(". ") : data.detail;
    const err = new Error(detail || `Error ${res.status}`); err.status = res.status;
    if (res.status === 401 && !path.startsWith("/auth/")) { auth.clear(); goLogin(); }
    throw err;
  }
  return data;
}

let META = null;
async function meta() { if (!META) META = await api("/meta"); return META; }

// ---------- cola de respuestas sin conexión ----------
// Si se cae la red, la respuesta se guarda en este dispositivo y se envía al reconectar.
// El servidor es idempotente: reenviar una respuesta no la duplica.
const queue = {
  all: () => store.get("ratio_queue", []),
  // Cada respuesta pendiente guarda de quién es: en un computador compartido solo se envían las del usuario actual.
  add(item) { store.set("ratio_queue", [...queue.all().filter((q) => !(q.sid === item.sid && q.question_id === item.question_id)), { ...item, uid: auth.user()?.id }]); },
  mine: () => queue.all().filter((q) => !q.uid || q.uid === auth.user()?.id),
  pendingFor: (sid) => queue.mine().filter((q) => q.sid === sid),
  async flush() {
    if (!auth.user()) return true;
    for (const item of queue.mine()) {
      try {
        await api(`/sessions/${item.sid}/answers`, { method: "POST", body: { question_id: item.question_id, answer: item.answer } });
        store.set("ratio_queue", queue.all().filter((q) => !(q.sid === item.sid && q.question_id === item.question_id)));
      } catch (e) {
        if (e.offline) return false;  // sigue sin conexión: se intenta más tarde
        if (e.status === 401 || e.status === 403) continue;  // no es de esta cuenta: no se borra
        store.set("ratio_queue", queue.all().filter((q) => q !== item && !(q.sid === item.sid && q.question_id === item.question_id)));
      }
    }
    return true;
  },
};
window.addEventListener("online", async () => {
  document.body.classList.remove("is-offline");
  if (queue.all().length) { await queue.flush(); toast("Conexión recuperada: tus respuestas se sincronizaron"); render(); }
});
window.addEventListener("offline", () => document.body.classList.add("is-offline"));

// Botones genéricos sin JavaScript en línea (la CSP lo bloquea).
document.addEventListener("click", (e) => {
  // «Saltar al contenido»: mueve el foco sin cambiar el hash (el router usa el hash para las vistas).
  if (e.target.closest(".skip-link")) { e.preventDefault(); $app.focus(); $app.scrollIntoView(); return; }
  if (e.target.closest("[data-retry]")) render();
  if (e.target.closest("[data-print]")) window.print();
});

// ---------- router ----------
const routes = [
  [/^#?\/?$/, viewHome, {}],
  [/^#\/ingresar(\?registro)?$/, viewLogin, {}],
  [/^#\/instrucciones\/(\w+)$/, viewInstructions, { login: true }],
  [/^#\/historial$/, viewHistory, { login: true }],
  [/^#\/privacidad$/, viewPrivacy, {}],
  [/^#\/perfil$/, viewProfile, { login: true }],
  [/^#\/preparando\/(\w+)$/, viewPreparing, { login: true }],
  [/^#\/diagnostico\/(\w+)$/, viewDiagnostic, { login: true }],
  [/^#\/resultado\/(\w+)$/, viewResult, { login: true }],
  [/^#\/informe\/(\w+)$/, viewPrintable, { login: true }],
  [/^#\/admin$/, viewAdmin, { admin: true }],
  [/^#\/admin\/informe\/(\w+)$/, viewAdminReport, { admin: true }],
  [/^#\/docente.*$/, () => { location.hash = "#/admin"; }, {}],
];

function goLogin() {
  if (location.hash !== "#/ingresar") { store.set("ratio_next", location.hash); location.hash = "#/ingresar"; }
}

async function render() {
  const hash = location.hash || "#/";
  document.body.classList.toggle("printing", hash.startsWith("#/informe/"));
  for (const [re, view, opts] of routes) {
    const m = hash.match(re);
    if (!m) continue;
    if ((opts.login || opts.admin) && !auth.user()) { goLogin(); return; }
    if (opts.admin && !auth.isAdmin()) { showError(new Error("Esta sección es solo para administradores.")); return; }
    try { await view(...m.slice(1)); } catch (e) { showError(e); }
    $app.focus({ preventScroll: true });
    window.scrollTo(0, 0);
    return;
  }
  location.hash = "#/";
}
window.addEventListener("hashchange", render);
function showError(e) {
  if (e.status === 403 && auth.user() && /otro usuario/.test(e.message)) {
    $app.innerHTML = html`<div class="card stack"><p class="eyebrow">Acceso restringido</p>
      <h2>Este diagnóstico es de otra cuenta</h2>
      <p class="muted">Entraste como <strong>${esc(auth.user().email)}</strong>, y este enlace corresponde a un diagnóstico de otra persona (por ejemplo, si alguien más usó este computador o te compartió el enlace). Cada quien solo ve sus propios diagnósticos; los administradores pueden ver todos desde «Administración».</p>
      <div class="row"><a class="btn" href="#/historial">Ir a mi historial</a><a class="btn btn-ghost" href="#/perfil">Nuevo diagnóstico</a></div></div>`;
    return;
  }
  $app.innerHTML = html`<div class="card"><h2>${e.offline ? "Sin conexión" : "Algo salió mal"}</h2><p class="error">${esc(e.message)}</p>
    <div class="row"><button class="btn" type="button" data-retry>Reintentar</button><a class="btn btn-ghost" href="#/">Inicio</a></div></div>`;
}

function renderNav() {
  const nav = document.getElementById("topnav");
  if (!nav) return;
  const user = auth.user();
  nav.innerHTML = user
    ? html`<a href="#/perfil">Nuevo diagnóstico</a><a href="#/historial">Mi historial</a>
       ${user.role === "admin" ? '<a href="#/admin">Administración</a>' : ""}
       <span class="nav-user" title="${esc(user.email)}">${esc(user.name.split(" ")[0])}</span>
       <a href="#/" id="logout">Salir</a>`
    : '<a href="#/ingresar">Ingresar</a>';
  const out = document.getElementById("logout");
  if (out) out.onclick = (e) => { e.preventDefault(); auth.clear(); location.hash = "#/"; render(); };
}

// ---------- vistas: cuenta ----------
async function viewHome() {
  const [m, areas] = await Promise.all([meta(), api("/areas").catch(() => [])]);
  const user = auth.user();
  const cta = user
    ? html`<a class="btn btn-lg" href="#/perfil">Comenzar diagnóstico</a><a class="btn btn-ghost btn-lg" href="#/historial">Mi historial</a>`
    : html`<a class="btn btn-lg" href="#/ingresar?registro">Crear mi cuenta</a><a class="btn btn-ghost btn-lg" href="#/ingresar">Ya tengo cuenta</a>`;
  const aiOn = m.ai.live_generation || m.ai.generator;
  $app.innerHTML = html`
  <section class="landing-hero">
    <div class="landing-copy">
      <div class="lockup"><img src="img/ratio-icon.png" width="64" height="64" alt="">
        <div><span class="lockup-name">Ratio</span><span class="lockup-tag">Diagnosticamos tu conocimiento</span></div></div>
      <p class="eyebrow">Educación jurídica · Diagnóstico adaptativo</p>
      <h1>Descubre qué sabes de derecho y <em>por dónde empezar</em> a estudiar.</h1>
      <p class="lead">${esc(m.app_name)} es un diagnóstico, no un curso. Respondes preguntas que se ajustan a ti y, al final, recibes tu nivel, tus fortalezas, tus vacíos y una ruta de estudio con fuentes oficiales.</p>
      <div class="row">${cta}</div>
      <ul class="facts" aria-label="En resumen">
        <li><strong>15–45</strong><span>preguntas, según los temas que elijas</span></li>
        <li><strong>≈ 1 min</strong><span>por pregunta; puedes pausar y seguir</span></li>
        <li><strong>0</strong><span>notas durante el cuestionario</span></li>
      </ul>
    </div>
    <div class="specimen" aria-hidden="true">
      <div class="specimen-card">
        <div class="progress"><span style="width:40%"></span></div>
        <div class="question-meta"><span>Terminación del contrato</span><span>Pregunta 6 de 15</span></div>
        <p class="stem">Una trabajadora es despedida sin justa causa tras dos años con contrato a término indefinido. Según el art. 64 del Código Sustantivo del Trabajo, ¿qué le corresponde?</p>
        <div class="options">
          <div class="option"><span class="key">A</span><span>Nada, porque el empleador puede terminar el contrato libremente.</span></div>
          <div class="option" aria-pressed="true"><span class="key">B</span><span>Una indemnización calculada según su salario y antigüedad.</span></div>
          <div class="option"><span class="key">C</span><span>Solo el pago de vacaciones pendientes.</span></div>
        </div>
      </div>
      <div class="specimen-route">
        <p class="eyebrow">Tu ruta · paso 1</p>
        <strong>Indemnización por despido</strong>
        <span class="badge b-alta">Prioridad alta</span>
      </div>
    </div>
  </section>

  <section class="landing-section">
    <p class="eyebrow">Cómo funciona</p>
    <h2>Tres momentos, unos 20 minutos</h2>
    <div class="grid grid-3">
      ${[
        ["1", "Cuéntanos de ti", "Tu formación, los temas que te interesan, tu objetivo y el tiempo que tienes. Con eso elegimos el enfoque y el nivel de partida."],
        ["2", "Responde el diagnóstico", "Si aciertas, la dificultad sube; si fallas, revisamos la base del tema. Hay preguntas de opción múltiple y abiertas."],
        ["3", "Recibe tu ruta", "Tu nivel por tema, qué dominas, qué te falta y qué estudiar primero, con la evidencia y las fuentes de cada paso."],
      ].map(([n, t, d]) => html`<article class="card moment"><div class="num">${n}</div><h3>${t}</h3><p class="muted">${d}</p></article>`).join("")}
    </div>
  </section>

  <section class="landing-section grid grid-2">
    <article class="card audience">
      <p class="eyebrow">Si estudias o ejerces derecho</p>
      <h3>Enfoque técnico-jurídico</h3>
      <p class="muted">Preguntas con normas, jurisprudencia y casos para análisis. Ideal para preparar parciales, preparatorios o repasar un área antes de ejercer.</p>
    </article>
    <article class="card audience">
      <p class="eyebrow">Si no eres abogado</p>
      <h3>Enfoque práctico</h3>
      <p class="muted">Situaciones cotidianas en lenguaje claro: tus derechos en el trabajo, cómo se protege un derecho fundamental, qué hacer y a dónde acudir.</p>
    </article>
  </section>

  ${areas.length ? html`<section class="landing-section">
    <p class="eyebrow">Qué se evalúa</p>
    <h2>Áreas disponibles</h2>
    <div class="grid grid-2">${areas.map((a) => html`<article class="card area-card">
      <h3>${esc(a.name)}</h3>
      <p class="muted" style="font-size:.88rem">${(a.subtopics || []).length} temas · eliges cuáles evaluar</p>
      <div class="chips">${(a.subtopics || []).slice(0, 7).map((s) => `<span class="chip chip-static">${esc(s.name)}</span>`).join("")}
        ${(a.subtopics || []).length > 7 ? `<span class="chip chip-static muted">+${a.subtopics.length - 7} más</span>` : ""}</div>
    </article>`).join("")}</div>
  </section>` : ""}

  <section class="card landing-trust">
    <div>
      <p class="eyebrow">Con respaldo</p>
      <h3>Fuentes oficiales y revisión humana</h3>
      <p class="muted">Cada recomendación cita la norma o sentencia que la sustenta. Un administrador académico verifica el informe antes de enviártelo en PDF.</p>
    </div>
    <div>
      <p class="eyebrow">Con IA</p>
      <h3>${aiOn ? "Preguntas a tu medida" : "Banco curado de preguntas"}</h3>
      <p class="muted">${aiOn
        ? `Mientras respondes, la IA (${esc(m.ai.model_generator)}) prepara preguntas nuevas según tu perfil y tus respuestas, y otro modelo las verifica antes de mostrártelas.`
        : "Las preguntas vienen de un banco revisado; la generación con IA está desactivada en este momento."}</p>
    </div>
  </section>

  <section class="landing-cta">
    <h2>¿Listo para saber dónde estás?</h2>
    <p class="muted">Recuerda: Ratio evalúa y orienta, no enseña. No verás si acertaste hasta el final.</p>
    <div class="row" style="justify-content:center">${cta}</div>
  </section>`;
}

// «Iniciar sesión con Google»: se carga el script oficial solo si el servidor tiene GOOGLE_CLIENT_ID.
let gisPromise = null;
const loadGoogle = () => (gisPromise ||= new Promise((ok, fail) => {
  const s = document.createElement("script");
  s.src = "https://accounts.google.com/gsi/client"; s.async = true; s.onload = ok; s.onerror = () => { gisPromise = null; fail(); };
  document.head.appendChild(s);
}));
const afterLogin = (res) => {
  auth.set(res);
  const next = store.get("ratio_next"); store.del("ratio_next");
  location.hash = next || (res.user.role === "admin" ? "#/admin" : "#/perfil");
};

// Google solo acepta el botón en https o en localhost: si se entra por la IP de la red (http://10.x...)
// el botón fallaría con «origin_mismatch», así que no se muestra.
const googleAllowed = () => location.protocol === "https:" || ["localhost"].includes(location.hostname);

async function mountGoogle(clientId, mode) {
  const box = document.getElementById("g-btn");
  if (!box) return;
  if (!googleAllowed()) { box.closest(".google-wrap").hidden = true; return; }
  try { await loadGoogle(); } catch { box.closest(".google-wrap").hidden = true; return; }
  if (!document.body.contains(box)) return;
  google.accounts.id.initialize({
    client_id: clientId, ux_mode: "popup",
    callback: async ({ credential }) => {
      const err = document.getElementById("auth-error");
      try { afterLogin(await api("/auth/google", { method: "POST", body: { credential } })); }
      catch (ex) { if (err) err.textContent = ex.message; }
    },
  });
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  google.accounts.id.renderButton(box, { theme: dark ? "filled_black" : "outline", size: "large", shape: "pill", locale: "es",
    text: mode === "registro" ? "signup_with" : "signin_with", width: Math.min(400, box.clientWidth || 320) });
}

async function viewLogin(wantsRegister) {
  if (auth.user()) { location.hash = "#/historial"; return; }
  const m = await meta();
  let mode = wantsRegister ? "registro" : "ingresar";
  const paint = () => {
    $app.innerHTML = html`
    <div class="auth-layout">
    <aside class="auth-aside">
      <img class="auth-logo" src="img/ratio-icon.png" width="56" height="56" alt="Ratio">
      <p class="eyebrow">${mode === "registro" ? "Crea tu cuenta" : "Bienvenido de nuevo"}</p>
      <h1 class="auth-title">${mode === "registro" ? "Tu diagnóstico jurídico empieza aquí" : "Retoma donde lo dejaste"}</h1>
      <ul class="auth-points">
        <li><strong>Diagnóstico adaptativo.</strong> Las preguntas se ajustan a tu nivel y a los temas que elijas.</li>
        <li><strong>Ruta de estudio.</strong> Qué estudiar primero, por qué y con qué fuentes oficiales.</li>
        <li><strong>Historial guardado.</strong> Con tu correo conservamos cada diagnóstico y puedes continuar uno sin terminar.</li>
      </ul>
    </aside>
    <section class="card stack auth-card">
      <div class="tabs" role="tablist">
        <button class="tab" data-mode="ingresar" aria-selected="${mode === "ingresar"}">Ingresar</button>
        <button class="tab" data-mode="registro" aria-selected="${mode === "registro"}">Crear cuenta</button>
      </div>
      ${m.google_client_id ? html`<div class="google-wrap"><div id="g-btn" class="google-btn"></div><div class="divider">o con tu correo</div></div>` : ""}
      <form class="stack" id="auth-form" autocomplete="on" novalidate>
        ${mode === "registro" ? html`<div><label class="field-label" for="nombre">Nombre completo</label>
          <input type="text" id="nombre" name="nombre" required minlength="2" maxlength="120" autocomplete="name"></div>` : ""}
        <div><label class="field-label" for="email">Correo electrónico</label>
          <input type="email" id="email" name="email" required autocomplete="email" inputmode="email" placeholder="tucorreo@ejemplo.com"></div>
        <div><label class="field-label" for="password">Contraseña</label>
          <input type="password" id="password" name="password" required minlength="6" autocomplete="${mode === "registro" ? "new-password" : "current-password"}">
          ${mode === "registro" ? '<p class="hint" style="margin-top:.4rem">Mínimo 6 caracteres. Tu correo es tu usuario: con él guardamos tu historial de diagnósticos.</p>' : ""}</div>
        <p class="error" id="auth-error" role="alert"></p>
        <button class="btn btn-block" type="submit">${mode === "registro" ? "Crear cuenta" : "Ingresar"}</button>
        <p class="muted" style="font-size:.85rem;text-align:center;margin:0">${mode === "registro"
          ? '¿Ya tienes cuenta? <a href="#" data-switch="ingresar">Ingresa</a>'
          : '¿Primera vez? <a href="#" data-switch="registro">Crea tu cuenta</a>'}</p>
      </form>
    </section>
    </div>`;
    if (m.google_client_id) mountGoogle(m.google_client_id, mode);
    $app.querySelectorAll("[data-switch]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); mode = a.dataset.switch; paint(); }));
    $app.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => { mode = b.dataset.mode; paint(); }));
    document.getElementById("auth-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target; const err = document.getElementById("auth-error");
      const body = { email: f.email.value.trim(), password: f.password.value };
      if (mode === "registro") body.nombre = f.nombre.value.trim();
      if (mode === "registro" && body.nombre.length < 2) { err.textContent = "Escribe tu nombre."; return; }
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(body.email)) { err.textContent = "Escribe un correo válido."; return; }
      if (body.password.length < 6) { err.textContent = "La contraseña debe tener al menos 6 caracteres."; return; }
      try {
        afterLogin(await api(mode === "registro" ? "/auth/register" : "/auth/login", { method: "POST", body }));
      } catch (ex) { err.textContent = ex.message; }
    });
  };
  paint();
}

async function viewHistory() {
  loading();
  const [rows] = await Promise.all([api("/me/sessions"), meta()]);
  const user = auth.user();
  const action = (r) => {
    if (r.status === "en_curso") return html`<a class="btn btn-small" href="#/diagnostico/${esc(r.session_id)}">Continuar</a>`;
    return html`<a class="btn btn-small btn-ghost" href="#/resultado/${esc(r.session_id)}">Ver resultado</a>
      ${reportButton({ ...r, email: { enabled: META?.mail_enabled } })}`;
  };
  $app.innerHTML = html`
  <div class="stack">
    <div class="row between"><div><p class="eyebrow">Mi historial</p><h2>${esc(user.name)}</h2><p class="muted">${esc(user.email)}</p></div>
      <a class="btn" href="#/perfil">Nuevo diagnóstico</a></div>
    ${rows.length ? html`<div class="card table-wrap"><table class="list"><thead><tr><th>Fecha</th><th>Área y temas</th><th>Avance</th><th>Resultado</th><th></th></tr></thead><tbody>
      ${rows.map((r) => html`<tr><td>${fmtDate(r.created_at)}</td>
        <td><strong>${esc(r.area)}</strong><br><small class="muted">${esc(r.topics.join(" · "))}</small></td>
        <td>${r.status === "en_curso" ? `${r.answered} de ~${r.expected}` : "Terminado"}</td>
        <td>${r.level_name ? html`${esc(r.level_name)}<br><span class="badge ${r.download_allowed ? "b-fortaleza" : "b-review"}">${esc(REVIEW[r.review_status] || r.review_status)}</span>` : '<span class="muted">—</span>'}</td>
        <td><div class="row" style="gap:.4rem">${action(r)}</div></td></tr>`).join("")}
    </tbody></table></div>` : '<div class="card empty">Aún no has presentado diagnósticos.</div>'}
    ${accountHtml(user)}
  </div>`;
  bindReportButtons();
  bindAccount(user);
}

// ---------- mi cuenta: contraseña, datos y eliminación (Ley 1581) ----------
function accountHtml(user) {
  const google = user.provider === "google";
  return html`<details class="card account" id="account"><summary><strong>Mi cuenta y mis datos</strong></summary>
    <div class="grid grid-2" style="margin-top:1rem">
      <form class="stack" id="pw-form" novalidate>
        <h3>Cambiar contraseña</h3>
        ${google ? '<p class="hint">Entras con Google; si quieres, crea también una contraseña (deja vacía la actual).</p>' : ""}
        <div><label class="field-label" for="pw-current">Contraseña actual</label>
          <input type="password" id="pw-current" autocomplete="current-password"></div>
        <div><label class="field-label" for="pw-new">Nueva contraseña</label>
          <input type="password" id="pw-new" minlength="8" autocomplete="new-password">
          <p class="hint" style="margin-top:.4rem">Mínimo 8 caracteres. Se cerrará la sesión en tus otros dispositivos.</p></div>
        <p class="error" id="pw-error" role="alert"></p>
        <button class="btn btn-small" type="submit">Guardar contraseña</button>
      </form>
      <div class="stack">
        <h3>Tus datos</h3>
        <p class="muted">Puedes descargar todo lo que Ratio guarda de ti o eliminar tu cuenta y tus diagnósticos. Lee la <a href="#/privacidad">política de privacidad</a>.</p>
        <button class="btn btn-small btn-ghost" type="button" id="export">Descargar mis datos (JSON)</button>
        <form class="stack danger-zone" id="del-form" novalidate>
          <label class="field-label" for="del-confirm">Eliminar mi cuenta</label>
          <p class="hint">Se borran tu cuenta, tus diagnósticos, respuestas e informes. No se puede deshacer. Escribe <strong>ELIMINAR</strong>${google ? "" : " y tu contraseña"}.</p>
          <input type="text" id="del-confirm" autocomplete="off" placeholder="ELIMINAR">
          ${google ? "" : '<input type="password" id="del-pw" autocomplete="current-password" placeholder="Tu contraseña">'}
          <p class="error" id="del-error" role="alert"></p>
          <button class="btn btn-small btn-danger" type="submit">Eliminar definitivamente</button>
        </form>
      </div>
    </div></details>`;
}

function bindAccount(user) {
  const pw = document.getElementById("pw-form");
  if (!pw) return;
  pw.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("pw-error");
    const body = { current: document.getElementById("pw-current").value, new: document.getElementById("pw-new").value };
    if (body.new.length < 8) { err.textContent = "La nueva contraseña debe tener al menos 8 caracteres."; return; }
    try { auth.set(await api("/auth/password", { method: "POST", body })); pw.reset(); err.textContent = ""; toast("Contraseña actualizada"); }
    catch (ex) { err.textContent = ex.message; }
  });
  document.getElementById("export").addEventListener("click", async () => {
    try {
      const data = await api("/me/export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const a = Object.assign(document.createElement("a"), { href: url, download: "mis-datos-ratio.json" });
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (ex) { toast(ex.message); }
  });
  document.getElementById("del-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("del-error");
    const confirm = document.getElementById("del-confirm").value.trim();
    if (confirm !== "ELIMINAR") { err.textContent = "Escribe ELIMINAR para confirmar."; return; }
    try {
      await api("/me/delete", { method: "POST", body: { confirm, password: document.getElementById("del-pw")?.value || "" } });
      auth.clear(); store.del("ratio_queue"); location.hash = "#/"; toast("Tu cuenta y tus datos fueron eliminados");
    } catch (ex) { err.textContent = ex.message; }
  });
}

// ---------- política de privacidad ----------
async function viewPrivacy() {
  const m = await meta();
  $app.innerHTML = html`<article class="card stack prose">
    <p class="eyebrow">Ley 1581 de 2012</p>
    <h1>Política de tratamiento de datos</h1>
    <p><strong>Responsable:</strong> equipo ${esc(m.app_name)} (Hackatón ICESI 2026-II). Contacto: ${esc(m.contact_email || "el correo del administrador")}.</p>
    <h2>Qué datos tratamos</h2>
    <p>Nombre y correo de tu cuenta; tu perfil para el diagnóstico (formación, semestre o profesión, área, temas, objetivo y tiempo disponible); tus respuestas, resultado, ruta de estudio y la revisión del administrador. No pedimos datos sensibles.</p>
    <h2>Para qué</h2>
    <p>Generar tu diagnóstico y tu ruta de estudio, guardar tu historial, permitir que un administrador académico verifique los informes y enviarte el informe por correo.</p>
    <h2>Con quién se comparten (encargados)</h2>
    <ul>
      <li>Base de datos alojada en Supabase (Estados Unidos).</li>
      <li>Servidor de la aplicación (Render, Estados Unidos).</li>
      <li>Proveedor de IA (Groq, Estados Unidos): recibe tu perfil académico (sin nombre ni correo) para generar preguntas a tu medida, y tus respuestas abiertas si se califican con IA.</li>
      <li>Correo saliente (Gmail u otro proveedor) para enviarte el informe; Google si eliges iniciar sesión con Google.</li>
    </ul>
    <p>Al aceptar el consentimiento autorizas esta transmisión internacional para las finalidades descritas.</p>
    <h2>Tus derechos</h2>
    <p>Conocer, actualizar, rectificar y suprimir tus datos, y revocar la autorización. Desde <a href="#/historial">Mi historial → Mi cuenta y mis datos</a> puedes descargar todos tus datos o eliminar tu cuenta en cualquier momento. Para otras consultas o reclamos escribe al contacto indicado; respondemos en los plazos de la ley (10 días hábiles para consultas y 15 para reclamos).</p>
    <h2>Menores de edad</h2>
    <p>Ratio está dirigido a personas mayores de 18 años. Si eres menor de edad, solo puedes usarlo con la autorización de tu representante legal, quien puede ejercer tus derechos sobre los datos (Ley 1581 de 2012, art. 7). Si detectamos una cuenta de un menor sin autorización, la eliminaremos.</p>
    <h2>Conservación</h2>
    <p>Conservamos tus datos mientras tengas la cuenta activa. Si la eliminas, se borran tus diagnósticos, respuestas e informes; las preguntas que la IA generó quedan en el banco sin vínculo contigo.</p>
    <p class="muted">Última actualización: octubre de 2026. Este texto es una base para el proyecto académico y debe revisarlo un asesor antes de un uso real.</p>
  </article>`;
}

// ---------- vistas: diagnóstico ----------
// Formación: la primera vez se elige; después queda en la cuenta y solo puede avanzar
// (subir de semestre o pasar de estudiante a abogado). El servidor aplica la misma regla.
function formationHtml(f, opts) {
  const semesterOpts = (from) => Array.from({ length: 10 - from + 1 }, (_, i) => from + i)
    .map((n) => `<option value="${n}" ${n === from ? "selected" : ""}>${n}.º semestre</option>`).join("");
  if (!f) {
    return html`<fieldset><legend>¿Cuál es tu formación? *</legend>
      <p class="hint">Se guarda en tu cuenta: en los próximos diagnósticos no te la volveremos a preguntar (solo podrás actualizar tu semestre o registrarte como abogado(a)).</p>
      <div class="choices">
        <label class="choice"><input type="radio" name="perfil" value="estudiante"><strong>Estudiante de Derecho</strong><small>Enfoque técnico-jurídico</small></label>
        <label class="choice"><input type="radio" name="perfil" value="abogado"><strong>Abogado(a)</strong><small>Enfoque técnico-jurídico, desde nivel intermedio</small></label>
        <label class="choice"><input type="radio" name="perfil" value="no_abogado"><strong>Otra profesión o sin formación jurídica</strong><small>Enfoque práctico</small></label>
      </div>
    </fieldset>
    <div id="semestre-wrap" hidden><label class="field-label" for="semestre">Semestre que cursas *</label>
      <select id="semestre" name="semestre"><option value="" selected disabled>Selecciona…</option>${opts(10, (n) => `${n}.º semestre`)}</select></div>
    <div id="profesion-wrap" hidden><label class="field-label" for="profesion">¿A qué te dedicas? <span class="muted">(opcional)</span></label>
      <input type="text" id="profesion" name="profesion" maxlength="80" placeholder="Ej.: ingeniera, contador, emprendedor"></div>`;
  }
  if (f.perfil === "estudiante") {
    const from = f.semestre || 1;
    return html`<fieldset class="formation"><legend>Tu formación</legend>
      <p class="formation-saved">Estudiante de Derecho · ${from}.º semestre <span class="muted">(registrada en tu cuenta)</span></p>
      <div class="choices">
        <label class="choice"><input type="radio" name="perfil" value="estudiante" checked><strong>Sigo estudiando Derecho</strong><small>Actualiza tu semestre si avanzaste</small></label>
        <label class="choice"><input type="radio" name="perfil" value="abogado"><strong>Ya me gradué: soy abogado(a)</strong><small>Pasarás a evaluarte desde nivel intermedio</small></label>
      </div>
    </fieldset>
    <div id="semestre-wrap"><label class="field-label" for="semestre">Semestre que cursas *</label>
      <select id="semestre" name="semestre">${semesterOpts(from)}</select></div>
    <div id="profesion-wrap" hidden><input type="hidden" id="profesion" name="profesion" value=""></div>`;
  }
  const isLawyer = f.perfil === "abogado";
  return html`<fieldset class="formation"><legend>Tu formación</legend>
    <p class="formation-saved">${esc(f.label)}${!isLawyer && f.profesion ? ` · ${esc(f.profesion)}` : ""} <span class="muted">(registrada en tu cuenta)</span></p>
    <input type="radio" name="perfil" value="${esc(f.perfil)}" checked hidden>
  </fieldset>
  <div id="semestre-wrap" hidden><select id="semestre" name="semestre"><option value=""></option></select></div>
  <div id="profesion-wrap" ${isLawyer ? "hidden" : ""}><label class="field-label" for="profesion">¿A qué te dedicas? <span class="muted">(opcional)</span></label>
    <input type="text" id="profesion" name="profesion" maxlength="80" value="${esc(f.profesion || "")}" placeholder="Ej.: ingeniera, contador, emprendedor"></div>`;
}

async function viewProfile() {
  loading();
  const [areas, m, me] = await Promise.all([api("/areas"), meta(), api("/auth/me")]);
  const user = auth.user();
  const f = me.formacion;
  const state = { area: null };
  const opts = (n, label) => Array.from({ length: n }, (_, i) => `<option value="${i + 1}">${label(i + 1)}</option>`).join("");
  const areaChoices = areas.map((a) => html`
    <label class="choice"><input type="radio" name="area_id" value="${esc(a.id)}">
      <strong>${esc(a.name)}</strong><small>${esc(a.description)}</small></label>`).join("");
  // autocomplete="off" y ningún valor por defecto: nada aparece preseleccionado.
  $app.innerHTML = html`
  <form class="card stack" id="profile" novalidate autocomplete="off">
    <div><p class="eyebrow">Momento 1 · Perfilar</p><h2>${f ? "Nuevo diagnóstico" : "Cuéntanos sobre ti"}</h2>
      <p class="muted">Diagnóstico de <strong>${esc(user.name)}</strong> (${esc(user.email)}). Con estas respuestas ajustamos el enfoque, el nivel inicial y la ruta a tu tiempo real.</p></div>
    ${formationHtml(f, opts)}
    <fieldset><legend>Área que quieres valorar *</legend><div class="choices">${areaChoices}</div></fieldset>
    <fieldset id="interests-wrap" hidden><legend>¿Sobre qué temas quieres que te evaluemos? *</legend>
      <p class="hint">Te preguntaremos <strong>solo</strong> sobre los temas que marques: 15 preguntas si eliges uno, y 5 más por cada tema adicional (máximo 45).${m.ai.live_generation ? " Mientras respondes, la IA prepara preguntas nuevas a la medida de tu perfil y de tus respuestas." : ""}</p>
      <div class="row" style="margin-bottom:.6rem"><button type="button" class="btn btn-ghost btn-small" id="select-all">Seleccionar todos</button>
        <span class="muted" id="interest-count" style="font-size:.85rem"></span></div>
      <div class="chips" id="interests"></div></fieldset>
    <div><label class="field-label" for="objetivo">¿Para qué quieres aprender? *</label>
      <select id="objetivo" name="objetivo"><option value="" selected disabled>Selecciona…</option>
        <option value="consultorio">Prepararme para consultorio jurídico</option>
        <option value="examen">Preparar un examen o parcial</option>
        <option value="ejercicio">Ejercicio profesional y litigio</option>
        <option value="actualizacion">Actualizarme en reformas recientes</option>
        <option value="conocer_derechos">Conocer mis derechos y cómo reclamarlos</option>
      </select></div>
    <div class="grid grid-2">
      <div><label class="field-label" for="horas">Horas de estudio por semana *</label>
        <select id="horas" name="horas_semana"><option value="" selected disabled>Selecciona…</option>${opts(20, (n) => `${n} h`)}</select></div>
      <div><label class="field-label" for="semanas">Semanas disponibles *</label>
        <select id="semanas" name="semanas"><option value="" selected disabled>Selecciona…</option>${opts(16, (n) => `${n} semana${n > 1 ? "s" : ""}`)}</select></div>
    </div>
    <label class="consent"><input type="checkbox" name="consentimiento">
      <span>Autorizo el tratamiento de mis datos de perfil y respuestas para generar mi diagnóstico, guardar mi historial y que un administrador revise mi ruta, conforme a la Ley 1581 de 2012 y la <a href="#/privacidad" target="_blank" rel="noopener">política de privacidad</a> (incluye el uso de proveedores en el exterior). Declaro que soy mayor de 18 años o que cuento con la autorización de mi representante legal. No se piden datos sensibles.</span></label>
    <p class="error" id="form-error" role="alert"></p>
    <div class="row between"><a href="#/historial" class="btn btn-ghost">Volver</a><button class="btn" type="submit">Empezar el diagnóstico</button></div>
  </form>`;

  const form = document.getElementById("profile");
  form.reset();  // por si el navegador intenta restaurar valores anteriores
  const checked = () => form.querySelectorAll("input[name=intereses]:checked").length;
  const countInterests = () => {
    const n = checked();
    document.getElementById("interest-count").textContent = n ? `${n} tema${n > 1 ? "s" : ""} · ${totalQuestions(n)} preguntas` : "Elige al menos un tema";
  };
  const paintInterests = () => {
    const area = areas.find((a) => a.id === state.area);
    document.getElementById("interests-wrap").hidden = !area;
    document.getElementById("interests").innerHTML = (area?.subtopics || []).map((s) =>
      `<label class="chip" title="${esc((s.concepts || []).join(" · "))}"><input type="checkbox" name="intereses" value="${esc(s.id)}">${esc(s.name)}</label>`).join("");
    countInterests();
  };
  form.addEventListener("change", (e) => {
    if (e.target.name === "area_id") { state.area = e.target.value; paintInterests(); }
    if (e.target.name === "perfil") {
      document.getElementById("semestre-wrap").hidden = e.target.value !== "estudiante";
      document.getElementById("profesion-wrap").hidden = e.target.value !== "no_abogado";
    }
    if (e.target.name === "intereses") countInterests();
  });
  document.getElementById("select-all").addEventListener("click", () => {
    const boxes = [...form.querySelectorAll("input[name=intereses]")];
    const allOn = boxes.every((b) => b.checked);
    boxes.forEach((b) => { b.checked = !allOn; });
    countInterests();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("form-error");
    const perfil = form.querySelector("input[name=perfil]:checked")?.value;
    const area = form.querySelector("input[name=area_id]:checked")?.value;
    const missing = [];
    if (!perfil) missing.push("tu formación");
    if (perfil === "estudiante" && !form.semestre.value) missing.push("el semestre");
    if (!area) missing.push("el área");
    if (area && !checked()) missing.push("al menos un tema");
    if (!form.objetivo.value) missing.push("tu objetivo");
    if (!form.horas.value) missing.push("las horas por semana");
    if (!form.semanas.value) missing.push("las semanas disponibles");
    if (missing.length) { err.textContent = `Falta: ${missing.join(", ")}.`; return; }
    if (!form.consentimiento.checked) { err.textContent = "Necesitamos tu autorización para procesar el diagnóstico."; return; }
    const body = {
      perfil, area_id: area, objetivo: form.objetivo.value,
      semestre: perfil === "estudiante" ? Number(form.semestre.value) : null,
      profesion: perfil === "no_abogado" ? (form.profesion.value.trim() || null) : null,
      intereses: [...form.querySelectorAll("input[name=intereses]:checked")].map((i) => i.value),
      horas_semana: Number(form.horas.value), semanas: Number(form.semanas.value), consentimiento: true,
    };
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const res = await api("/sessions", { method: "POST", body });
      store.set(`ratio_start_${res.session_id}`, {
        area: res.area, approach: res.plan?.approach, topics: body.intereses.length,
        expected: res.progress?.expected, preparing: res.preparing });
      location.hash = `#/instrucciones/${res.session_id}`;
    } catch (ex) { err.textContent = ex.message; btn.disabled = false; }
  });
}

// Pantalla intermedia: instrucciones antes de la primera pregunta.
async function viewInstructions(sid) {
  const m = await meta();
  const info = store.get(`ratio_start_${sid}`, {}) || {};
  const expected = info.expected || 15;
  const approach = info.approach === "tecnico" ? "Técnico-jurídico" : info.approach === "practico" ? "Práctico" : null;
  const aiOn = m.ai.live_generation;
  const rules = [
    ["Sin notas mientras respondes", "No te diremos si acertaste. Ratio mide, no enseña: tus resultados completos aparecen al final."],
    ["Las preguntas se adaptan", "Si aciertas, la dificultad sube; si fallas, revisamos la base del tema. Que una pregunta te parezca fácil o difícil no indica cómo vas."],
    ["Dos tipos de pregunta", "En las de opción múltiple eliges una respuesta. En las abiertas escribes con tus palabras: importa qué elementos jurídicos identificas, no la extensión."],
    ["Responde con honestidad", "Si no sabes, elige la opción que te parezca más razonable, sin buscar en internet. Así tu ruta de estudio será útil de verdad."],
    ["Tu progreso se guarda", "Cada respuesta queda guardada. Si se cae la conexión o cierras la página, puedes continuar desde «Mi historial»."],
  ];
  if (aiOn) rules.splice(2, 0, ["Preguntas a tu medida con IA", "Mientras respondes, la IA prepara preguntas nuevas según tu perfil y tus respuestas. Si ves «un momento…», está terminando una; tarda pocos segundos."]);
  $app.innerHTML = html`
  <section class="card stack instructions">
    <div>
      <p class="eyebrow">Antes de empezar</p>
      <h2>Así funciona tu diagnóstico${info.area ? html` de ${esc(info.area)}` : ""}</h2>
    </div>
    <div class="level-hero">
      <div class="stat"><div class="label">Preguntas</div><div class="value">≈ ${expected}</div></div>
      <div class="stat"><div class="label">Tiempo estimado</div><div class="value">${Math.round(expected * 0.8)}–${Math.round(expected * 1.3)} min</div></div>
      ${info.topics ? html`<div class="stat"><div class="label">Temas</div><div class="value">${info.topics}</div></div>` : ""}
      ${approach ? html`<div class="stat"><div class="label">Enfoque</div><div class="value">${approach}</div></div>` : ""}
    </div>
    <ol class="rules">${rules.map(([t, d]) => html`<li><strong>${t}</strong><span>${d}</span></li>`).join("")}</ol>
    <div class="banner"><strong>Al terminar</strong> verás tu nivel, tus fortalezas, tus vacíos y la ruta de estudio. Si alguna fuente necesita revisión, un administrador la aprueba antes de que puedas descargar el PDF.</div>
    <div class="row" style="justify-content:flex-end">
      <a class="btn btn-lg" id="start" href="${info.preparing ? `#/preparando/${esc(sid)}` : `#/diagnostico/${esc(sid)}`}">Comenzar diagnóstico</a>
    </div>
  </section>`;
  document.getElementById("start").addEventListener("click", () => store.del(`ratio_start_${sid}`));
}

// Pantalla de preparación (solo para temas libres generados por IA).
const STEP_ICON = { pendiente: "○", en_curso: "◌", listo: "✓", error: "✕" };
let prepTimer = null;
async function viewPreparing(sid) {
  clearTimeout(prepTimer);
  const st = await api(`/sessions/${sid}/preparation`);
  if (location.hash !== `#/preparando/${sid}`) return;
  const job = st.job;
  if (!job || (job.status === "listo" && st.ready_to_start)) { location.hash = `#/diagnostico/${sid}`; return; }
  const steps = job.steps.map((s) => html`<li class="step step-${esc(s.status)}">
      <span class="step-icon" aria-hidden="true">${STEP_ICON[s.status] || "○"}</span>
      <span><strong>${esc(s.label)}</strong>${s.detail ? `<br><small>${esc(s.detail)}</small>` : ""}</span>
      <small class="muted">${s.seconds != null ? `${s.seconds} s` : s.status === "en_curso" ? "trabajando…" : ""}</small></li>`).join("");
  $app.innerHTML = html`
  <section class="card stack">
    <div><p class="eyebrow">Preparando tu diagnóstico con IA</p><h2>${esc(st.area)}</h2></div>
    <ol class="steps">${steps}</ol>
    ${st.failed ? html`<div class="banner"><strong>No se pudo preparar el tema.</strong> ${esc(job.error)}</div>` : ""}
    ${!st.failed && st.ready_to_start ? html`<div class="row"><a class="btn" href="#/diagnostico/${esc(sid)}">Empezar ahora</a></div>` : ""}
  </section>`;
  if (!st.failed) prepTimer = setTimeout(() => { if (location.hash === `#/preparando/${sid}`) viewPreparing(sid).catch(showError); }, 1200);
}

function offlineCard(sid, pending) {
  $app.innerHTML = html`<section class="card stack offline-card">
    <p class="eyebrow">Sin conexión</p>
    <h2>Tu progreso está a salvo</h2>
    <p>${pending} respuesta${pending > 1 ? "s quedaron guardadas" : " quedó guardada"} en este dispositivo. En cuanto vuelva la conexión se enviarán y podrás continuar donde ibas.</p>
    <div class="row"><button class="btn" id="retry">Reintentar ahora</button><span class="muted" id="retry-status"></span></div>
  </section>`;
  const retry = async () => {
    document.getElementById("retry-status").textContent = "Intentando…";
    const ok = await queue.flush();
    if (ok && !queue.pendingFor(sid).length) { toast("Respuestas sincronizadas"); render(); }
    else if (document.getElementById("retry-status")) document.getElementById("retry-status").textContent = "Aún sin conexión. Reintentaremos solos.";
  };
  document.getElementById("retry").onclick = retry;
  const timer = setInterval(() => {
    if (location.hash !== `#/diagnostico/${sid}`) { clearInterval(timer); return; }
    if (navigator.onLine) { clearInterval(timer); retry(); }
  }, 4000);
}

const aiRunning = new Set();
async function viewDiagnostic(sid) {
  loading();
  store.set("ratio_active", sid);
  if (queue.pendingFor(sid).length) {
    await queue.flush();
    if (queue.pendingFor(sid).length) { offlineCard(sid, queue.pendingFor(sid).length); return; }
  }
  let data;
  try { data = await api(`/sessions/${sid}/next`); }
  catch (e) { if (e.offline) { offlineCard(sid, queue.pendingFor(sid).length || 0); return; } throw e; }
  // En la web (serverless) la IA corre en su propia solicitud: se pide sin esperar la respuesta.
  if (data.ai?.pending && !aiRunning.has(sid)) {
    aiRunning.add(sid);
    api(`/sessions/${sid}/ai/run`, { method: "POST" }).catch(() => {}).finally(() => aiRunning.delete(sid));
  }
  if (data.waiting) {
    $app.innerHTML = html`<section class="card"><p class="eyebrow">Momento 2 · Diagnosticar</p>
      <h2>Un momento…</h2><p class="muted">${esc(data.message)}</p><div class="spinner"></div></section>`;
    setTimeout(() => { if (location.hash === `#/diagnostico/${sid}`) render(); }, 1500);
    return;
  }
  if (data.finished) {
    $app.innerHTML = '<div class="card"><h2>Analizando tus respuestas…</h2><div class="spinner"></div></div>';
    await api(`/sessions/${sid}/finish`, { method: "POST" });
    store.del("ratio_active");
    location.hash = `#/resultado/${sid}`;
    return;
  }
  const q = data.question;
  const p = q.progress;
  const ratio = Math.min(100, Math.round((p.answered / Math.max(1, p.expected)) * 100));
  const optionsHtml = q.kind === "mc"
    ? `<div class="options" role="group" aria-label="Opciones de respuesta">${q.options.map((o) => html`
        <button type="button" class="option" data-key="${esc(o.key)}" aria-pressed="false"><span class="key">${esc(o.key)}</span><span>${esc(o.text)}</span></button>`).join("")}</div>`
    : html`<label class="field-label" for="open-answer">Tu respuesta</label>
        <p class="hint">Responde con tus palabras. Se valora qué elementos jurídicos identificas, no la extensión.</p>
        <textarea id="open-answer" maxlength="4000" placeholder="Escribe tu análisis…"></textarea>`;
  $app.innerHTML = html`
  <section class="card">
    <p class="eyebrow">Momento 2 · Diagnosticar</p>
    <div class="progress" aria-label="Progreso"><span style="width:${ratio}%"></span></div>
    <div class="question-meta"><span>${esc(q.subtopic)}</span><span>Pregunta ${p.answered + 1} de ${p.expected}</span></div>
    ${q.tailored ? '<p class="ai-tag" style="margin:-.4rem 0 .9rem">✦ Pregunta generada por IA a la medida de tu perfil</p>' : ""}
    <p class="stem">${esc(q.stem)}</p>
    ${optionsHtml}
    <div class="row between" style="margin-top:1.3rem">
      <span class="muted" style="font-size:.85rem">${q.kind === "open" ? "Pregunta abierta" : "Elige una opción"}</span>
      <button class="btn" id="next" disabled>Siguiente</button>
    </div>
    <p class="note">No te mostramos si acertaste: el objetivo es medir, no enseñar. Tu progreso se guarda con cada respuesta: si se cae la conexión o cierras la página, puedes continuar desde «Mi historial».
      ${data.ai && data.ai.enabled ? `<br><span class="ai-live ${data.ai.active ? "on" : ""}">${data.ai.active
        ? "La IA está preparando preguntas según tus respuestas…"
        : `IA activa · ${data.ai.tailored_questions} preguntas generadas para ti`}</span>` : ""}</p>
  </section>`;

  let answer = "";
  const next = document.getElementById("next");
  if (q.kind === "mc") {
    $app.querySelectorAll(".option").forEach((b) => b.addEventListener("click", () => {
      $app.querySelectorAll(".option").forEach((x) => x.setAttribute("aria-pressed", "false"));
      b.setAttribute("aria-pressed", "true"); answer = b.dataset.key; next.disabled = false;
    }));
  } else {
    const ta = document.getElementById("open-answer");
    ta.addEventListener("input", () => { answer = ta.value.trim(); next.disabled = answer.length < 15; });
  }
  next.addEventListener("click", async () => {
    next.disabled = true;
    const item = { sid, question_id: q.id, answer, at: Date.now() };
    queue.add(item);  // primero se guarda en el dispositivo; luego se envía
    try {
      await api(`/sessions/${sid}/answers`, { method: "POST", body: { question_id: q.id, answer } });
      store.set("ratio_queue", queue.all().filter((x) => !(x.sid === sid && x.question_id === q.id)));
      render();
    } catch (ex) {
      if (ex.offline) { offlineCard(sid, queue.pendingFor(sid).length); return; }
      store.set("ratio_queue", queue.all().filter((x) => !(x.sid === sid && x.question_id === q.id)));
      toast(ex.message); next.disabled = false;
    }
  });
}

// ---------- vistas: resultado ----------
function sourceList(sources) {
  return `<ul class="sources">${(sources || []).map((s) => html`<li><span class="kind">${esc(KIND[s.kind] || s.kind || "Fuente")}</span>
    ${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.citation)}</a>` : esc(s.citation)}
    ${s.detail ? `<br><small>${esc(s.detail)}</small>` : ""}</li>`).join("")}</ul>`;
}

// Informes antiguos citaban preguntas por su código interno: lo cambiamos por el enunciado.
let QSTEMS = {};
const QID = /(La pregunta|Pregunta|Respuesta abierta) ([0-9a-f]{32})( fue generada por IA\.|:)?/g;
const shortStem = (t) => { t = (t || "").replace(/\s+/g, " ").trim(); return t.length > 180 ? `${t.slice(0, 180)}…` : t; };
function flagText(text) {
  return (text || "").replace(QID, (m, kind, id) => (QSTEMS[id] ? `«${shortStem(QSTEMS[id])}»${kind === "Respuesta abierta" ? " (respuesta abierta)" : ""}` : m));
}
const flagQid = (f) => f.question_id || ((f.text || "").match(/[0-9a-f]{32}/) || [])[0];

// Para la persona evaluada: un resumen corto en vez de la lista técnica de marcas.
function flagSummary(flags) {
  const ia = new Set(flags.filter((f) => f.type === "pregunta_generada_por_ia").map(flagQid));
  const parts = [];
  if (ia.size) parts.push(`${ia.size} ${ia.size === 1 ? "pregunta creada" : "preguntas creadas"} por IA`);
  const risky = flags.filter((f) => f.type === "pregunta_con_riesgo" && !ia.has(flagQid(f))).length;
  if (risky) parts.push(`${risky} ${risky === 1 ? "pregunta" : "preguntas"} con un punto por confirmar`);
  const src = flags.filter((f) => ["fuente_con_riesgo", "fuente_sin_verificar", "subtema_en_transicion"].includes(f.type)).length;
  if (src) parts.push(`${src} ${src === 1 ? "fuente" : "fuentes"} por confirmar`);
  const other = flags.filter((f) => ["calificacion_automatica", "evidencia_insuficiente", "resultado_inconsistente"].includes(f.type)).length;
  if (other) parts.push(`${other} ${other === 1 ? "calificación" : "calificaciones"} por confirmar`);
  return parts.join(", ");
}

function flagList(flags) {
  if (!flags.length) return '<p class="muted">Sin marcas.</p>';
  return `<ul class="flags">${flags.map((f) => html`<li class="sev-${esc(f.severity)}"><strong>${esc(FLAG[f.type] || f.type)}:</strong> ${esc(flagText(f.text))}</li>`).join("")}</ul>`;
}

function routeItemHtml(item, { admin = false } = {}) {
  const evidenceRows = item.justification.evidence.map((e) => html`<tr><td>${esc(e.concept)}</td><td>Nivel ${e.level}</td><td>${e.correct ? "Acierto" : "Error"}</td></tr>`).join("");
  const serious = item.review_flags.filter((f) => f.severity !== "baja");
  return html`
  <li class="route-item status-${esc(item.status)}">
    <span class="route-step">${item.step}</span>
    <article class="card">
      <div class="row between"><h3>${esc(item.subtopic)}</h3>
        <div class="row" style="gap:.4rem"><span class="badge b-${esc(item.status)}">${esc(STATUS[item.status])}</span>
        <span class="badge b-${esc(item.priority)}">${esc(PRIORITY[item.priority])}</span></div></div>
      <p>${esc(item.objective)}</p>
      <dl class="kv">
        <dt>Acción</dt><dd>${esc(item.action)} hasta nivel ${["", "básico", "intermedio", "avanzado"][item.target_level]}</dd>
        <dt>Enfócate en</dt><dd>${esc((item.focus_concepts || []).join(", "))}</dd>
        <dt>Dedicación</dt><dd>${item.hours} h · semana ${item.weeks[0]}${item.weeks[1] !== item.weeks[0] ? `–${item.weeks[1]}` : ""}${item.within_time ? "" : " · <em>fuera de tu tiempo disponible (opcional)</em>"}</dd>
      </dl>
      <strong style="font-size:.9rem">Fuentes sugeridas</strong>
      ${sourceList(item.sources)}
      ${item.teacher_note ? html`<div class="banner ok" style="margin-top:.8rem"><strong>Nota del administrador:</strong> ${esc(item.teacher_note)}</div>` : ""}
      ${(item.admin_comments || []).length ? html`<ul class="flags" style="margin-top:.6rem">${item.admin_comments.map((c) => html`<li class="sev-baja"><strong>Revisión:</strong> ${esc(c.comment)}</li>`).join("")}</ul>` : ""}
      <details><summary>¿Por qué está en tu ruta?</summary>
        <p>${esc(item.justification.text)}</p>
        ${item.justification.errors.length ? `<p><strong>Errores detectados:</strong></p><ul>${item.justification.errors.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
        <div class="table-wrap"><table class="evidence"><thead><tr><th>Concepto</th><th>Nivel</th><th>Resultado</th></tr></thead><tbody>${evidenceRows}</tbody></table></div>
      </details>
      ${admin ? html`<details><summary>Marcas para revisión (${item.review_flags.length})</summary>${flagList(item.review_flags)}</details>`
        : serious.length && !item.reviewed ? html`<p class="pending-note"><strong>En revisión:</strong> un administrador está confirmando ${esc(flagSummary(serious))} antes de habilitar tu informe.</p>` : ""}
    </article>
  </li>`;
}

function resultSummary(report, { admin = false } = {}) {
  const g = report.result.global;
  const subs = Object.values(report.result.subtopics).filter((s) => s.status !== "sin_evidencia");
  const bars = subs.map((s) => html`
    <div class="bar-row"><span>${esc(s.name)}</span><span class="badge b-${esc(s.status)}">${esc(STATUS[s.status])}${s.mastery != null ? ` · ${pct(s.mastery)}` : ""}</span>
      <div class="bar"><span class="fill-${esc(s.status)}" style="width:${Math.max(3, Math.round((s.mastery ?? 0) * 100))}%"></span></div>
      <small class="muted" style="grid-column:1/-1">Nivel alcanzado: ${esc(s.level_name || "—")} · ${s.correct ?? 0}/${s.answered} aciertos · confianza ${esc(s.confidence)}</small></div>`).join("");
  return html`
  <section class="card stack">
    <div><p class="eyebrow">Momento 3 · Clasificar</p><h2>${admin ? `Diagnóstico de ${esc(report.profile.nombre || "usuario")}` : `${esc((report.profile.nombre || "").split(" ")[0] || "Este")}, este es tu diagnóstico`}</h2>
      <p class="muted">${esc(report.profile.perfil_label)} · ${esc(report.profile.objetivo_label)} · ${esc(report.area_name)}${admin && report.profile.email ? ` · ${esc(report.profile.email)}` : ""}</p></div>
    <div class="level-hero">
      <div class="stat"><div class="label">Nivel global</div><div class="value">${esc(g.level_name)}</div></div>
      <div class="stat"><div class="label">Enfoque</div><div class="value">${esc(g.approach_label)}</div></div>
      <div class="stat"><div class="label">Dominio ponderado</div><div class="value">${pct(g.mastery)}</div></div>
    </div>
    ${g.ceiling_note ? `<p class="muted">${esc(g.ceiling_note)}</p>` : ""}
    <h3>Fortalezas y vacíos por tema</h3>
    <div class="bars">${bars}</div>
  </section>`;
}

function questionsHtml(report) {
  const rows = (report.questions || []).map((q) => html`<tr><td>${q.seq}</td><td>${esc(q.subtopic)}</td><td>${esc(q.stem)}${q.tailored ? ' <span class="ai-tag">IA</span>' : ""}</td>
    <td>${["", "Básico", "Intermedio", "Avanzado"][q.level]}</td><td>${q.correct ? '<span class="badge b-fortaleza">Acierto</span>' : '<span class="badge b-vacio">Error</span>'}</td></tr>`).join("");
  return html`<details class="card"><summary>Preguntas que respondiste (${(report.questions || []).length})</summary>
    <div class="table-wrap"><table class="evidence"><thead><tr><th>#</th><th>Tema</th><th>Pregunta</th><th>Nivel</th><th>Resultado</th></tr></thead><tbody>${rows}</tbody></table></div></details>`;
}

function traceHtml(report) {
  return html`<details class="card"><summary>Cómo llegamos a este diagnóstico (${report.result.trace.length} decisiones del motor)</summary>
    <ol class="trace">${report.result.trace.map((t) => html`<li class="ev-${esc(t.event)}">${esc(t.text)}</li>`).join("")}</ol>
    <p class="muted" style="margin-top:.8rem">${esc(report.result.plan.rationale.join(" "))}</p></details>`;
}

function emailLine(report) {
  const e = report.email || {};
  if (!e.enabled) return "";
  if (e.status === "enviado") return ` Te lo enviamos en PDF a ${esc(e.to)} (${fmtDate(e.sent_at)}).`;
  if (e.status === "enviando") return ` Lo estamos enviando en PDF a ${esc(e.to)}.`;
  if (e.status === "error") return " No pudimos enviarlo a tu correo; usa el botón «Enviar PDF a mi correo» o avisa al administrador.";
  return "";
}

function reviewBanner(report) {
  const r = report.review;
  const mail = emailLine(report);
  if (report.download_allowed && r.status === "no_requiere") return html`<div class="banner ok"><strong>Tu informe está listo.</strong> No tenía elementos que requirieran revisión.${mail}</div>`;
  if (report.download_allowed) return html`<div class="banner ok"><strong>Informe aprobado por ${esc(r.reviewed_by || "el administrador")}${r.reviewed_at ? ` (${fmtDate(r.reviewed_at)})` : ""}.</strong> ${esc(r.notes)}${mail}</div>`;
  return html`<div class="banner"><strong>Tu informe está en verificación.</strong> Un administrador académico lo revisa antes de entregarlo. ${report.email?.enabled ? html`Cuando lo apruebe, te enviaremos el informe en PDF a <strong>${esc(report.email.to)}</strong>.` : "Cuando lo apruebe podrás descargar el PDF desde «Mi historial»."}</div>`;
}

async function viewResult(sid) {
  loading();
  const report = await api(`/sessions/${sid}/report`);
  $app.innerHTML = html`
  <div class="stack">
    ${resultSummary(report)}
    <section class="stack">
      <div class="row between"><div><p class="eyebrow">Momentos 4 y 5 · Orientar y justificar</p><h2>Tu ruta de aprendizaje</h2></div>
        ${reportButton(report)}</div>
      ${reviewBanner(report)}
      <ol class="route">${report.route.map((i) => routeItemHtml(i)).join("")}</ol>
    </section>
    ${questionsHtml(report)}
    <div class="row"><a class="btn" href="#/perfil">Nuevo diagnóstico</a><a class="btn btn-ghost" href="#/historial">Mi historial</a></div>
  </div>`;
  bindReportButtons();
}

// Informe imprimible: se guarda como PDF con el diálogo de impresión del navegador.
async function viewPrintable(sid) {
  loading();
  const r = await api(`/sessions/${sid}/report`);
  if (!r.download_allowed) { location.hash = `#/resultado/${sid}`; toast("El informe aún no ha sido aprobado"); return; }
  const g = r.result.global;
  const subs = Object.values(r.result.subtopics).filter((s) => s.status !== "sin_evidencia");
  $app.innerHTML = html`
  <article class="printable">
    <div class="no-print row between" style="margin-bottom:1rem"><a href="#/resultado/${esc(sid)}">← Volver</a>
      <button class="btn" type="button" data-print>Descargar / guardar como PDF</button></div>
    <header class="doc-head">
      <div><img class="doc-logo" src="img/ratio-icon.png" width="56" height="56" alt="Ratio"></div>
      <div><h1>Informe de diagnóstico jurídico</h1>
        <p>${esc(r.profile.nombre)} · ${esc(r.profile.email || "")}<br>${esc(r.area_name)} · ${esc(r.profile.perfil_label)} · ${esc(r.profile.objetivo_label)}<br>
        Presentado el ${fmtDate(r.created_at)}</p></div>
    </header>
    <section><h2>Resultado</h2>
      <table class="doc-table"><tr><th>Nivel global</th><td>${esc(g.level_name)}</td><th>Enfoque</th><td>${esc(g.approach_label)}</td><th>Dominio</th><td>${pct(g.mastery)}</td></tr></table>
      <table class="doc-table"><thead><tr><th>Tema</th><th>Estado</th><th>Nivel alcanzado</th><th>Aciertos</th></tr></thead><tbody>
        ${subs.map((s) => html`<tr><td>${esc(s.name)}</td><td>${esc(STATUS[s.status])}</td><td>${esc(s.level_name)}</td><td>${s.correct}/${s.answered}</td></tr>`).join("")}
      </tbody></table></section>
    <section><h2>Ruta de aprendizaje</h2>
      ${r.route.map((i) => html`<div class="doc-item"><h3>${i.step}. ${esc(i.subtopic)} <small>(${esc(PRIORITY[i.priority])} · ${i.hours} h)</small></h3>
        <p>${esc(i.objective)}</p>
        <p><strong>Enfócate en:</strong> ${esc((i.focus_concepts || []).join(", "))}</p>
        <p><strong>Fuentes:</strong> ${(i.sources || []).map((s) => esc(s.citation)).join("; ")}</p>
        ${i.teacher_note ? html`<p><strong>Nota del administrador:</strong> ${esc(i.teacher_note)}</p>` : ""}
        ${(i.admin_comments || []).map((c) => html`<p><strong>Revisión:</strong> ${esc(c.comment)}</p>`).join("")}
        <p class="doc-why"><strong>Por qué:</strong> ${esc(i.justification.text)}</p></div>`).join("")}</section>
    <section><h2>Preguntas respondidas</h2>
      <table class="doc-table"><thead><tr><th>#</th><th>Tema</th><th>Pregunta</th><th>Resultado</th></tr></thead><tbody>
        ${(r.questions || []).map((q) => html`<tr><td>${q.seq}</td><td>${esc(q.subtopic)}</td><td>${esc(q.stem)}</td><td>${q.correct ? "Acierto" : "Error"}</td></tr>`).join("")}
      </tbody></table></section>
    <footer class="doc-foot">${r.review.status === "no_requiere" ? "Informe generado automáticamente: no tenía elementos que requirieran revisión."
      : html`Revisado y aprobado por ${esc(r.review.reviewed_by)} (${fmtDate(r.review.reviewed_at)}).${r.review.notes ? ` Observaciones: ${esc(r.review.notes)}` : ""}`}
      <br>Ratio evalúa y orienta; no reemplaza la asesoría de un abogado.</footer>
  </article>`;
  setTimeout(() => window.print(), 500);
}

// ---------- vistas: administrador ----------
async function viewAdmin() {
  loading();
  const tab = store.get("ratio_tab", "rutas");
  $app.innerHTML = html`
  <div class="stack">
    <div><p class="eyebrow">Administración</p><h2>Revisión de informes y calidad del banco</h2></div>
    <div class="tabs" role="tablist">
      ${[["rutas", "Informes por aprobar"], ["todos", "Todos los informes"], ["banco", "Banco de preguntas"], ["riesgos", "Riesgos normativos"], ["ia", "Consumo de IA"]].map(([id, label]) =>
        `<button class="tab" role="tab" data-tab="${id}" aria-selected="${tab === id}">${label}</button>`).join("")}
    </div>
    <div id="tab-body"><div class="spinner"></div></div>
  </div>`;
  $app.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => { store.set("ratio_tab", b.dataset.tab); viewAdmin(); }));
  const body = document.getElementById("tab-body");
  if (tab === "rutas" || tab === "todos") await tabReports(body, tab === "rutas");
  if (tab === "banco") await tabBank(body);
  if (tab === "riesgos") await tabRisks(body);
  if (tab === "ia") await tabUsage(body);
}

// Consumo de IA: para vigilar la cuota del plan gratuito del proveedor (Groq).
async function tabUsage(body) {
  const u = await api("/teacher/ai-usage");
  const n = (x) => Number(x || 0).toLocaleString("es-CO");
  const lim = u.limits || {};
  const row = (label, limit, remaining, reset) => limit
    ? html`<tr><td>${label}</td><td>${n(remaining)} de ${n(limit)}</td><td>${esc(reset || "—")}</td></tr>` : "";
  const perDiag = u.sessions_with_ai_24h ? Math.round(u.total_tokens / Math.max(1, u.sessions_with_ai_24h)) : null;
  body.innerHTML = html`<div class="stack">
    ${u.quota_blocked ? html`<div class="banner"><strong>IA en pausa por cuota:</strong> ${esc(u.quota_blocked)} Mientras tanto se usa el banco de preguntas.</div>` : ""}
    <div class="level-hero">
      <div class="stat"><div class="label">Tokens usados</div><div class="value">${n(u.total_tokens)}</div><small class="muted">en ${u.hours} h desde el último arranque</small></div>
      <div class="stat"><div class="label">Llamadas a la IA</div><div class="value">${n(u.calls)}</div><small class="muted">${n(u.errors_429)} rechazadas por límite (429)</small></div>
      <div class="stat"><div class="label">Diagnósticos con IA (24 h)</div><div class="value">${n(u.sessions_with_ai_24h)}</div><small class="muted">${n(u.batches_24h)} lotes${perDiag ? ` · ≈ ${n(perDiag)} tokens c/u` : ""}</small></div>
    </div>
    <div class="card table-wrap"><h3>Límites que informa el proveedor</h3>
      ${Object.keys(lim).length ? html`<table class="list"><thead><tr><th>Límite</th><th>Disponible</th><th>Se renueva en</th></tr></thead><tbody>
        ${row("Solicitudes por día", lim["limit-requests"], lim["remaining-requests"], lim["reset-requests"])}
        ${row("Tokens por minuto", lim["limit-tokens"], lim["remaining-tokens"], lim["reset-tokens"])}
      </tbody></table>` : '<p class="muted">Aún no hay llamadas desde el último arranque del servidor.</p>'}
      <p class="hint" style="margin-top:.8rem">El límite diario de tokens no llega en estas cabeceras: revísalo en console.groq.com → Settings → Limits (y el gasto en Usage). Modelo: ${esc(u.model || "—")}.</p>
    </div></div>`;
}

async function tabReports(body, onlyPending) {
  let rows = await api("/teacher/reports");
  if (onlyPending) rows = rows.filter((r) => ["pendiente", "en_revision"].includes(r.review_status));
  if (!rows.length) { body.innerHTML = `<div class="card empty">${onlyPending ? "No hay informes esperando aprobación." : "Aún no hay diagnósticos terminados."}</div>`; return; }
  body.innerHTML = html`<div class="card table-wrap"><table class="list"><thead><tr><th>Usuario</th><th>Área</th><th>Nivel</th><th>Lista de verificación</th><th>Estado</th><th></th></tr></thead><tbody>
    ${rows.map((r) => html`<tr><td>${esc(r.nombre)}<br><small>${esc(r.email)}</small></td><td>${esc(r.area_id)}</td><td>${esc(r.level_name)}</td>
      <td>${r.checklist_total ? `${r.checklist_done} de ${r.checklist_total}` : "—"}</td>
      <td><span class="badge ${["pendiente", "en_revision"].includes(r.review_status) ? "b-review" : "b-fortaleza"}">${esc(REVIEW[r.review_status] || r.review_status)}</span></td>
      <td><a class="btn btn-small" href="#/admin/informe/${esc(r.session_id)}">Revisar</a></td></tr>`).join("")}
  </tbody></table></div>`;
}

async function tabBank(body) {
  const [m, areas, questions] = await Promise.all([meta(), api("/areas"), api("/teacher/questions")]);
  const subOptions = areas.flatMap((a) => a.subtopics.map((s) => `<option value="${esc(s.id)}">${esc(a.name)} · ${esc(s.name)}</option>`)).join("");
  // Solo se revisan a mano las de riesgo alto; las verificadas por la IA con riesgo bajo o medio quedan probadas.
  const open = (q) => q.status === "pendiente" || q.status === "verificada";
  const pending = questions.filter((q) => open(q) && q.risk_level === "alta");
  const proven = questions.filter((q) => open(q) && q.risk_level !== "alta");
  const counts = questions.reduce((acc, q) => ((acc[q.status] = (acc[q.status] || 0) + 1), acc), {});
  body.innerHTML = html`
  <div class="stack">
    <form class="card stack" id="gen">
      <div><h3>Generar preguntas con IA</h3><p class="muted">La IA redacta a partir del fragmento de una fuente de la lista cerrada; un verificador independiente la revisa. ${m.ai.generator ? "" : "<strong>Sin proveedor configurado.</strong>"}</p></div>
      <div class="grid grid-3">
        <div><label class="field-label" for="g-sub">Tema</label><select id="g-sub" name="subtopic_id">${subOptions}</select></div>
        <div><label class="field-label" for="g-level">Nivel</label><select id="g-level" name="level"><option value="1">Básico</option><option value="2">Intermedio</option><option value="3">Avanzado</option></select></div>
        <div><label class="field-label" for="g-ap">Enfoque</label><select id="g-ap" name="approach"><option value="ambos">Ambos</option><option value="tecnico">Técnico</option><option value="practico">Práctico</option></select></div>
      </div>
      <div><button class="btn" ${m.ai.generator ? "" : "disabled"}>Generar y verificar</button></div>
    </form>
    <p class="muted">Banco: ${counts.aprobada || 0} aprobadas · ${proven.length} verificadas por IA y probadas · ${pending.length} de riesgo alto por revisar · ${counts.rechazada || 0} rechazadas.</p>
    <h3>Riesgo alto: por revisar (${pending.length})</h3>
    <p class="muted" style="margin-top:-.4rem">Las preguntas que el verificador aprobó con riesgo bajo o medio ya quedan probadas y no necesitan tu revisión.</p>
    <div class="stack">${pending.length ? pending.map((q) => questionCard(q, { review: true })).join("") : '<div class="card empty">No hay preguntas de riesgo alto por revisar.</div>'}</div>
    <details class="card"><summary>Verificadas por IA y probadas (${proven.length})</summary>
      <div class="stack">${proven.map((q) => questionCard(q)).join("")}</div></details>
    <details class="card"><summary>Rechazadas por el verificador (${counts.rechazada || 0})</summary>
      <div class="stack">${questions.filter((q) => q.status === "rechazada").map((q) => questionCard(q)).join("")}</div></details>
    <details class="card"><summary>Banco aprobado (${counts.aprobada || 0})</summary>
      <div class="stack">${questions.filter((q) => q.status === "aprobada").map((q) => questionCard(q)).join("")}</div></details>
  </div>`;
  document.getElementById("gen").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target; const btn = f.querySelector("button"); btn.disabled = true; btn.textContent = "Generando…";
    try {
      await api("/teacher/questions/generate", { method: "POST", body: { subtopic_id: f.subtopic_id.value, level: Number(f.level.value), approach: f.approach.value } });
      toast("Pregunta generada"); tabBank(body);
    } catch (ex) { toast(ex.message); btn.disabled = false; btn.textContent = "Generar y verificar"; }
  });
  body.querySelectorAll("[data-decision]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/teacher/questions/${b.dataset.id}/decision`, { method: "POST", body: { decision: b.dataset.decision } });
    toast(b.dataset.decision === "aprobada" ? "Pregunta aprobada" : "Pregunta rechazada"); tabBank(body);
  }));
}

function questionCard(q, { review = false } = {}) {
  const opts = q.kind === "mc"
    ? `<ol class="opts" type="A">${q.options.map((o) => html`<li class="${o.key === q.correct_key ? "correct" : ""}">${esc(o.text)}${o.misconception ? `<small class="muted">Diagnostica: ${esc(o.misconception)}</small>` : ""}</li>`).join("")}</ol>`
    : `<ul class="opts">${q.rubric.map((r) => `<li>${esc(r.criterion)}</li>`).join("")}</ul>`;
  return html`<article class="card qcard">
    <div class="row between"><small class="muted">${esc(q.subtopic)} · nivel ${q.level} · ${esc(q.approach)} · ${q.origin === "ia" ? "IA" : "curada"}</small>
      <span class="badge ${q.status === "aprobada" ? "b-fortaleza" : q.status === "rechazada" ? "b-vacio" : "b-review"}">${esc(q.status === "verificada" ? (q.risk_level === "alta" ? "verificada por IA" : "probada (IA)") : q.status)}</span></div>
    <p style="margin:.5rem 0 0;font-weight:600">${esc(q.stem)}</p>${opts}
    <p class="muted" style="font-size:.85rem;margin-top:.6rem">Fuente: ${esc(q.source || "—")}${q.risk_level !== "ninguno" ? ` · <span class="badge b-${esc(q.risk_level)}">riesgo ${esc(q.risk_level)}</span>` : ""}</p>
    ${q.verifier_note ? `<p class="muted" style="font-size:.85rem">Verificador: ${esc(q.verifier_note)}</p>` : ""}
    ${review ? `<div class="row"><button class="btn btn-small" data-decision="aprobada" data-id="${esc(q.id)}">Aprobar</button><button class="btn btn-ghost btn-small" data-decision="rechazada" data-id="${esc(q.id)}">Rechazar</button></div>` : ""}
  </article>`;
}

async function tabRisks(body) {
  const r = await api("/teacher/risks");
  const order = { alta: 0, media: 1, baja: 2, ninguno: 3 };
  r.sources.sort((a, b) => (order[a.risk_level] ?? 3) - (order[b.risk_level] ?? 3));
  const safe = r.sources.filter((s) => s.risk_level === "ninguno");
  const link = (s) => (s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.citation)}</a>` : esc(s.citation));
  const risk = (s) => `<span class="badge b-${s.risk_level === "ninguno" ? "baja" : esc(s.risk_level)}">${s.risk_level === "ninguno" ? "sin riesgo" : `riesgo ${esc(s.risk_level)}`}</span>`;
  body.innerHTML = html`
  <div class="stack">
    <div class="card"><h3>Temas en transición normativa</h3>
      <ul class="flags">${r.subtopics.map((s) => html`<li class="sev-${esc(s.risk_level)}"><strong>${esc(s.name)}</strong> (${esc(s.area_id)}): ${esc(s.note)}</li>`).join("")}</ul></div>
    <div class="card stack"><div class="row between"><h3>Fuentes por verificar</h3><span class="badge b-review">${r.sources.length} pendientes</span></div>
      <p class="muted" style="margin:0">Abre el enlace, confirma que la norma o sentencia es correcta y vigente, y márcala como verificada. Sale de esta lista y deja de pedir revisión en los informes nuevos. Las de riesgo alto o medio van primero.</p>
      ${safe.length ? html`<div><button class="btn btn-small btn-ghost" type="button" id="confirm-safe">✓ Marcar como verificadas las ${safe.length} fuentes sin riesgo</button></div>` : ""}
      ${r.sources.length ? r.sources.map((s) => html`<div class="check-item src-item">
        <div><strong>${link(s)}</strong> ${risk(s)}${s.note ? `<br><small class="muted">${esc(s.note)}</small>` : ""}</div>
        <button class="btn btn-small" type="button" data-confirm="${esc(s.id)}">✓ Fuente verificada</button></div>`).join("")
        : '<div class="empty">No hay fuentes pendientes por verificar.</div>'}</div>
    <details class="card"><summary>Fuentes ya verificadas por un administrador (${r.confirmed.length})</summary>
      <div class="stack" style="margin-top:.6rem">${r.confirmed.map((s) => html`<div class="check-item src-item">
        <div><strong>${link(s)}</strong><br><small class="muted">Verificada por ${esc(s.confirmed_by || "administrador")} · ${fmtDate(s.confirmed_on)}</small></div>
        <button class="btn btn-small btn-ghost" type="button" data-unconfirm="${esc(s.id)}">Deshacer</button></div>`).join("")}</div></details>
  </div>`;
  const act = (attr, confirm, msg) => body.querySelectorAll(`[${attr}]`).forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await api(`/teacher/sources/${encodeURIComponent(b.getAttribute(attr))}/confirm`, { method: "POST", body: { confirm } }); toast(msg); tabRisks(body); }
    catch (e) { toast(e.message); b.disabled = false; }
  }));
  act("data-confirm", true, "Fuente marcada como verificada");
  const all = document.getElementById("confirm-safe");
  if (all) all.onclick = async () => {
    all.disabled = true; all.textContent = "Marcando…";
    try {
      for (const s of safe) await api(`/teacher/sources/${encodeURIComponent(s.id)}/confirm`, { method: "POST", body: { confirm: true } });
      toast(`${safe.length} fuentes marcadas como verificadas`);
    } catch (e) { toast(e.message); }
    tabRisks(body);
  };
  act("data-unconfirm", false, "Fuente devuelta a la lista de pendientes");
}

function aiBatchesHtml(batches) {
  if (!batches.length) return "";
  const rows = batches.map((b) => html`<tr><td>${b.id}</td><td>Tras ${b.after_answers} respuestas</td>
    <td>${b.targets.map((t) => html`<strong>${esc(t.name)}</strong> · nivel ${t.level} · ${t.count} preg.<br><small>${esc(t.reason)}${t.focus.length ? ` — foco: ${esc(t.focus.join("; "))}` : ""}</small>`).join("<br>")}</td>
    <td>${b.status === "en_curso" ? "Generando…" : `${b.verified}/${b.generated} aprobadas`}${b.seconds != null ? `<br><small>${b.seconds} s</small>` : ""}${b.error ? `<br><small class="error">${esc(b.error.slice(0, 160))}</small>` : ""}</td></tr>`).join("");
  return html`<details class="card"><summary>IA durante el diagnóstico (${batches.length} lotes)</summary>
    <div class="table-wrap"><table class="evidence"><thead><tr><th>#</th><th>Momento</th><th>Qué generó y por qué</th><th>Resultado</th></tr></thead><tbody>${rows}</tbody></table></div></details>`;
}

async function viewAdminReport(sid) {
  loading();
  const r = await api(`/teacher/reports/${sid}`);
  QSTEMS = Object.fromEntries(r.responses.map((x) => [x.question_id, x.stem]));
  const byQ = Object.fromEntries(r.responses.map((x) => [x.question_id, x]));
  const saved = r.checklist || {};
  const items = r.checklist_items;
  const current = Object.fromEntries(r.route.map((i) => [i.subtopic_id, i]));
  const answers = r.responses.map((x) => html`<tr><td>${x.seq}</td><td>${esc(x.stem)}<br><small class="muted">nivel ${x.level}${x.tailored ? " · IA para este perfil" : x.origin === "ia" ? " · IA" : ""}</small></td>
    <td>${x.kind === "mc" ? `${esc(x.answer)} <small class="muted">(clave ${esc(x.correct_key)})</small>` : `<small>${esc(x.answer)}</small>`}</td>
    <td>${x.correct ? '<span class="badge b-fortaleza">Acierto</span>' : '<span class="badge b-vacio">Error</span>'}${x.misconception ? `<br><small>${esc(x.misconception)}</small>` : ""}</td></tr>`).join("");
  const checklistHtml = items.length ? items.map((it) => html`
    <div class="check-item sev-${esc(it.severity)}">
      <label class="consent"><input type="checkbox" data-check="${esc(it.key)}" ${saved[it.key]?.checked ? "checked" : ""}>
        <span><strong>${esc(it.subtopic)}</strong> · ${esc(FLAG[it.type] || it.type)}<br><small>${esc(flagText(it.text))}</small>
          ${(() => { const x = byQ[flagQid(it)]; return x ? html`<br><small class="muted">Pregunta ${x.seq} del diagnóstico · respondió ${esc(x.kind === "mc" ? x.answer : "texto")}${x.kind === "mc" ? ` (clave ${esc(x.correct_key)})` : ""} · ${x.correct ? "acierto" : "error"}</small>` : ""; })()}</span></label>
      <input type="text" data-comment="${esc(it.key)}" value="${esc(saved[it.key]?.comment || "")}" placeholder="Comentario (opcional): p. ej. «Fuente confirmada en SUIN»">
    </div>`).join("") : '<p class="banner ok" style="margin:0">Ninguna fuente de la ruta está marcada como dudosa. Puedes aprobar y enviar directamente.</p>';
  const editors = r.original_route.map((orig) => {
    const it = current[orig.subtopic_id] || orig;
    const removed = !current[orig.subtopic_id];
    return html`<details class="card edit-item" ${items.some((x) => x.subtopic_id === orig.subtopic_id) ? "open" : ""}>
      <summary>${esc(orig.subtopic)} <span class="badge b-${esc(orig.status)}">${esc(STATUS[orig.status])}</span></summary>
      <div class="stack" style="margin-top:.6rem" data-edit="${esc(orig.subtopic_id)}">
        <div><label class="field-label">Objetivo</label><textarea name="objective" style="min-height:70px">${esc(it.objective)}</textarea></div>
        <div class="grid grid-2">
          <div><label class="field-label">Acción</label><select name="action">${["Reforzar", "Profundizar", "Repasar"].map((a) => `<option ${a === it.action ? "selected" : ""}>${a}</option>`).join("")}</select></div>
          <div><label class="field-label">Conceptos en los que debe enfocarse (separados por coma)</label><input type="text" name="focus" value="${esc((it.focus_concepts || []).join(", "))}"></div>
        </div>
        <div><label class="field-label">Fuentes (una por línea: cita | enlace)</label>
          <textarea name="sources" style="min-height:80px">${esc((it.sources || []).map((s) => `${s.citation}${s.url ? ` | ${s.url}` : ""}`).join("\n"))}</textarea></div>
        <div><label class="field-label">Nota para la persona (opcional)</label><input type="text" name="note" value="${esc(it.teacher_note || "")}"></div>
        <label class="consent"><input type="checkbox" name="removed" ${removed ? "checked" : ""}> Quitar este paso de la ruta</label>
      </div></details>`;
  }).join("");
  $app.innerHTML = html`
  <div class="stack">
    <a href="#/admin">← Volver a administración</a>
    ${resultSummary(r, { admin: true })}
    <form class="stack" id="review">
      <section class="card stack">
        <div class="row between"><h3>1. Fuentes por confirmar</h3><span class="badge b-review" id="check-count"></span></div>
        ${items.length ? html`<p class="muted">Solo las fuentes de la ruta de procedencia o vigencia dudosa. Las preguntas que respondió la persona no necesitan revisión. Los comentarios son opcionales y aparecen en el informe.</p>
        <div><button class="btn btn-small btn-ghost" type="button" id="check-all">Marcar todas como confirmadas</button></div>` : ""}
        ${checklistHtml}
      </section>
      <section class="stack"><h3>2. Ajustes a la ruta (lo que quede aquí es lo que sale en el PDF)</h3>${editors}</section>
      <section class="card stack">
        <h3>3. Decisión</h3>
        <textarea name="notes" placeholder="Observaciones generales para la persona (opcional)" style="min-height:80px">${esc(r.review.notes)}</textarea>
        <p class="error" id="review-error" role="alert"></p>
        <div class="row"><button class="btn btn-ghost" type="button" id="save">Guardar avance</button>
          <button class="btn" type="submit" id="approve">${r.email?.enabled ? "Aprobar y enviar al correo" : "Aprobar y habilitar descarga"}</button>
          <span class="muted">Estado: ${esc(REVIEW[r.review.status] || r.review.status)}</span></div>
        ${r.email?.enabled ? html`<p class="muted" style="margin:0">Correo de la persona: <strong>${esc(r.email.to)}</strong> ·
          ${r.email.status === "enviado" ? `enviado el ${fmtDate(r.email.sent_at)}` : r.email.status === "enviando" ? "enviando…"
            : r.email.status === "error" ? html`<span class="error">error al enviar: ${esc(r.email.error)}</span>` : "se envía al aprobar"}
          ${r.download_allowed ? ' · <button class="btn btn-small btn-ghost" type="button" id="resend">Reenviar correo</button>' : ""}</p>`
          : '<p class="muted" style="margin:0">El envío por correo no está configurado (SMTP_USER / SMTP_PASSWORD en el .env): la persona descargará el PDF.</p>'}
      </section>
    </form>
    <details class="card table-wrap"><summary>Respuestas del diagnóstico (${r.responses.length})</summary>
      <table class="evidence"><thead><tr><th>#</th><th>Pregunta</th><th>Respuesta</th><th>Resultado</th></tr></thead><tbody>${answers}</tbody></table></details>
    ${aiBatchesHtml(r.ai_batches || [])}
    ${traceHtml(r)}
  </div>`;

  const form = document.getElementById("review");
  const syncCount = () => {
    const done = form.querySelectorAll("[data-check]:checked").length;
    document.getElementById("check-count").textContent = items.length ? `${done} de ${items.length} confirmadas` : "Nada por confirmar";
    document.getElementById("approve").disabled = done < items.length;
  };
  form.addEventListener("change", syncCount);
  syncCount();
  const checkAll = document.getElementById("check-all");
  if (checkAll) checkAll.onclick = () => { form.querySelectorAll("[data-check]").forEach((c) => { c.checked = true; }); syncCount(); };
  const collect = (approve) => {
    const checklist = {};
    items.forEach((it) => {
      checklist[it.key] = { checked: form.querySelector(`[data-check="${CSS.escape(it.key)}"]`).checked,
                            comment: form.querySelector(`[data-comment="${CSS.escape(it.key)}"]`).value.trim() };
    });
    const edits = {};
    form.querySelectorAll("[data-edit]").forEach((box) => {
      const sources = box.querySelector("[name=sources]").value.split("\n").map((l) => l.trim()).filter(Boolean)
        .map((l) => { const [citation, url] = l.split("|").map((x) => x.trim()); return { citation, url: url || "" }; });
      edits[box.dataset.edit] = {
        objective: box.querySelector("[name=objective]").value.trim(),
        action: box.querySelector("[name=action]").value,
        focus_concepts: box.querySelector("[name=focus]").value.split(",").map((x) => x.trim()).filter(Boolean),
        sources, teacher_note: box.querySelector("[name=note]").value.trim(),
        removed: box.querySelector("[name=removed]").checked,
      };
    });
    return { approve, notes: form.notes.value, checklist, edits };
  };
  const send = async (approve) => {
    const err = document.getElementById("review-error"); err.textContent = "";
    try {
      await api(`/teacher/reports/${sid}/review`, { method: "POST", body: collect(approve) });
      toast(approve ? (r.email?.enabled ? `Informe aprobado: se está enviando a ${r.email.to}` : "Informe aprobado: la persona ya puede descargarlo") : "Avance guardado");
      if (approve) location.hash = "#/admin"; else viewAdminReport(sid);
    } catch (ex) { err.textContent = ex.message; }
  };
  document.getElementById("save").onclick = () => send(false);
  const resend = document.getElementById("resend");
  if (resend) resend.onclick = async () => {
    resend.disabled = true;
    try { await api(`/teacher/reports/${sid}/email`, { method: "POST" }); toast(`Reenviando a ${r.email.to}`); setTimeout(() => viewAdminReport(sid), 2500); }
    catch (e) { toast(e.message); resend.disabled = false; }
  };
  form.addEventListener("submit", (e) => { e.preventDefault(); send(true); });
}

// ---------- arranque ----------
meta().then((m) => {
  document.querySelectorAll("[data-app-name]").forEach((el) => { el.textContent = m.app_name; });
  document.querySelectorAll("[data-app-tagline]").forEach((el) => { el.textContent = m.tagline; });
  document.title = `${m.app_name} · ${m.tagline}`;
}).catch(() => {});
window.render = render;  // usado por botones «Reintentar»
renderNav();
if (!navigator.onLine) document.body.classList.add("is-offline");
queue.flush().finally(render);
