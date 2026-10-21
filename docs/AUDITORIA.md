# Ratio — Documento técnico para auditoría

> **Para el agente auditor.** Este documento describe qué hace Ratio, cómo está construido y qué decisiones se tomaron. Tu tarea es auditar la aplicación y proponer correcciones concretas para que su calidad sea muy alta. Al final hay una lista de **áreas a revisar** y el **formato de respuesta** esperado. El código fuente acompaña este documento; cuando una afirmación de aquí no coincida con el código, el código manda y la diferencia es en sí un hallazgo.

---

## 1. Contexto y propósito

- **Origen:** reto «Educación jurídica» de la Hackatón ICESI 2026-II.
- **Qué hace:** Ratio es una herramienta de **diagnóstico jurídico adaptativo**. Perfila a la persona, evalúa sus fortalezas y vacíos por subtema y la clasifica en un nivel (básico, intermedio o avanzado). Define además el enfoque que le conviene: técnico-jurídico para estudiantes y abogados, práctico para quienes no son abogados. Con eso le entrega una ruta de estudio priorizada, con objetivos, fuentes oficiales y la justificación de cada paso. Lo dudoso se marca para que lo revise un administrador.
- **Regla del organizador:** la herramienta **evalúa y orienta, no enseña**. Durante el cuestionario no hay retroalimentación (no se muestra si la respuesta fue correcta).
- **Áreas actuales:** Derecho constitucional (12 subtemas) y Derecho laboral (11 subtemas), ambas colombianas.
- **Usuarios:**
  - persona evaluada (rol `usuario`);
  - administrador académico (rol `admin`), que revisa informes dudosos, gestiona el banco de preguntas y ve los riesgos.

### Los 5 «momentos» del reto

| Momento | Qué hace Ratio | Dónde está |
|---|---|---|
| 1. Perfilar | Formulario: formación, semestre o profesión, área, temas, objetivo, horas y semanas, consentimiento | `engine/profiling.py`, `viewProfile` en `app.js` |
| 2. Diagnosticar | Preguntas adaptativas (escalera por subtema con exploración de prerrequisitos); la IA genera preguntas a la medida durante la prueba | `engine/adaptive.py`, `services/diagnostic.py`, `services/live_ai.py` |
| 3. Clasificar | Dominio ponderado, nivel alcanzado, estado y confianza por subtema; nivel global | `engine/classification.py` |
| 4. Orientar | Ruta priorizada con objetivo, acción, horas, semanas y fuentes | `engine/routing.py` |
| 5. Justificar | Evidencia de cada paso, traza de decisiones del motor y marcas de revisión | `engine/routing.py` (`_justification`, `_flags`) |

---

## 2. Arquitectura

```
Navegador (SPA en JS puro, sin compilación)
   │  fetch /api/*  (token Bearer firmado con HMAC)
   ▼
FastAPI (backend/app)
   ├─ api/routes.py        → capa HTTP delgada: valida, autentica y traduce errores
   ├─ services/*           → casos de uso (diagnóstico, usuarios, administrador, IA en vivo, correo, PDF)
   ├─ engine/*             → lógica pura y probada aparte (sin base de datos ni IA)
   ├─ ai/*                 → clientes LLM (compatibles con OpenAI: Groq/Gemini), prompts, validación y verificación
   ├─ models.py / db.py    → SQLAlchemy 2 (SQLite local o PostgreSQL/Supabase)
   └─ seed.py              → carga idempotente del banco curado (data/seed/*.json)
```

- **Patrones usados:**
  - Settings centralizado (`config.py`, inmutable).
  - Strategy para calificar (`KeyGrader`, `RubricKeywordGrader`, `AIRubricGrader`).
  - Capa de servicios.
  - Motor como funciones puras sobre un `state` serializable en JSON.
  - Cola de reintentos en el cliente.
  - Respuestas idempotentes.
- **Frontend:** un solo módulo `frontend/app.js` (~1.100 líneas) con router por hash. Las vistas son funciones que pintan `innerHTML` mediante plantillas; el escape se hace con `esc()`. Estilos en `styles.css`, con modo oscuro por `prefers-color-scheme`.
- **Persistencia:** SQLite (`ratio.db`) en local y PostgreSQL en producción (Supabase, session pooler, puerto 5432). No hay Alembic: `init_db()` hace `create_all` y una migración ligera que agrega columnas nuevas con `ALTER TABLE ADD COLUMN`. Un listener `before_flush` recorta cadenas que superan el largo de su `VARCHAR`, porque PostgreSQL lo exige y SQLite no.
- **Despliegue:**
  - Render, con Docker (`Dockerfile`, `render.yaml`), que es el que se usa.
  - Vercel como alternativa (`pyproject.toml`, `vercel.json`, `requirements.txt` en la raíz, modo `serverless`).
  - Local con `run.bat` / `run.sh`.

