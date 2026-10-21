"""Clientes de IA (patrón Adapter).

* OpenAICompatibleClient: cualquier API con /chat/completions (Gemini, Groq, OpenRouter,
  Ollama, Claude vía compatibilidad). Cambiar de proveedor = cambiar variables de entorno.
* GeminiSearchClient: API nativa de Gemini con la herramienta google_search, para que la
  IA consulte fuentes reales en la web (temas libres). Devuelve también las URLs citadas.

Sin claves, la app funciona con el banco curado. Con límite gratuito agotado (HTTP 429)
reintenta con espera y, si sigue fallando, informa el error sin tumbar la aplicación.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

import httpx

from ..config import AIEndpoint, settings


class AIUnavailable(RuntimeError):
    pass


# Limita llamadas simultáneas al proveedor para respetar el límite por minuto del plan gratuito.
_semaphore = threading.BoundedSemaphore(max(1, settings.ai_max_concurrency))
log = logging.getLogger("ratio.ai")

# Se activa si el modelo rechaza el nivel de razonamiento configurado (HTTP 400).
_reasoning_unsupported = threading.Event()


def _is_thinking_error(text: str) -> bool:
    lowered = text.lower()
    return "thinking" in lowered or "reasoning" in lowered


_shared: httpx.Client | None = None
_shared_lock = threading.Lock()


def make_http_client(force_ipv4: bool) -> httpx.Client:
    # local_address="0.0.0.0" obliga a IPv4: evita esperas de ~20 s por intento en redes con IPv6 roto.
    transport = httpx.HTTPTransport(local_address="0.0.0.0") if force_ipv4 else httpx.HTTPTransport()
    return httpx.Client(transport=transport)


def _http() -> httpx.Client:
    """Cliente HTTP compartido: reutiliza conexiones (keep-alive) entre llamadas."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = make_http_client(settings.ai_force_ipv4)
        return _shared


def _http_post(url: str, *, json: dict, headers: dict, timeout: float) -> httpx.Response:
    return _http().post(url, json=json, headers=headers, timeout=timeout)


# Si se agota la cuota DIARIA, no tiene sentido seguir llamando: la IA se pausa y la app
# sigue con el banco curado y las preguntas ya guardadas.
_quota_block_until = 0.0


def quota_blocked() -> str:
    """Mensaje si la IA está pausada por cuota diaria agotada; "" si está disponible."""
    remaining = _quota_block_until - time.time()
    if remaining <= 0:
        return ""
    return f"Cuota diaria de la IA agotada: pausada {remaining / 60:.0f} min más. Se usa el banco guardado."


def describe_quota_error(response: httpx.Response) -> tuple[str, bool, float]:
    """Devuelve (mensaje legible, es_diaria, segundos_sugeridos_de_espera) para un HTTP 429."""
    try:
        data = response.json()
    except ValueError:
        return f"Límite de la IA alcanzado (HTTP 429): {response.text[:200]}", False, 0.0
    error = (data[0] if isinstance(data, list) and data else data).get("error", {}) if data else {}
    quota_id, value, delay = "", "", 0.0
    for detail in error.get("details") or []:
        for violation in detail.get("violations") or []:
            quota_id = violation.get("quotaId", quota_id)
            value = violation.get("quotaValue", value)
        if detail.get("retryDelay"):
            try:
                delay = float(str(detail["retryDelay"]).rstrip("s"))
            except ValueError:
                pass
    daily = "PerDay" in quota_id
    if daily:
        msg = (f"Límite gratuito DIARIO alcanzado ({value or '?'} solicitudes/día por modelo; {quota_id}). "
               "Se reinicia a medianoche hora del Pacífico (~2 a. m. en Colombia).")
    elif "PerMinute" in quota_id:
        kind = "tokens" if "Token" in quota_id else "solicitudes"
        msg = (f"Límite gratuito POR MINUTO alcanzado ({value or '?'} {kind}/min; {quota_id}). "
               f"Se libera en ~{delay or 60:.0f} s.")
    else:
        msg = f"Límite de la IA alcanzado (HTTP 429): {quota_id or error.get('message', '')[:160]}"
    return msg, daily, delay


def _post_with_retry(url: str, *, json_body: dict, headers: dict, timeout: float, retries: int = 2) -> httpx.Response:
    global _quota_block_until
    blocked = quota_blocked()
    if blocked:
        raise AIUnavailable(blocked)
    last_error = ""
    for attempt in range(retries + 1):
        started = time.monotonic()
        with _semaphore:
            try:
                response = _http_post(url, json=json_body, headers=headers, timeout=timeout)
            except httpx.HTTPError as exc:
                last_error = f"No se pudo contactar al proveedor de IA: {type(exc).__name__} {exc}"
                response = None
        elapsed = time.monotonic() - started
        if response is not None and response.status_code not in (429, 500, 502, 503, 504):
            log.info("intento %d: HTTP %d en %.1f s", attempt + 1, response.status_code, elapsed)
            return response
        wait = 2.0 * (attempt + 1)
        if response is not None and response.status_code == 429:
            last_error, daily, delay = describe_quota_error(response)
            if daily:
                # Reintentar solo gastaría más cupo: se pausa la IA una hora y se avisa.
                _quota_block_until = time.time() + 3600
                log.warning(last_error)
                raise AIUnavailable(last_error)
            wait = delay or 15.0
        elif response is not None:
            last_error = f"El proveedor respondió {response.status_code}: {response.text[:200]}"
            wait = 5.0 * (attempt + 1)  # 503 = modelo saturado: esperar un poco más
        wait = min(wait, 30)
        log.info("intento %d falló en %.1f s (%s)%s", attempt + 1, elapsed, last_error[:140],
                 f"; reintento en {wait:.0f} s" if attempt < retries else "")
        if attempt < retries:
            time.sleep(wait)
    raise AIUnavailable(last_error)


