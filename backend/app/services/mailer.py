"""Envío del informe por correo cuando queda verificado (patrón: tarea en segundo plano).

Se configura con SMTP_USER / SMTP_PASSWORD en el .env. Con Gmail, SMTP_PASSWORD debe ser una
"contraseña de aplicación" (16 letras), no la contraseña normal de la cuenta.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape

from ..config import settings
from ..db import SessionLocal
from ..models import DiagnosticSession
log = logging.getLogger("ratio.mail")

try:  # fpdf2 genera el PDF; si falta, el servidor arranca igual y el correo queda desactivado
    from . import report_pdf
except ImportError:  # pragma: no cover
    report_pdf = None
    log.warning("Falta la librería fpdf2: instala las dependencias (pip install -r requirements.txt) "
                "para enviar el informe por correo.")


API_PROVIDERS = {"brevo", "resend"}


def provider() -> str:
    if settings.mail_provider in API_PROVIDERS and settings.mail_api_key:
        return settings.mail_provider
    if settings.smtp_user and settings.smtp_password:
        return "smtp"
    return ""


def sender() -> str:
    return settings.mail_from or settings.smtp_user


def enabled() -> bool:
    return bool(provider() and sender() and report_pdf is not None)


def email_info(report) -> dict:
    return {"enabled": enabled(), "status": report.email_status or "",
            "sent_at": report.email_sent_at.isoformat() if report.email_sent_at else None,
            "error": report.email_error or ""}


def _html_body(payload: dict) -> str:
    profile, result = payload["profile"], payload["result"]
    g = result.get("global", {})
    steps = "".join(
        f"<li style='margin-bottom:8px'><strong>{escape(i.get('subtopic', ''))}</strong> — {escape(i.get('objective', ''))}</li>"
        for i in payload.get("route", [])[:8])
    strengths = ", ".join(result.get("strengths") or []) or "—"
    gaps = ", ".join((result.get("gaps") or []) + (result.get("developing") or [])) or "—"
    link = f"{settings.app_url.rstrip('/')}/#/resultado/{payload['session_id']}"
    return f"""<!doctype html><html><body style="margin:0;background:#f6f2ea;font-family:Arial,Helvetica,sans-serif;color:#1d2a44">
<div style="max-width:600px;margin:0 auto;padding:24px">
  <div style="background:#1d2a44;color:#f6f2ea;border-radius:14px 14px 0 0;padding:18px 22px">
    <img src="cid:ratio-logo" width="40" height="40" alt="" style="vertical-align:middle;border:0;border-radius:9px">
    <span style="font:bold 22px Georgia,serif;margin-left:10px;vertical-align:middle">Ratio</span>
    <span style="display:block;margin-top:6px;font:bold 10px Arial,sans-serif;letter-spacing:3px;color:#7fa7e6">DIAGNOSTICAMOS TU CONOCIMIENTO</span>
  </div>
  <div style="background:#fffdf8;border:1px solid #ddd5c6;border-top:0;border-radius:0 0 14px 14px;padding:22px">
    <p>Hola, {escape(profile.get('nombre', ''))}:</p>
    <p>Tu diagnóstico de <strong>{escape(payload.get('area_name', ''))}</strong> ya fue verificado. Adjuntamos el informe completo en PDF.</p>
    <table style="width:100%;border-collapse:collapse;margin:14px 0;font-size:14px">
      <tr><td style="padding:8px;border:1px solid #ddd5c6;background:#f6f2ea"><strong>Nivel global</strong></td><td style="padding:8px;border:1px solid #ddd5c6">{escape(g.get('level_name', '—'))}</td></tr>
      <tr><td style="padding:8px;border:1px solid #ddd5c6;background:#f6f2ea"><strong>Fortalezas</strong></td><td style="padding:8px;border:1px solid #ddd5c6">{escape(strengths)}</td></tr>
      <tr><td style="padding:8px;border:1px solid #ddd5c6;background:#f6f2ea"><strong>Por reforzar</strong></td><td style="padding:8px;border:1px solid #ddd5c6">{escape(gaps)}</td></tr>
    </table>
    <p style="margin-bottom:6px"><strong>Tu ruta de estudio</strong></p>
    <ol style="padding-left:20px;font-size:14px">{steps}</ol>
    <p><a href="{escape(link)}" style="display:inline-block;background:#1d2a44;color:#f6f2ea;text-decoration:none;padding:10px 18px;border-radius:999px">Ver mi resultado en Ratio</a></p>
    <p style="font-size:12px;color:#4a5568">Ratio evalúa y orienta; no reemplaza la asesoría de un abogado. Recibiste este correo porque presentaste un diagnóstico con esta dirección.</p>
  </div>