### Estructura de archivos

```
backend/app/
  main.py            lifespan: init_db → load_all (semilla) → ensure_admin; monta frontend/ como estático en "/"
  config.py          variables de entorno (ver §9)
  db.py              engine, SessionLocal, init_db (migración ligera), recorte de cadenas
  models.py          Area, Subtopic, Source, Question, User, DiagnosticSession, Response, Report, GenerationJob, AIBatch
  schemas.py         Pydantic: ProfileIn, AnswerIn, RegisterIn, LoginIn, GoogleIn, ReviewIn, GenerateIn, DecisionIn
  auth.py            PBKDF2-SHA256 para contraseñas; token propio firmado con HMAC-SHA256 (SECRET_KEY o archivo .secret_key)
  seed.py            validate_seed (reglas de calidad del banco) + carga idempotente
  migrar.py          copia datos entre bases (SQLite → Supabase) sin duplicar
  api/routes.py      endpoints (ver §6)
  engine/            adaptive, classification, grading, profiling, routing
  ai/                client (HTTP, reintentos, cuota), prompts, service (generar/verificar/calificar), pregenerar (CLI), exportar (CSV), check (diagnóstico de claves)
  services/          diagnostic, users, teacher, live_ai, preparation, mailer, report_pdf
  fonts/             DejaVu (PDF con tildes en cualquier servidor)
backend/tests/       pytest: 42 pruebas (motor, API, IA simulada, correo, Google, modo serverless). Corren en SQLite y en PostgreSQL (TEST_DATABASE_URL)
frontend/            index.html, app.js, styles.css, img/ (logo)
data/seed/           constitucional.json, laboral.json (subtemas, fuentes con fragmento, preguntas curadas)
```

---

## 3. Flujo de la persona evaluada

1. **Portada** (`#/`): explica qué es Ratio y lleva a crear la cuenta o a ingresar.
2. **Cuenta** (`#/ingresar`):
   - **Registro:** nombre, correo único (no distingue mayúsculas) y contraseña de mínimo 6 caracteres.
   - **Ingreso:** correo y contraseña.
   - **Google:** «Continuar con Google» (Google Identity Services). El servidor valida el ID token contra `oauth2.googleapis.com/tokeninfo` y revisa `aud`, `iss`, `email_verified` y `exp`. El botón solo se muestra en `https` o en `localhost`.
3. **Perfil** (`#/perfil`): nada viene preseleccionado y todo es obligatorio, salvo la profesión. Se elige área y temas (al menos uno); **solo se evalúan los temas elegidos**. Requiere consentimiento según la Ley 1581 de 2012.
4. **Instrucciones** (`#/instrucciones/:id`): número de preguntas estimado, tiempo, temas y enfoque, junto con las reglas: sin retroalimentación, preguntas adaptativas, tipos de pregunta, honestidad y progreso guardado.
5. **Diagnóstico** (`#/diagnostico/:id`):
   - Una pregunta a la vez, de opción múltiple (opciones barajadas por sesión) o abierta (texto de al menos 15 caracteres).
   - No hay retroalimentación.
   - La pregunta puede estar marcada «✦ generada por IA a la medida».
   - Si el motor espera a la IA, aparece «Un momento…» y la vista consulta de nuevo cada 1,5 s.
6. **Sin conexión:**
   - Cada respuesta se guarda primero en `localStorage` (`ratio_queue`, con el `uid` del usuario) y luego se envía.
   - Si falla la red se muestra «Tu progreso está a salvo» y se reenvía al reconectar.
   - El servidor es idempotente: reenviar no duplica la respuesta.
   - En un computador compartido solo se envían las respuestas del usuario actual.
7. **Resultado** (`#/resultado/:id`):
   - Nivel global, enfoque, dominio y barras por subtema.
   - Ruta de aprendizaje con «¿Por qué está en tu ruta?» y las preguntas respondidas.
   - Si hay marcas de riesgo medio o alto, el informe queda **en verificación**. El botón del PDF muestra entonces un aviso: no se puede descargar y llegará por correo cuando lo aprueben.
8. **Correo:** cuando el informe queda verificado (sin marcas, o aprobado por el administrador), se envía en HTML con el **PDF adjunto** (fpdf2) y el logo incrustado. La persona puede reenviarlo (máximo uno cada 2 minutos). Si no hay SMTP configurado, se ofrece una vista imprimible (`#/informe/:id` → `window.print()`).
9. **Historial** (`#/historial`): todos sus diagnósticos, con botones para continuar, ver resultado o recibir el PDF.