class OpenAICompatibleClient:
    def __init__(self, endpoint: AIEndpoint, timeout: float | None = None):
        self.endpoint = endpoint
        self.timeout = timeout or settings.ai_timeout

    @property
    def enabled(self) -> bool:
        return self.endpoint.enabled

    def chat_json(self, system: str, user: str, temperature: float = 0.3) -> dict:
        if not self.enabled:
            raise AIUnavailable("No hay proveedor de IA configurado (AI_BASE_URL, AI_API_KEY, modelo).")
        url = self.endpoint.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.endpoint.model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if settings.ai_reasoning_effort and not _reasoning_unsupported.is_set():
            body["reasoning_effort"] = settings.ai_reasoning_effort
        headers = {"Authorization": f"Bearer {self.endpoint.api_key}"}
        response = _post_with_retry(url, json_body=body, headers=headers, timeout=self.timeout)
        if response.status_code == 400 and "reasoning_effort" in body and _is_thinking_error(response.text):
            # El modelo no acepta ese nivel de razonamiento: se reintenta sin él y se recuerda.
            log.warning("El modelo no acepta AI_REASONING_EFFORT=%s; se usa su valor por defecto.",
                        settings.ai_reasoning_effort)
            _reasoning_unsupported.set()
            body.pop("reasoning_effort")
            response = _post_with_retry(url, json_body=body, headers=headers, timeout=self.timeout)
        if response.status_code >= 400:
            raise AIUnavailable(f"El proveedor de IA respondió {response.status_code}: {response.text[:300]}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise AIUnavailable(f"Respuesta inesperada del proveedor: {response.text[:300]}") from exc
        return parse_json(content or "")


def is_gemini(endpoint: AIEndpoint) -> bool:
    return "generativelanguage.googleapis.com" in endpoint.base_url


class GeminiSearchClient:
    """Gemini nativo + Google Search. Usa la misma clave y modelo que el generador."""

    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, endpoint: AIEndpoint, timeout: float | None = None):
        self.endpoint = endpoint
        self.timeout = timeout or settings.ai_timeout

    @property
    def enabled(self) -> bool:
        return self.endpoint.enabled and is_gemini(self.endpoint) and settings.ai_web_search

    def search_json(self, system: str, user: str, temperature: float = 0.2) -> tuple[dict, list[dict]]:
        """Devuelve (json_de_la_respuesta, fuentes_web_citadas[{title, uri}])."""
        if not self.enabled:
            raise AIUnavailable("La búsqueda web requiere Gemini (AI_BASE_URL de Google) y AI_WEB_SEARCH=true.")
        model = self.endpoint.model.removeprefix("models/")
        url = f"{self.BASE}/models/{model}:generateContent"
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": temperature},
        }
        level = {"none": "minimal"}.get(settings.ai_reasoning_effort, settings.ai_reasoning_effort)
        if level in ("minimal", "low", "medium", "high") and not _reasoning_unsupported.is_set():
            body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": level}
        headers = {"x-goog-api-key": self.endpoint.api_key}
        response = _post_with_retry(url, json_body=body, headers=headers, timeout=self.timeout)
        if response.status_code == 400 and "thinkingConfig" in body["generationConfig"] and _is_thinking_error(response.text):
            _reasoning_unsupported.set()
            body["generationConfig"].pop("thinkingConfig")
            response = _post_with_retry(url, json_body=body, headers=headers, timeout=self.timeout)
        if response.status_code >= 400:
            raise AIUnavailable(f"Gemini (búsqueda) respondió {response.status_code}: {response.text[:300]}")
        data = response.json()
        try:
            candidate = data["candidates"][0]
            text = "".join(p.get("text", "") for p in candidate["content"]["parts"])
        except (KeyError, IndexError) as exc:
            raise AIUnavailable(f"Respuesta inesperada de Gemini: {str(data)[:300]}") from exc
        chunks = (candidate.get("groundingMetadata") or {}).get("groundingChunks") or []
        web = [{"title": c["web"].get("title", ""), "uri": c["web"].get("uri", "")} for c in chunks if c.get("web")]
        return parse_json(text), web


def parse_json(content: str) -> dict:
    """Tolera respuestas envueltas en bloques de código o con texto alrededor."""
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIUnavailable(f"La IA no devolvió JSON válido: {exc}") from exc
