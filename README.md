# Reintento automático de instancia ARM en Oracle Cloud

Este repositorio reintenta crear una instancia `VM.Standard.A1.Flex` (4 OCPU, 24GB RAM, Always Free)
cada 10 minutos hasta que Oracle Cloud tenga capacidad disponible, y te avisa por correo en el camino.

## Correos que vas a recibir

1. **🚀 Inicio** — una sola vez, en la primera corrida del workflow.
2. **📊 Resumen** — cada 12 horas, con el número de intentos realizados mientras sigue sin haber capacidad.
3. **✅ Éxito** — cuando la instancia se crea. Incluye OCID, IP pública y el comando SSH para conectarte.
   Este correo también cierra el flujo (no hay un correo de cierre separado).

## Secrets necesarios (Settings → Secrets and variables → Actions)

| Secret | Descripción | Ejemplo |
|---|---|---|
| `OCI_USER_OCID` | OCID de tu usuario | `ocid1.user.oc1..xxxx` |
| `OCI_FINGERPRINT` | Fingerprint de tu clave de **API** (no la SSH) | `be:16:60:55:...` |
| `OCI_TENANCY_OCID` | OCID de tu tenancy (= compartimento raíz) | `ocid1.tenancy.oc1..xxxx` |
| `OCI_REGION` | Región | `sa-saopaulo-1` |
| `OCI_SUBNET_OCID` | OCID de la subred pública (no el de la VCN) | `ocid1.subnet.oc1.sa-saopaulo-1.xxxx` |
| `OCI_PRIVATE_KEY` | Contenido completo de la clave privada de **API** (incluye `BEGIN/END PRIVATE KEY`) | — |
| `OCI_AVAILABILITY_DOMAIN` | Nombre completo del AD. El prefijo de 4 letras es único por tenancy | `wfha:SA-SAOPAULO-1-AD-1` |
| `OCI_SSH_PUBLIC_KEY` | Contenido de la clave pública **SSH** (la del formulario de Red al crear la instancia) | `ssh-rsa AAAA...` |
| `GMAIL_ADDRESS` | Correo **remitente** (la cuenta Gmail donde generaste la contraseña de aplicación) | `tu_cuenta@gmail.com` |
| `GMAIL_APP_PASSWORD` | Contraseña de aplicación de 16 caracteres de esa cuenta | `abcd efgh ijkl mnop` |
| `NOTIFY_EMAIL_TO` | Correo **receptor** donde quieres que lleguen los avisos (puede ser distinto al remitente) | `tucorreo@outlook.com` |

⚠️ No confundir clave de **API** (autentica el script contra Oracle) con clave **SSH** (te permite
conectarte a la VM una vez creada). Tampoco confundir `GMAIL_ADDRESS` (remitente) con `NOTIFY_EMAIL_TO`
(receptor) — pueden ser el mismo correo o distintos.

## Cómo funciona el estado entre corridas (`state.json`)

GitHub Actions ejecuta el script desde cero en cada corrida, así que no hay memoria automática entre
una ejecución y la siguiente. Para saber si ya se envió el correo de inicio, cuántos intentos van, y
cuándo toca el próximo resumen, el script guarda esa información en `state.json` en la raíz del repo.

Al final de cada corrida, el workflow comitea automáticamente los cambios en `state.json` de vuelta
al repositorio (con el mensaje `chore: actualizar estado de reintento [skip ci]`), para que la
siguiente corrida arranque con el estado correcto. No necesitas tocar ese archivo manualmente.

Estructura de `state.json`:
```json
{
  "started_at": "2026-06-16T16:30:00+00:00",
  "attempts": 7,
  "start_email_sent": true,
  "last_summary_email_at": null,
  "finished": false
}
```

## Cómo usarlo

1. Agrega los 11 secrets de la tabla arriba.
2. El workflow corre solo cada 10 minutos automáticamente (`schedule`).
3. También puedes forzar una corrida manual: pestaña **Actions** → **Reintento Oracle ARM** → **Run workflow**.
4. Revisa los logs de cada corrida en la pestaña **Actions**:
   - `⏳ Sin capacidad disponible todavía` → normal, seguirá reintentando solo.
   - `✅ ¡Instancia creada con éxito!` → listo, revisa tu correo y la consola de Oracle.
5. Cuando llegue el correo de éxito, **desactiva el workflow** (Actions → ⋯ → Disable workflow)
   para que deje de correr.

## Troubleshooting

**`CannotParseRequest` (status 400)**
El JSON enviado a Oracle estaba mal formado, generalmente por un `OCI_AVAILABILITY_DOMAIN` con el
prefijo incorrecto. Verifícalo desde la consola web (sin confirmar la creación) y copia el valor
exacto bajo "Dominio de disponibilidad" — el prefijo de 4 letras es único por cuenta.

**`Out of host capacity` (status 500, `code: InternalError`)**
Caso esperado. Oracle cambió el formato de este error con el tiempo — antes era `OutOfCapacity` (400),
ahora puede venir como `InternalError` (500) con el mensaje `"Out of host capacity."`. El script
detecta ambos formatos y reintenta en 10 minutos sin marcarlo como fallo real.

**`ConnectTimeout` al hablar con la API de Oracle**
Problema de red transitorio entre el runner de GitHub y Oracle. El script reintenta automáticamente
hasta 3 veces dentro de la misma corrida (esperando 15s entre cada intento) antes de rendirse.

**No llegan los correos**
Revisa que los 3 secrets de Gmail estén bien escritos. Si `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` o
`NOTIFY_EMAIL_TO` faltan o están vacíos, el script lo indica en el log con un mensaje de advertencia
y continúa sin enviar el correo (no rompe el flujo principal).

**El workflow no logra comitear `state.json`**
Verifica que el repo tenga habilitado "Allow GitHub Actions to create and approve pull requests" o
al menos permisos de escritura por defecto en Settings → Actions → General → Workflow permissions
→ "Read and write permissions".

## Importante

- GitHub Actions en repos privados tiene minutos gratis limitados al mes, pero este job es muy
  rápido (segundos por corrida), así que no debería ser un problema en el plan gratuito.
- Una vez tengas tu instancia, **rota (regenera) tu clave de API** en Oracle Cloud por seguridad.