## 4. Flujo del administrador

- `#/admin` tiene tres pestañas:
  - **Informes:** filtro por estado de revisión.
  - **Banco de preguntas:** curadas y generadas por IA, con aprobación, rechazo y generación manual.
  - **Riesgos:** subtemas y fuentes con riesgo normativo.
- `#/admin/informe/:id` tiene tres partes:
  1. **Lista de verificación:** una casilla por cada marca de severidad media o alta, con comentario opcional. **Hay que marcar todo para aprobar.** Las preguntas se muestran por su enunciado, junto con la respuesta dada, la clave y si acertó.
  2. **Ajustes a la ruta:** objetivo, acción, conceptos, fuentes, nota y opción de quitar el paso. La ruta original queda guardada (`original_route`) y lo editado es lo que ve la persona y lo que sale en el PDF.
  3. **Decisión:** observaciones generales y «Aprobar y enviar al correo».
- La misma pantalla muestra el estado del correo (enviado, error o reenviar), las respuestas, los lotes de IA de la sesión y la traza del motor.
- **Estados de revisión:** `no_requiere`, `pendiente`, `en_revision`, `aprobada` y `ajustada` (aprobada con cambios reales). La descarga y el correo solo se habilitan en `no_requiere`, `aprobada` y `ajustada`.
- El administrador inicial se crea desde `ADMIN_EMAIL` y `ADMIN_PASSWORD`. **Los valores por defecto son `admin@ratio.local` / `admin-demo`.**

---

## 5. Motor de diagnóstico (lógica pura)

### 5.1 Perfil → plan (`profiling.build_plan`)

**Enfoque:**
- Es `practico` si la persona no es abogada o si su objetivo es `conocer_derechos`.
- En los demás casos es `tecnico`.

**Nivel inicial y techo:**

| Perfil | Nivel inicial | Techo |
|---|---|---|
| Abogado | 2 | 3 |
| Estudiante de 6.º semestre o más | 2 | 3 |
| Estudiante de primeros semestres | 1 | 3 |
| No abogado | 1 | 2 |

Si el objetivo es `ejercicio`, el techo sube a 3.

### 5.2 Presupuesto de preguntas

- Total: `min(45, 15 + 5·(n_temas − 1))`. Es decir, 1 tema da 15 preguntas, 2 temas dan 20, y así sucesivamente hasta 45.
- Por tema: `ceil(total / n)`.

### 5.3 Selección adaptativa (`adaptive.next_question`)

**Escalera por subtema:**
- Si acierta, sube un nivel. Acertar en el techo cierra el subtema.
- Si falla, baja un nivel.
- Si falla en el nivel básico, abre o prioriza el subtema prerrequisito para encontrar la raíz del vacío. Queda registrado en `probed_by`.

**Orden de los subtemas:**
- Se recorren en orden topológico de prerrequisitos y de forma intercalada.
- Con más de 4 preguntas por subtema entra en «modo exploración» y no cierra temprano.

**Ranking de preguntas candidatas**, por estos criterios en orden:
1. Distancia al nivel objetivo.
2. Preferencia por la pregunta abierta cuando corresponde.
3. Ajuste al perfil: 0 = generada para esta sesión, 1 = mismo perfil, 2 = curada, 3 = otro perfil.
4. Enfoque.
5. Exposición (veces mostrada).
6. Un azar estable por sesión: `jitter(seed, key)` con SHA-256, que no cambia al recargar.

**Espera a la IA:** si un subtema se queda sin preguntas y la IA puede generar más (`refillable`), el motor devuelve `WAIT` y se dispara un lote urgente.

**Trazabilidad:** el estado completo vive en `DiagnosticSession.state` (JSON). Cada decisión deja una línea en `trace`.

### 5.4 Calificación (`grading.py`)

- **Opción múltiple:** `KeyGrader`. Cada distractor declara el error conceptual que diagnostica (`misconception`).
- **Abierta sin IA:** `RubricKeywordGrader`. Normaliza el texto (sin tildes) y busca criterios de la rúbrica. Aprueba con un umbral de 0,6.
- **Abierta con IA:** `AIRubricGrader`. Devuelve el mismo `GradeResult`. Una respuesta calificada por palabras clave genera la marca `calificacion_automatica`.

### 5.5 Clasificación (`classification.py`)

