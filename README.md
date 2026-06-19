# Reintento automático de instancia ARM en Oracle Cloud

Este repositorio reintenta crear una instancia `VM.Standard.A1.Flex` (4 OCPU, 24GB RAM, Always Free)
en Oracle Cloud (cuenta `pasatedigitalvm`, región **US West (Phoenix)**) cada 10 minutos hasta que
Oracle tenga capacidad disponible, y te avisa por correo en el camino.

El script prueba, en orden, los **3 dominios de disponibilidad** de la región Phoenix en cada
corrida. Si el AD 1 no tiene capacidad, prueba el AD 2, luego el AD 3, antes de rendirse hasta la
siguiente corrida del cron.

## Correos que vas a recibir

1. **🚀 Inicio** — una sola vez, en la primera corrida del workflow.
2. **📊 Resumen** — cada 12 horas, con el número de intentos realizados mientras sigue sin haber capacidad (en ninguno de los 3 AD).
3. **✅ Éxito** — cuando la instancia se crea. Incluye OCID, AD usado, IP pública y el comando SSH para conectarte.
   Este correo también cierra el flujo (no hay un correo de cierre separado).

## Secrets necesarios (Settings → Secrets and variables → Actions)

| Secret | Descripción | Ejemplo |
|---|---|---|
| `OCI_USER_OCID` | OCID de tu usuario (cuenta Phoenix) | `ocid1.user.oc1..xxxx` |
| `OCI_FINGERPRINT` | Fingerprint de tu clave de **API** (no la SSH) | `be:16:60:55:...` |
| `OCI_TENANCY_OCID` | OCID de tu tenancy `pasatedigitalvm` (= compartimento raíz) | `ocid1.tenancy.oc1..xxxx` |
| `OCI_REGION` | Región | `us-phoenix-1` |
| `OCI_SUBNET_OCID` | OCID de la subred pública (no el de la VCN) | `ocid1.subnet.oc1.phx.xxxx` |
| `OCI_PRIVATE_KEY` | Contenido completo de la clave privada de **API** (incluye `BEGIN/END PRIVATE KEY`) | — |
| `OCI_AVAILABILITY_DOMAIN` | **Lista de los 3 AD separados por comas, sin espacios** | `XvQL:PHX-AD-1,XvQL:PHX-AD-2,XvQL:PHX-AD-3` |
| `OCI_SSH_PUBLIC_KEY` | Contenido de la clave pública **SSH** (la del formulario de Red al crear la instancia) | `ssh-rsa AAAA...` |
| `GMAIL_ADDRESS` | Correo **remitente** (la cuenta Gmail donde generaste la contraseña de aplicación) | `tu_cuenta@gmail.com` |
| `GMAIL_APP_PASSWORD` | Contraseña de aplicación de 16 caracteres de esa cuenta | `abcd efgh ijkl mnop` |
| `NOTIFY_EMAIL_TO` | Correo **receptor** donde quieres que lleguen los avisos (puede ser distinto al remitente) | `tucorreo@outlook.com` |

⚠️ No confundir clave de **API** (autentica el script contra Oracle) con clave **SSH** (te permite
conectarte a la VM una vez creada). Tampoco confundir `GMAIL_ADDRESS` (remitente) con `NOTIFY_EMAIL_TO`
(receptor) — pueden ser el mismo correo o distintos.

⚠️ `OCI_AVAILABILITY_DOMAIN` **ya no es un solo valor**: ahora acepta una lista separada por comas.
El script los prueba en el orden en que los escribas. No dejes espacios después de cada coma.

## Cómo funciona la validación de múltiples dominios de disponibilidad

En cada corrida, el script:

1. Verifica si la instancia ya existe (evita duplicados).
2. Busca la imagen Ubuntu 22.04 ARM más reciente (una sola vez).
3. Intenta crear la instancia en **AD-1**.
   - Si Oracle responde "Out of host capacity" → pasa a **AD-2**.
   - Si tampoco hay capacidad → pasa a **AD-3**.
   - Si ninguno de los 3 tiene capacidad → termina con `exit 1`, y el cron vuelve a intentar
     (los 3 AD de nuevo) en 10 minutos.
   - Si CUALQUIER otro tipo de error ocurre (credenciales, subnet inválida, etc.) → se detiene
     de inmediato sin seguir probando los demás AD, porque ese tipo de error no se resuelve
     cambiando de dominio.