</div></body></html>"""


def build_message(payload: dict) -> EmailMessage:
    profile = payload["profile"]
    msg = EmailMessage()
    msg["Subject"] = f"Tu informe de diagnóstico jurídico — {payload.get('area_name', '')}"
    msg["From"] = f"{settings.mail_from_name} <{sender()}>"
    msg["To"] = profile["email"]
    msg.set_content(f"Hola, {profile.get('nombre', '')}. Tu diagnóstico ya fue verificado; el informe va adjunto en PDF.")
    msg.add_alternative(_html_body(payload), subtype="html")
    if report_pdf.LOGO.exists():  # el logo va incrustado (los correos bloquean imágenes externas)
        html_part = msg.get_payload()[1]
        html_part.add_related(report_pdf.LOGO.read_bytes(), maintype="image", subtype="png", cid="<ratio-logo>")
    name = f"Informe Ratio - {profile.get('nombre', 'diagnostico')}.pdf".replace("/", "-")
    msg.add_attachment(report_pdf.build_pdf(payload), maintype="application", subtype="pdf", filename=name)
    return msg


def _send(msg: EmailMessage) -> None:
    if provider() in API_PROVIDERS:
        _send_api(msg)
        return
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15, context=context) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.starttls(context=context)
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)


def _send_api(msg: EmailMessage) -> None:
    """Envío por API HTTPS (puerto 443): funciona aunque el hosting bloquee los puertos SMTP."""
    import base64

    import httpx

    text = html = ""
    files = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_filename():
            files.append((part.get_filename(), base64.b64encode(part.get_payload(decode=True)).decode()))
        elif ctype == "text/plain" and not text:
            text = part.get_content()
        elif ctype == "text/html" and not html:
            html = part.get_content()
    # Las API no usan imágenes «cid»: el logo se toma de la app publicada (o se omite si es local).
    logo = f"{settings.app_url.rstrip('/')}/img/ratio-icon.png"
    html = html.replace("cid:ratio-logo", logo if settings.app_url.startswith("https://") else "")
    to = str(msg["To"])
    if provider() == "brevo":
        res = httpx.post("https://api.brevo.com/v3/smtp/email", timeout=30,
                         headers={"api-key": settings.mail_api_key, "accept": "application/json"},
                         json={"sender": {"name": settings.mail_from_name, "email": sender()}, "to": [{"email": to}],
                               "subject": str(msg["Subject"]), "htmlContent": html, "textContent": text,
                               "attachment": [{"name": n, "content": c} for n, c in files]})
    else:
        res = httpx.post("https://api.resend.com/emails", timeout=30,
                         headers={"Authorization": f"Bearer {settings.mail_api_key}"},
                         json={"from": f"{settings.mail_from_name} <{sender()}>", "to": [to],
                               "subject": str(msg["Subject"]), "html": html, "text": text,
                               "attachments": [{"filename": n, "content": c} for n, c in files]})
    if res.status_code >= 300:
        raise RuntimeError(f"{provider()} respondió {res.status_code}: {res.text[:200]}")


def send_report_now(session_id: str) -> str:
    """Envía el informe (bloqueante). Devuelve el estado final: enviado | error | sin_configurar."""
    from .diagnostic import download_allowed, report_payload  # import diferido: evita ciclos

    with SessionLocal() as db:
        session = db.get(DiagnosticSession, session_id)
        if not session or not session.report or not download_allowed(session.report):
            return "no_aplica"
        report = session.report
        if not enabled():
            report.email_status = "sin_configurar"
            db.commit()
            return report.email_status
        payload = report_payload(session)
        try:
            if not payload["profile"].get("email"):
                raise ValueError("La sesión no tiene correo de destino")
            _send(build_message(payload))
            report.email_status, report.email_error = "enviado", ""
            report.email_sent_at = datetime.now(timezone.utc)
        except smtplib.SMTPAuthenticationError:
            report.email_status = "error"
            report.email_error = ("Gmail rechazó el usuario o la contraseña. SMTP_PASSWORD debe ser una "
                                  "contraseña de aplicación de 16 letras (requiere verificación en dos pasos).")
        except (OSError, TimeoutError) as exc:
            report.email_status = "error"
            report.email_error = str(exc)[:120]
            if provider() == "smtp" and not isinstance(exc, smtplib.SMTPException):  # p. ej. Render Free bloquea los puertos SMTP (25/465/587)
                report.email_error = ("El servidor no pudo conectarse por SMTP; el hosting probablemente bloquea "
                                      "ese puerto (Render Free lo hace). Usa MAIL_PROVIDER=brevo. Detalle: "
                                      + report.email_error)[:300]
        except Exception as exc:  # noqa: BLE001 - se registra para que el admin pueda reintentar
            report.email_status, report.email_error = "error", str(exc)[:300]
        if report.email_status == "error":
            log.warning("Correo del informe %s: %s", session_id, report.email_error)
        db.commit()
        return report.email_status


def send_report_async(session_id: str) -> None:
    """Marca el informe como «enviando» y lo envía en segundo plano (no demora la respuesta HTTP)."""
    if not enabled():
        return
    with SessionLocal() as db:
        session = db.get(DiagnosticSession, session_id)
        if session and session.report:
            session.report.email_status = "enviando"
            db.commit()
    if settings.serverless:  # sin hilos en serverless: se envía dentro de la misma solicitud (2-5 s)
        send_report_now(session_id)
        return
    threading.Thread(target=send_report_now, args=(session_id,), daemon=True, name=f"mail-{session_id}").start()