**Por subtema:**
- **Dominio** = Σ(puntaje·nivel) / Σ(nivel).
- **Nivel alcanzado** = el nivel más alto acertado. Baja uno si hubo un error en un nivel inferior.
- **Estado:**
  - fortaleza si el dominio es ≥ 0,75 con al menos 2 respuestas;
  - vacío si es < 0,45;
  - en desarrollo en el resto de casos.
- **Confianza:** alta con 3 o más respuestas, media con 2, baja con 1.

**Global:** se calcula con el nivel y el dominio promedio, usando los umbrales 2,4/0,7, 1,5 y 0,75.

### 5.6 Ruta (`routing.py`)

**Prioridad:**
- Base según el estado: vacío 3, en desarrollo 2, fortaleza 0,5.
- +1 si el subtema bloquea a otros débiles (es prerrequisito).
- +0,5 si está entre los intereses.
- Etiqueta: «alta» desde 3, «media» desde 1,5 y «baja» por debajo.

**Orden:** primero lo débil que es raíz, luego el resto de lo débil, y al final las fortalezas.

**Horas:** vacío 4 (×1,5 en enfoque técnico), en desarrollo 2 y fortaleza 1. Se reparten en las semanas disponibles; lo que excede el tiempo queda marcado como opcional (`within_time`).

**Fuentes:** se filtran por `min_level`. En enfoque práctico se priorizan normas y guías sobre jurisprudencia.

**Marcas de revisión (`_flags`):**
- subtema en transición normativa;
- fuente con riesgo;
- fuente sin verificar (severidad baja);
- pregunta generada por IA: una por pregunta, citada por su enunciado;
- pregunta curada con riesgo;
- calificación automática;
- evidencia insuficiente (confianza baja).

---

## 6. API

Todas las rutas van bajo `/api`. Los errores salen como `{detail}`, con códigos 401, 403, 404, 409, 422, 429, 502 y 503.

| Método y ruta | Autenticación | Uso |
|---|---|---|
| POST `/auth/register` | — | Crear cuenta. Devuelve `{token, user}` |
| POST `/auth/login` | — | Ingresar |
| POST `/auth/google` | — | Ingresar con el ID token de Google |
| GET `/auth/me` | usuario | Usuario actual |
| GET `/me/sessions` | usuario | Historial |
| GET `/health`, `/meta`, `/areas` | — | Salud del servicio; metadatos (IA, `google_client_id`, `mail_enabled`); áreas y subtemas |
| POST `/sessions` | usuario | Iniciar el diagnóstico con el perfil |
| GET `/sessions/{id}/preparation` | dueño | Estado de la preparación con IA (tema libre) |
| GET `/sessions/{id}/next` | dueño | Siguiente pregunta, `waiting` o `finished`, más el estado de la IA (`ai.pending` y `ai.active`) |
| POST `/sessions/{id}/answers` | dueño | Responder. Idempotente por (sesión, pregunta) |
| POST `/sessions/{id}/finish` | dueño | Clasificar, armar la ruta y crear el informe |
| GET `/sessions/{id}/report` | dueño | Informe (incluye `download_allowed` y `email`) |
| POST `/sessions/{id}/ai/run` | dueño | Modo serverless: ejecuta los lotes de IA pendientes |
| POST `/sessions/{id}/email` | dueño | Reenviar el PDF al correo |
| GET `/teacher/reports[/{id}]` | admin | Lista y detalle (`checklist_items`, `responses`, `ai_batches`) |
| POST `/teacher/reports/{id}/review` | admin | `{approve, notes, checklist{key:{checked,comment}}, edits{subtopic_id:{...}}}` |
| POST `/teacher/reports/{id}/email` | admin | Reenviar el correo |
| GET `/teacher/questions`, POST `/teacher/questions/generate`, POST `/teacher/questions/{id}/decision` | admin | Gestión del banco |
| GET `/teacher/risks` | admin | Riesgos normativos |

**Control de acceso:** `_session(db, sid, user)` exige ser dueño o administrador. Si no lo es, responde 403: «Este diagnóstico pertenece a otro usuario».

---

## 7. Inteligencia artificial

- **Proveedor:** cualquier API compatible con OpenAI (`AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL_GENERATOR`); hoy es **Groq**. Puede haber un verificador separado (`AI_VERIFIER_*`). Gemini tiene un cliente nativo con búsqueda (`AI_WEB_SEARCH`), hoy apagado.
- **Cliente (`ai/client.py`):**
  - `httpx` compartido;
  - semáforo de concurrencia (`AI_MAX_CONCURRENCY`);
  - reintentos con espera;
  - detección de cuota diaria agotada: pausa la IA y no reintenta (`quota_blocked`);
  - respaldo si el modelo no acepta el nivel de razonamiento;
  - opción de forzar IPv4.