4. En cuanto un AD tiene éxito, crea la instancia, envía el correo de éxito (indicando qué AD se
   usó) y termina el proceso — no sigue probando los demás.

## Cómo funciona el estado entre corridas (`state.json`)

GitHub Actions ejecuta el script desde cero en cada corrida, así que no hay memoria automática entre
una ejecución y la siguiente. Para saber si ya se envió el correo de inicio, cuántos intentos van, y
cuándo toca el próximo resumen, el script guarda esa información en `state.json` en la raíz del repo.

Al final de cada corrida, el workflow comitea automáticamente los cambios en `state.json` de vuelta
al repositorio (con el mensaje `chore: actualizar estado de reintento`), para que la
siguiente corrida arranque con el estado correcto. No necesitas tocar ese archivo manualmente, salvo
si cambias de cuenta/tenancy (como ocurrió aquí), en cuyo caso conviene resetearlo a:

```json
{
  "started_at": null,
  "attempts": 0,
  "start_email_sent": false,
  "last_summary_email_at": null,
  "finished": false
}
```

Estructura normal de `state.json` mientras corre:

```json
{
  "started_at": "2026-06-18T20:00:00+00:00",
  "attempts": 7,
  "start_email_sent": true,
  "last_summary_email_at": null,
  "finished": false
}
```

## Cómo usarlo

1. Agrega los 10 secrets de la tabla arriba con los datos de la cuenta `pasatedigitalvm` (Phoenix).
2. Resetea `state.json` a los valores limpios de arriba (importante al migrar de cuenta/región).
3. El workflow corre solo cada 10 minutos automáticamente (`schedule`).
4. También puedes forzar una corrida manual: pestaña **Actions** → **Reintento Oracle ARM** → **Run workflow**.
5. Revisa los logs de cada corrida en la pestaña **Actions**:
   - `--- Probando dominio de disponibilidad: XvQL:PHX-AD-1 ---` → indica qué AD se está probando en ese momento.
   - `⏳ Sin capacidad en XvQL:PHX-AD-1` → pasa al siguiente AD automáticamente.
   - `⏳ Sin capacidad disponible todavía en ningún dominio` → ninguno de los 3 tuvo capacidad, normal, reintentará solo.
   - `✅ ¡Instancia creada con éxito!` → listo, revisa tu correo y la consola de Oracle.
6. Cuando llegue el correo de éxito, **desactiva el workflow** (Actions → ⋯ → Disable workflow)
   para que deje de correr.

## Troubleshooting

**`CannotParseRequest` (status 400)**
El JSON enviado a Oracle estaba mal formado, generalmente por un valor de `OCI_AVAILABILITY_DOMAIN`
con el prefijo incorrecto. Verifícalo desde la consola web (Identidad y seguridad → Dominios de
disponibilidad, o al crear una instancia sin confirmar) y copia el valor exacto, incluyendo el
prefijo de 4 letras (que es único por tenancy, en tu caso `XvQL`).

**`Out of host capacity` (status 500, `code: InternalError`)**
Caso esperado. Oracle cambió el formato de este error con el tiempo — antes era `OutOfCapacity` (400),
ahora puede venir como `InternalError` (500) con el mensaje `"Out of host capacity."`. El script
detecta ambos formatos en cada AD y, si ningún AD tiene capacidad, reintenta en 10 minutos sin
marcarlo como fallo real.

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

**Migré de cuenta/región y el script sigue mostrando los intentos viejos**
Resetea manualmente `state.json` a los valores limpios (ver sección de arriba) y comitéalo. Si no lo
haces, `attempts` seguirá sumando desde el número anterior (no rompe nada funcionalmente, pero el
contador y la fecha de inicio en los correos quedarán desactualizados).

## Importante

- GitHub Actions en repos privados tiene minutos gratis limitados al mes, pero este job es muy
  rápido (segundos por corrida, incluso probando los 3 AD), así que no debería ser un problema en
  el plan gratuito.
- Una vez tengas tu instancia, **rota (regenera) tu clave de API** en Oracle Cloud por seguridad.
