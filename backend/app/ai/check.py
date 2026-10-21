"""Prueba real de la conexión con la IA. Uso (desde backend/):

    python -m app.ai.check            # prueba completa
    python -m app.ai.check --red      # solo el diagnóstico de red (rápido)

No modifica la base de datos: llama al proveedor, mide tiempos y muestra lo que devuelve.
"""
from __future__ import annotations

import json
import logging
import sys
import time

from ..config import settings
from . import service as ai
from .client import AIUnavailable, make_http_client


def _timed(label: str, fn):
    print(f"\n▶ {label}…", flush=True)
    start = time.monotonic()
    try:
        result = fn()
        print(f"  ✔ OK en {time.monotonic() - start:.1f} s")
        return result
    except AIUnavailable as exc:
        print(f"  ✘ Falló en {time.monotonic() - start:.1f} s: {exc}")
        return None


def _network_check() -> None:
    """Mide cuánto tarda solo la red (sin IA): conexión normal vs. forzando IPv4."""
    if "generativelanguage.googleapis.com" not in settings.generator.base_url:
        return
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    headers = {"x-goog-api-key": settings.generator.api_key}
    print("\n▶ 0/4 Diagnóstico de red (sin IA, solo conexión con Google)…", flush=True)
    results = {}
    for label, ipv4 in (("normal", False), ("solo IPv4", True)):
        times = []
        with make_http_client(ipv4) as client:
            for _ in range(2):  # 1.ª = conexión nueva; 2.ª = conexión reutilizada
                start = time.monotonic()
                try:
                    client.get(url, headers=headers, params={"pageSize": 1}, timeout=60)
                    times.append(time.monotonic() - start)
                except Exception as exc:  # noqa: BLE001
                    times.append(None)
                    print(f"  {label}: error {type(exc).__name__}: {exc}")
        results[label] = times
        fmt = " / ".join("—" if t is None else f"{t:.1f} s" for t in times)
        print(f"  Conexión {label}: {fmt}  (nueva / reutilizada)")
    normal, ipv4 = results["normal"][0], results["solo IPv4"][0]
    if normal and ipv4 and normal > 5 and normal > 3 * ipv4:
        print("  ⚠ La red tarda mucho por IPv6. Agrega AI_FORCE_IPV4=true al archivo .env")
    elif normal and normal > 5:
        print("  ⚠ La conexión con Google es lenta en esta red (no es la IA).")
    else:
        print("  La red está bien: si las llamadas tardan, es el tiempo de respuesta del modelo.")


def _list_models() -> None:
    """Si el modelo no existe, muestra los modelos Flash disponibles para esta clave."""
    if "generativelanguage.googleapis.com" not in settings.generator.base_url:
        return
    try:
        with make_http_client(settings.ai_force_ipv4) as client:
            res = client.get("https://generativelanguage.googleapis.com/v1beta/models",
                             headers={"x-goog-api-key": settings.generator.api_key},
                             params={"pageSize": 200}, timeout=30)
        names = [m["name"].removeprefix("models/") for m in res.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in m["name"]]
    except Exception as exc:  # noqa: BLE001
        print(f"  (No se pudo listar modelos: {exc})")
        return
    if names:
        print("\n  Modelos Flash disponibles para tu clave (usa uno en AI_MODEL_GENERATOR):")
        for name in names:
            print(f"   - {name}")


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # consolas de Windows sin UTF-8
    except AttributeError:
        pass
    # Muestra cada intento HTTP (código y tiempo) para ver si hay reintentos.
    logging.basicConfig(level=logging.WARNING, format="    · %(message)s")
    logging.getLogger("ratio.ai").setLevel(logging.INFO)

    status = ai.ai_status()
    print("Configuración de IA")
    print(f"  Generador : {settings.generator.model or '—'} @ {settings.generator.base_url or '—'}")
    print(f"  Verificador: {settings.verifier.model or '—'} @ {settings.verifier.base_url or '—'}")
    print(f"  Búsqueda web (Google Search): {'sí' if status['web_search'] else 'no'}")
    print(f"  Nivel de razonamiento: {settings.ai_reasoning_effort or 'por defecto del modelo (puede ser lento)'}"
          f" · solo IPv4: {'sí' if settings.ai_force_ipv4 else 'no'}"
          f" · tiempo máximo por llamada: {settings.ai_timeout:.0f} s")
    if not status["generator"]:
        print("\n✘ Falta configurar AI_BASE_URL, AI_API_KEY y AI_MODEL_GENERATOR en el archivo .env")
        return 1

    _network_check()
    if "--red" in sys.argv:
        return 0

    ping = _timed("1/4 Conexión básica con el generador",
                  lambda: ai.generator.chat_json('Responde solo JSON: {"ok": true, "modelo": "<tu nombre>"}', "ping"))
    if ping is None:
        _list_models()
        return 1
    print("  Respuesta:", ping)

    sources = [{"id": "src_test", "citation": "Ley 2466 de 2025, arts. 10 y 14",
                "fragment": "Trabajo nocturno entre las 7:00 p. m. y las 6:00 a. m. Recargo dominical y festivo: "
                            "80 % desde el 1 de julio de 2025, 90 % desde el 1 de julio de 2026 y 100 % desde el "
                            "1 de julio de 2027."}]
    batch = _timed("2/4 Generar un lote de 3 preguntas desde una fuente",
                   lambda: ai.generate_batch(subtopic_name="Jornada laboral y recargos",
                                             concepts=["Trabajo nocturno", "Recargo dominical"],
                                             sources=sources, count=3, existing_stems=[]))
    if batch:
        questions, errors = batch
        for q in questions:
            print(f"  · [nivel {q['level']}] {q['stem']}  (correcta: {q['correct_key']})")
        if errors:
            print("  Descartadas:", errors)
        checks = _timed("3/4 Verificador independiente",
                        lambda: ai.verify_batch(questions, {s["id"]: s for s in sources}))
        if checks:
            for q, c in zip(questions, checks):
                print(f"  · {'aprobada' if c['approved'] else 'RECHAZADA'} · riesgo {c['risk']} · {c['issues']}")

    if status["web_search"]:
        result = _timed("4/4 Mapa de un tema libre con Google Search (el paso más lento)",
                        lambda: ai.build_topic_map("Sucesión intestada", "estudiante, objetivo examen"))
        if result:
            data, web, searched, search_error = result
            print(f"  Búsqueda usada: {searched} · {len(web)} resultados web")
            if search_error:
                print(f"  Motivo por el que no se usó la búsqueda: {search_error}")
            for s in data.get("subtopics", []):
                print(f"  · {s.get('name')} — {len(s.get('sources') or [])} fuentes")
            if "--json" in sys.argv:
                print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print("\n4/4 Búsqueda web omitida (requiere Gemini y AI_WEB_SEARCH=true)")
    print("\nListo. Si todo salió OK, reinicia el servidor y elige un área: verás la preparación con IA.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