- **Generación durante el diagnóstico (`services/live_ai.py`):**
  - Cada `AI_BATCH_EVERY` respuestas (y en urgencia si falta material) crea un `AIBatch` con objetivos. Primero van los subtemas donde falló, enviando sus errores como foco; luego los no evaluados; luego los dominados, al nivel siguiente.
  - Genera `AI_BATCH_SIZE` preguntas con el perfil en el prompt, basadas **solo** en fragmentos de fuentes de una lista cerrada.
  - Las valida en código:
    - 4 opciones A–D;
    - una clave válida;
    - un error conceptual por cada distractor;
    - fuente dentro de la lista;
    - sin repetir enunciados;
    - **todo artículo citado debe decir de qué norma es**.
  - Normaliza formatos inesperados del modelo: opciones como texto o diccionario, preguntas que no son objetos.
  - Un **verificador** independiente aprueba o rechaza cada pregunta.
  - Las aprobadas quedan `verificada` y ligadas a la sesión (`session_id`, `profile_tag`); las rechazadas se guardan como `rechazada`.
  - Un lote «en curso» por más de 5 minutos se da por perdido.
- **Modos de ejecución:**
  - **Servidor normal (Render/local):** el lote corre en un hilo en segundo plano.
  - **Serverless (Vercel):** el lote queda `pendiente` y el navegador llama a `POST /ai/run` sin esperar la respuesta. El paso de `pendiente` a `en_curso` es atómico.
- **Banco curado:** `data/seed/*.json`, validado por `seed.validate_seed`: fuente obligatoria, errores por distractor, prerrequisitos válidos y artículos con norma. `pregenerar` llena el banco con IA por adelantado. Mientras la base es SQLite, `exportar` mantiene `preguntas_exportadas.csv`.
- **Tema libre** (`services/preparation.py`): la IA arma un mapa de subtemas y fuentes y genera preguntas. Hoy está desactivado en serverless y poco usado.

---

## 8. Correo, PDF y Google

- **Correo (`services/mailer.py`):**
  - SMTP con STARTTLS (Gmail, contraseña de aplicación), enviado en un hilo (o síncrono en serverless).
  - El estado queda en `Report.email_status/email_sent_at/email_error`.
  - Se envía al terminar sin marcas o al aprobar. El usuario puede reenviarlo con un límite de 2 minutos; el administrador, sin límite.
  - Si falta `fpdf2`, el servidor arranca igual y el correo queda desactivado.
- **PDF (`services/report_pdf.py`):** fpdf2 con DejaVu incluida. Contiene encabezado con logo, resultado, temas, ruta (con «Por qué»), preguntas respondidas y el cierre de revisión. Las horas se muestran en hora de Colombia (`zoneinfo`).
- **Google:** ver §3. `GOOGLE_CLIENT_ID` es público. El origen de JavaScript debe estar autorizado en Google Cloud.

---

## 9. Configuración (variables de entorno)

| Grupo | Variables |
|---|---|
| App | `APP_NAME`, `APP_TAGLINE`, `APP_URL` (enlace del correo) |
| Base de datos | `DATABASE_URL` (vacía = SQLite; acepta `postgresql://`, que se convierte a `postgresql+psycopg://`) |
| Seguridad | `SECRET_KEY` (obligatoria en la nube), `TOKEN_DAYS` (7) |
| Administrador | `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME` |
| Correo | `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM_NAME` |
| Google | `GOOGLE_CLIENT_ID` |
| IA | `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL_GENERATOR`, `AI_VERIFIER_*`, `AI_REASONING_EFFORT`, `AI_LIVE_GENERATION`, `AI_BATCH_SIZE` (3), `AI_BATCH_EVERY` (3), `AI_BATCH_TARGETS` (1), `AI_MAX_BATCHES` (4), `AI_MAX_CONCURRENCY`, `AI_TIMEOUT`, `AI_FORCE_IPV4`, `AI_WEB_SEARCH`, `AI_FREE_TOPIC` |
| Despliegue | `SERVERLESS` (automático si existe `VERCEL`), `PORT` |
| Pruebas | `TEST_DATABASE_URL` (correr pytest contra PostgreSQL) |

---

## 10. Pruebas

