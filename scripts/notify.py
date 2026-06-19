#!/usr/bin/env python3
"""
Envío de correos de notificación vía SMTP de Gmail.

Variables de entorno requeridas:
- GMAIL_ADDRESS: cuenta remitente (la que generó la contraseña de aplicación)
- GMAIL_APP_PASSWORD: contraseña de aplicación de 16 caracteres
- NOTIFY_EMAIL_TO: correo receptor donde quieres recibir los avisos
"""

import os
import smtplib
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def send_email(subject: str, body: str) -> None:
    """Envía un correo de texto plano. Si faltan credenciales, solo avisa y sigue."""
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("NOTIFY_EMAIL_TO")

    if not sender or not password or not recipient:
        print("⚠️  Faltan credenciales de correo (GMAIL_ADDRESS / GMAIL_APP_PASSWORD / "
              "NOTIFY_EMAIL_TO). No se envía notificación, pero el script continúa.")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        print(f"📧 Correo enviado: '{subject}'")
    except Exception as e:
        # Un fallo al enviar el correo nunca debe tumbar el flujo principal.
        print(f"⚠️  No se pudo enviar el correo '{subject}': {type(e).__name__}: {e}")