- **Comando:** `cd backend && python -m pytest -q`. Son 42 pruebas y tardan unos 10 s. También pasan contra PostgreSQL 16.
- **Cubren:**
  - el motor (escalera, prerrequisitos, presupuesto, orden aleatorio estable);
  - la API completa del diagnóstico;
  - la cola sin conexión y la idempotencia;
  - el control de acceso;
  - la lista de verificación y su bloqueo de la aprobación;
  - las ediciones del administrador reflejadas en el informe;
  - la IA simulada (lotes, foco en errores, verificador, recarga urgente, modo serverless);
  - las salidas mal formadas del modelo;
  - el correo con PDF adjunto y su límite de reenvío;
  - el PDF con caracteres especiales;
  - el login con Google (token válido, `aud` incorrecto y correo no verificado);
  - las reglas de calidad del banco.
- **No hay:**
  - pruebas automáticas del frontend (se verificó a mano con Playwright: portada, registro, perfil, instrucciones, diagnóstico, sin conexión, administrador, PDF; modo claro y oscuro; 1280 px y 390 px);
  - pruebas de carga;
  - análisis estático configurado (ruff, mypy, eslint);
  - CI.

---

## 11. Decisiones conocidas y deudas técnicas (para que no las reportes como descubrimiento, sino que las evalúes y priorices)

1. **Token propio con HMAC** guardado en `localStorage`; no usa JWT estándar ni cookies httpOnly. No hay revocación ni rotación.
2. **Sin límite de intentos** en login o registro, sin verificación del correo al registrarse y **sin recuperación de contraseña**.
3. **Contraseña mínima de 6 caracteres**, sin más reglas.
4. **CORS `allow_origins=["*"]`.** No hay cabeceras de seguridad (CSP, HSTS, X-Frame-Options).
5. **Frontend con `innerHTML`:** depende de que todas las interpolaciones pasen por `esc()`. Hay que revisar los puntos donde no pasen.
6. **Prompt injection:** `profesion` (texto libre) y los errores conceptuales se incluyen en el prompt del generador. Las respuestas abiertas se mandan a la IA cuando la califica.
7. **Migraciones ligeras** sin Alembic. No hay índices revisados para PostgreSQL. `_trigger_lock` es por proceso: con varias instancias depende de la condición en base de datos.
8. **Hilos en segundo plano** en el servidor: si el proceso se reinicia a mitad de un lote, el lote se pierde. Se considera perdido a los 5 minutos.
9. **Correo por SMTP:** algunos hosts gratuitos (Render Free) bloquean los puertos 25, 465 y 587. No hay cola de reintentos de correo.
10. **`app.js` monolítico** (~1.100 líneas). No hay gestión de estado ni componentes, ni i18n (todo en español).
11. **Sin observabilidad:** solo logs de uvicorn. No hay métricas ni trazas, ni Sentry.
12. **Datos personales** (nombre, correo, respuestas) en Supabase. Faltan política de retención, exportación y borrado de cuenta (Ley 1581).
13. **Calidad jurídica:** el banco curado lo escribió el equipo con apoyo de IA. El verificador también es un LLM. Las fuentes tienen `verified_on`, pero no hay revisión periódica.
14. **Accesibilidad:** se usaron roles ARIA básicos, foco y contraste. No se ha auditado formalmente con WCAG 2.2.
15. **El CSV de preguntas** solo se actualiza con SQLite. Con Supabase no hay exportación desde la interfaz.

---

## 12. Qué debe auditar el agente

Revisa estas áreas. En cada una, busca defectos reales en el código y propón la corrección.

1. **Seguridad:** autenticación y tokens, control de acceso (IDOR en `/sessions/{id}/*` y `/teacher/*`), XSS en el frontend, inyección (SQL, prompts), CORS y cabeceras, secretos, límites de tasa, validación de entradas, dependencias vulnerables, verificación de Google y SSRF (URLs de fuentes).
2. **Corrección del motor:** casos límite de la escalera (techo y piso, subtema sin preguntas, prerrequisitos en ciclo), presupuesto, clasificación (umbrales con poca evidencia), ruta (horas, semanas, `within_time`) y coherencia de las marcas de revisión.
3. **Concurrencia y consistencia:** respuestas duplicadas, carreras al disparar lotes de IA, sesiones abiertas en dos pestañas, la cola sin conexión con varios usuarios y transacciones en PostgreSQL.
4. **IA:** robustez ante salidas inválidas, costos y cuota (tokens por diagnóstico), latencia percibida, calidad y sesgo de las preguntas, independencia real del verificador y trazabilidad.
5. **Calidad jurídica y pedagógica:** que las preguntas evalúen sin enseñar, que los distractores diagnostiquen errores reales, que las fuentes estén vigentes, y la claridad para no abogados.
6. **Rendimiento:** consultas N+1 (informe, historial, banco), arranque en frío (semilla en cada arranque), tamaño de las respuestas y conexiones a Supabase.
7. **UX y accesibilidad:** flujo completo en celular, estados de carga y error, mensajes, teclado y lector de pantalla, contraste en modo oscuro, y textos legales y de consentimiento.
8. **Mantenibilidad:** estructura, duplicación, tipado, nombres, tamaño de `app.js`, pruebas faltantes (frontend, e2e) y lo que debería ir a CI (ruff, mypy, pytest, eslint).
9. **Operación y despliegue:** Docker, variables, migraciones, copias de seguridad, monitoreo y qué pasa si falla Groq, Supabase o el SMTP.
10. **Privacidad y cumplimiento:** Ley 1581 de 2012 (consentimiento, finalidad, derechos ARCO, retención) y los datos que se envían a terceros (Groq, Google, Gmail).

## 13. Formato de respuesta esperado

Entrega una tabla de hallazgos **ordenada por severidad** (Crítica, Alta, Media o Baja). Cada hallazgo debe incluir:

- **ID** y **título** corto;
- **Severidad** y **categoría** (de §12);
- **Ubicación:** archivo y función o línea;
- **Problema:** qué pasa y en qué escenario concreto (entrada → resultado incorrecto);
- **Corrección propuesta:** cambio concreto, con un fragmento de código si aplica;
- **Esfuerzo estimado** (S, M o L) y si **rompe compatibilidad**;
- **Cómo verificarlo:** la prueba que debería agregarse.

Al final, agrega:

- **Top 5** de cambios con mayor impacto por esfuerzo;
- lo que **no** cambiarías, y por qué;
- las preguntas abiertas para el equipo.

**No reportes como hallazgo algo que ya está resuelto en el código.** Cita el código que demuestra cada problema.


---

## Anexo — Cambios aplicados tras la primera auditoría (20/10/2026)

| ID | Cambio |
|---|---|
| A-01 | La contraseña `admin-demo` no sirve en producción (403). Si `ADMIN_PASSWORD` trae otra, el arranque la aplica sobre la de demostración. |
| A-02 | Correo por API HTTPS (`MAIL_PROVIDER=brevo` o `resend`) para hosts que bloquean SMTP. Con el informe verificado siempre está «Ver / imprimir» como respaldo. |
| A-03 | Límites de longitud en `area_id`, `intereses` y `question_id`, y formato de correo en el login. Límite de intentos por IP y correo (login 8 cada 15 min, registro 5 por hora, Google, cambio de clave, eliminación). |
| A-04 | Cabeceras: CSP, `nosniff`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, COOP, HSTS en https y `no-store` en la API. Se quitó el JavaScript en línea. Los tokens llevan versión: al cambiar la contraseña se cierran las demás sesiones. |
| A-05 | Página `#/privacidad` (Ley 1581): encargados en el exterior, derechos y plazos. «Descargar mis datos» (JSON) y «Eliminar mi cuenta». Enlace en el consentimiento y en el pie de página. |
| A-07 | `data/review/correcciones.json` retira 4 preguntas de IA con errores jurídicos y devuelve 2 a revisión. Se aplica al arrancar, una sola vez por pregunta. El generador rechaza fechas imposibles; el generador y el verificador tienen reglas nuevas (plazos hábiles, categorías vecinas, una sola respuesta correcta). |
| M-01 | `/docs`, `/redoc` y `/openapi.json` desactivados en producción. |
| M-02 | «Fortaleza» exige 3 respuestas; confianza alta, 4. El nivel global exige un dominio mínimo (Intermedio ≥ 50 %, Básico ≥ 30 %). |
| M-03 | El motor evita repetir un concepto ya preguntado, y el generador descarta enunciados casi idénticos (similitud ≥ 0,82). |
| M-05 | Verificado: `/next` no expone la clave ni los errores (`_public_question`). |
| M-06 | `/api/meta` ya no publica `max_questions` (no se usaba). El presupuesto real es `min(45, 15 + 5·(n − 1))`. |
| B-01 | CORS solo para `CORS_ORIGINS` explícitos (la app es del mismo origen). |
| B-02 | Enlace «Saltar al contenido», `h1` en la página de ingreso y cambio de contraseña. Sigue pendiente la recuperación de contraseña por correo. |
| B-03 | GitHub Actions: ruff, pytest (SQLite y PostgreSQL) y `node --check`. Son 52 pruebas. |

**Pendiente o fuera del código:**

- Revisión jurídica humana y firmada del banco (A-07).
- Mantener el servicio despierto con un ping externo, o usar un plan de pago (A-06).
- Revisión manual de las preguntas abiertas (M-04).
- Calificador con IA endurecido.
- Recuperación de contraseña.
- Migrar las plantillas a escape por defecto.

**Estado del despliegue para la segunda auditoría** (`https://ratio-9m0a.onrender.com`, 20/10/2026, 23:50):

- **Ya visible en producción:** `/docs` y `/openapi.json` dan 404. `/api/meta` ya no publica `max_questions` y sí `contact_email`. El botón de Google se carga bien con la CSP activa.
- **Pendiente de configuración:**
  - La IA sigue apagada en la web: faltan `AI_BASE_URL`, `AI_API_KEY` y `AI_MODEL_GENERATOR` en Render.
  - El correo sigue por SMTP: falta configurar Brevo.
  - No hay ping externo para evitar el arranque en frío.
  - El flujo de CI está en `docs/ci.yml` y falta moverlo a `.github/workflows/`.
- **Se pide al auditor:** volver a probar los hallazgos A-01 a B-03 contra el código del repositorio y el sitio desplegado.

### Cómo hacer la prueba con sesión iniciada

No crees cuentas en el sitio de producción. **Pide a la persona del equipo que inicie sesión ella misma** en su navegador, en `https://ratio-9m0a.onrender.com` (con su cuenta de usuario normal, con Google o con correo). Cuando te confirme "listo", continúa la revisión en esa pestaña y con esa sesión. Si necesitas el panel de administrador, pídele que cierre sesión y entre con la cuenta de administrador; nunca escribas tú contraseñas.

**Con la sesión de usuario, revisa:**

1. Un diagnóstico completo (perfil → instrucciones → preguntas → resultado). Usa el perfil "No abogado" para que se reconozca como prueba. Confirma:
   - que aparezcan preguntas "✦ generadas por IA a la medida", si la IA ya está encendida;
   - en DevTools → Network, que `/api/sessions/{id}/next` no incluya la clave ni el error asociado (M-05).
2. En el resultado:
   - el aviso del PDF cuando el informe está "en verificación";
   - "Ver / imprimir" cuando ya está aprobado.
3. En "Mi historial → Mi cuenta y mis datos":
   - que el formulario de cambio de contraseña y "Eliminar mi cuenta" se vean y validen. **No los ejecutes**: no cambies la contraseña de la persona ni elimines su cuenta;
   - "Descargar mis datos (JSON)" sí se puede ejecutar: solo descarga un archivo.
4. La página `#/privacidad` y el enlace en el consentimiento.
5. La consola del navegador: que no haya errores de JavaScript ni violaciones de la CSP.
6. IDOR: con la sesión de usuario, que pedir `/api/sessions/{id}/report` de un diagnóstico que no es suyo (pide un id al equipo) dé 403, y que `/api/teacher/reports` dé 403.

**Con la sesión de administrador (si te la dan):**

- La lista de verificación de un informe "en revisión": preguntas citadas por su enunciado, sin códigos, y aprobación bloqueada hasta marcar todo.
- El estado del correo después de aprobar.

**Lo que puedes probar sin sesión:**

- Cabeceras de seguridad.
- `/docs` y `/openapi.json` con 404.
- El límite de intentos de ingreso: con un correo inventado, el 9.º intento en 15 minutos debe dar 429. Usa un correo que no sea del equipo, para no bloquear a nadie.

**No hagas:** crear cuentas, cambiar contraseñas, eliminar cuentas, aprobar informes reales ni enviar correos sin que el equipo lo autorice en el momento.


### Segunda ronda (21/10/2026, 00:10)

| ID | Corrección |
|---|---|
| N-01 | «Saltar al contenido» mueve el foco a `<main>` sin cambiar el hash (`preventDefault`), así que el router no cambia de vista. |
| N-02 | HSTS se envía en todas las respuestas cuando `PRODUCTION` está activo (Render), también en `/` y en los archivos estáticos. Hay una prueba sobre `/`. |
| N-03 | El registro solo acepta correos con caracteres `[A-Za-z0-9._%+-]` (rechaza `<`, `>`, comillas y espacios) y hay una prueba de 422. El id del botón va con `encodeURIComponent`. Las líneas ~1180 y ~1188 usan `toast()`, que asigna `textContent`, no HTML, así que no son inyectables. El correo del panel ya pasa por `esc()`. |
| N-04 | El consentimiento incluye la declaración de mayoría de edad o la autorización del representante. La política tiene una sección «Menores de edad» (art. 7). |

El repositorio va adjunto en un zip (`ratio-repo.zip`), sin `.env` ni base de datos.
