#!/usr/bin/env python3
"""
Script que intenta crear una instancia VM.Standard.A1.Flex (ARM, Always Free)
en Oracle Cloud. Pensado para correr repetidamente desde GitHub Actions hasta
que Oracle tenga capacidad disponible.

Valida MÚLTIPLES dominios de disponibilidad (AD) dentro de la misma región:
si el AD 1 no tiene capacidad, prueba el AD 2, luego el AD 3, etc. Solo se
rinde (exit code 1, para que el cron reintente en 10 min) si NINGÚN AD tuvo
capacidad en esta corrida.

Envía 3 tipos de correo (vía scripts/notify.py), todos con la región en el
asunto:
- Primer correo ("inicio"): una sola vez, en la primera corrida del workflow.
  Incluye el detalle de qué pasó en CADA uno de los AD probados, para que
  puedas confirmar que el script efectivamente recorrió los 3 dominios.
- Resumen: cada ~12 horas mientras sigue intentando, con el número de intentos.
- Éxito: cuando la instancia se crea (incluye OCID, AD usado e IP, y también
  cierra el flujo).

El estado entre corridas (si ya se envió el correo de inicio, cuántos intentos
van, etc.) se guarda en scripts/state.py / state.json, que el workflow de
GitHub Actions debe comitear de vuelta al repo tras cada corrida.

Maneja tres tipos de fallo de forma distinta:
- Sin capacidad en NINGÚN AD ("Out of host capacity"): esperado, termina con
  exit code 1 para que el cron de GitHub Actions reintente en 10 minutos.
- Timeout de red transitorio: reintenta unas pocas veces dentro de la misma
  corrida (con espera corta) antes de rendirse con exit code 1.
- Cualquier otro error (credenciales, formato, etc.): error real, se imprime
  el detalle completo para depurar.
"""
import os
import sys
import time
import tempfile
import oci

from notify import send_email
from state import load_state, save_state, now_iso, hours_since

# Cuántas veces reintentar dentro de esta misma corrida ante un timeout de red,
# y cuánto esperar entre intentos (en segundos).
NETWORK_RETRY_ATTEMPTS = 3
NETWORK_RETRY_WAIT_SECONDS = 15

# Cada cuántas horas se envía el correo de resumen mientras se sigue intentando.
SUMMARY_INTERVAL_HOURS = 12


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: falta la variable de entorno {name}")
        sys.exit(1)
    return value


def get_availability_domains() -> list:
    """
    Lee OCI_AVAILABILITY_DOMAIN, que puede contener uno o varios AD separados
    por comas, ej: "XvQL:PHX-AD-1,XvQL:PHX-AD-2,XvQL:PHX-AD-3".
    """
    raw = get_env("OCI_AVAILABILITY_DOMAIN")
    domains = [d.strip() for d in raw.split(",") if d.strip()]
    if not domains:
        print("ERROR: OCI_AVAILABILITY_DOMAIN no contiene ningún dominio válido.")
        sys.exit(1)
    return domains


def build_config() -> dict:
    """Construye el config de OCI a partir de variables de entorno (secrets)."""
    private_key_content = get_env("OCI_PRIVATE_KEY")

    key_file = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    key_file.write(private_key_content)
    key_file.close()
    os.chmod(key_file.name, 0o600)

    config = {
        "user": get_env("OCI_USER_OCID"),
        "fingerprint": get_env("OCI_FINGERPRINT"),
        "tenancy": get_env("OCI_TENANCY_OCID"),
        "region": get_env("OCI_REGION"),
        "key_file": key_file.name,
    }
    oci.config.validate_config(config)
    return config


def instance_already_exists(compute_client, compartment_id: str, display_name: str):
    """Devuelve la instancia si ya existe con ese nombre (evita duplicados), o None."""
    response = compute_client.list_instances(compartment_id=compartment_id)
    for instance in response.data:
        if instance.display_name == display_name and instance.lifecycle_state not in (
            "TERMINATED",
            "TERMINATING",
        ):
            return instance
    return None


def get_public_ip(compute_client, network_client, compartment_id: str, instance_id: str) -> str:
    """Busca la IP pública asociada a la VNIC primaria de la instancia."""
    vnic_attachments = compute_client.list_vnic_attachments(
        compartment_id=compartment_id, instance_id=instance_id
    ).data
    for attachment in vnic_attachments:
        vnic = network_client.get_vnic(vnic_id=attachment.vnic_id).data
        if vnic.public_ip:
            return vnic.public_ip
    return "(sin IP pública asignada todavía, revisa la consola)"


def get_latest_ubuntu_arm_image(compute_client, compartment_id: str) -> str:
    """Busca la imagen Ubuntu 22.04 más reciente compatible con ARM (aarch64)."""
    images = compute_client.list_images(
        compartment_id=compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        print("ERROR: no se encontró ninguna imagen Ubuntu 22.04 ARM disponible.")
        sys.exit(1)
    return images[0].id


def is_out_of_capacity_error(e: "oci.exceptions.ServiceError") -> bool:
    message = str(getattr(e, "message", ""))
    code = str(getattr(e, "code", ""))
    return (
        "Out of capacity" in message
        or "Out of host capacity" in message
        or "OutOfCapacity" in code
    )


def print_service_error_details(e: "oci.exceptions.ServiceError") -> None:
    print(f"❌ Error inesperado de la API de Oracle: {e}")
    print("--- Detalle completo de la excepción ---")
    print(f"status: {e.status}")
    print(f"code: {e.code}")
    print(f"message: {e.message}")
    print(f"operation_name: {e.operation_name}")
    print(f"target_service: {e.target_service}")
    print(f"request_endpoint: {getattr(e, 'request_endpoint', 'N/A')}")


def send_success_email(instance, public_ip: str, state: dict, region: str) -> None:
    subject = f"✅ [{region}] Tu instancia Oracle ARM ya está creada"
    body = (
        "¡Buenas noticias! Tu instancia VM.Standard.A1.Flex (4 OCPU, 24GB RAM) "
        "ya fue creada en Oracle Cloud.\n\n"
        f"Región: {region}\n"
        f"Nombre: {instance.display_name}\n"
        f"OCID: {instance.id}\n"
        f"Estado: {instance.lifecycle_state}\n"
        f"Dominio de disponibilidad usado: {instance.availability_domain}\n"
        f"IP pública: {public_ip}\n\n"
        f"Intentos totales hasta lograrlo: {state['attempts']}\n\n"
        "Próximo paso: conéctate por SSH con tu clave privada:\n"
        f"  ssh -i tu_clave_privada.key ubuntu@{public_ip}\n\n"
        "El workflow de reintento ya no es necesario, puedes desactivarlo "
        "desde la pestaña Actions → ⋯ → Disable workflow.\n\n"
        "Este es el último correo de este proceso. ¡Listo!"
    )
    send_email(subject, body)


def send_first_run_email(state: dict, region: str, domains: list, domain_results: list) -> None:
    """
    Correo de "inicio", enviado una sola vez, DESPUÉS del primer intento real.
    Incluye el detalle de qué pasó en cada uno de los AD probados, para
    confirmar que el script efectivamente recorrió todos los dominios
    configurados (y no se quedó pegado en el primero).
    """
    subject = f"🚀 [{region}] Iniciando reintento automático de instancia Oracle ARM"

    detalle_dominios = "\n".join(f"  - {ad}: {resultado}" for ad, resultado in domain_results)

    body = (
        "Se acaba de activar el proceso de reintento automático para crear tu "
        "instancia VM.Standard.A1.Flex (4 OCPU, 24GB RAM) en Oracle Cloud.\n\n"
        f"Región: {region}\n"
        f"Dominios de disponibilidad configurados: {', '.join(domains)}\n\n"
        "Resultado del primer intento, dominio por dominio (esto confirma que "
        "el script SÍ recorre todos los AD configurados):\n"
        f"{detalle_dominios}\n\n"
        f"Hora de inicio (UTC): {state['started_at']}\n\n"
        "El script seguirá intentando cada 10 minutos, probando los mismos "
        f"dominios en el mismo orden. Recibirás un correo de resumen cada "
        f"{SUMMARY_INTERVAL_HOURS} horas, y un correo final cuando la "
        "instancia se cree con éxito."
    )
    send_email(subject, body)


def send_summary_email(state: dict, region: str) -> None:
    elapsed = hours_since(state["started_at"])
    subject = f"📊 [{region}] Resumen: {state['attempts']} intentos en {elapsed:.1f}h"
    body = (
        "Resumen del proceso de reintento automático de tu instancia Oracle ARM.\n\n"
        f"Región: {region}\n"
        f"Tiempo transcurrido: {elapsed:.1f} horas\n"
        f"Intentos realizados: {state['attempts']}\n"
        "Estado: todavía sin capacidad disponible en Oracle (en ninguno de los "
        "dominios configurados), el script sigue reintentando automáticamente "
        "cada 10 minutos.\n\n"
        "No necesitas hacer nada, te avisaremos en cuanto se cree la instancia."
    )
    send_email(subject, body)


def run_attempt(config: dict, state: dict, availability_domains: list, region: str):
    """
    Ejecuta un intento completo: revisar si ya existe, buscar imagen, y luego
    intentar lanzar la instancia probando cada AD de la lista en orden hasta
    que uno tenga capacidad.

    Si tiene éxito (instancia nueva o ya existente), envía el correo de éxito
    y termina el proceso con sys.exit(0).

    Si TODOS los AD fallan por falta de capacidad, vuelve a lanzar la última
    excepción de "out of capacity" para que el llamador la maneje igual que
    antes (exit code 1, reintento en 10 min vía cron).

    Si algún AD falla por un error que NO es de capacidad (credenciales,
    formato, etc.), se detiene inmediatamente y propaga ese error: no tiene
    sentido seguir probando otros AD si el problema es, por ejemplo, un
    subnet_id inválido.

    Antes de terminar (por éxito o por agotar los AD), si es la primera
    corrida del workflow, envía el correo de "inicio" con el detalle de qué
    pasó en cada AD probado.
    """
    compartment_id = get_env("OCI_TENANCY_OCID")  # compartimento raíz
    subnet_id = get_env("OCI_SUBNET_OCID")
    display_name = os.environ.get("OCI_INSTANCE_NAME", "pasatedigital")
    ssh_public_key = get_env("OCI_SSH_PUBLIC_KEY").strip()
    ssh_public_key = " ".join(ssh_public_key.split())
    ocpus = float(os.environ.get("OCI_OCPUS", "4"))
    memory_gb = float(os.environ.get("OCI_MEMORY_GB", "24"))
    boot_volume_gb = int(os.environ.get("OCI_BOOT_VOLUME_GB", "50"))

    compute_client = oci.core.ComputeClient(config)
    network_client = oci.core.VirtualNetworkClient(config)

    existing = instance_already_exists(compute_client, compartment_id, display_name)
    if existing:
        print(f"La instancia '{display_name}' ya existe.")
        public_ip = get_public_ip(compute_client, network_client, compartment_id, existing.id)
        if not state["finished"]:
            send_success_email(existing, public_ip, state, region)
            state["finished"] = True
            save_state(state)
        sys.exit(0)

    image_id = get_latest_ubuntu_arm_image(compute_client, compartment_id)
    print(f"Usando imagen: {image_id}")

    last_capacity_error = None
    domain_results = []  # lista de tuplas (ad, "texto de qué pasó")

    for ad in availability_domains:
        print(f"--- Probando dominio de disponibilidad: {ad} ---")
        launch_details = oci.core.models.LaunchInstanceDetails(
            availability_domain=ad,
            compartment_id=compartment_id,
            display_name=display_name,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ocpus,
                memory_in_gbs=memory_gb,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=boot_volume_gb,
            ),
            metadata={
                "ssh_authorized_keys": ssh_public_key,
            },
        )

        try:
            print(f"Intentando crear la instancia en {ad}...")
            response = compute_client.launch_instance(launch_details)
            instance = response.data
            print("✅ ¡Instancia creada con éxito!")
            print(f"OCID: {instance.id}")
            print(f"Estado: {instance.lifecycle_state}")
            print(f"AD usado: {ad}")

            domain_results.append((ad, "✅ tenía capacidad, instancia creada aquí"))

            if not state["start_email_sent"]:
                send_first_run_email(state, region, availability_domains, domain_results)
                state["start_email_sent"] = True

            public_ip = get_public_ip(compute_client, network_client, compartment_id, instance.id)
            send_success_email(instance, public_ip, state, region)
            state["finished"] = True
            save_state(state)
            sys.exit(0)

        except oci.exceptions.ServiceError as e:
            if is_out_of_capacity_error(e):
                print(f"⏳ Sin capacidad en {ad}. Probando el siguiente dominio (si hay)...")
                domain_results.append((ad, "⏳ sin capacidad"))
                last_capacity_error = e
                continue  # probar el siguiente AD
            else:
                domain_results.append((ad, f"❌ error real: {e.code}"))
                if not state["start_email_sent"]:
                    send_first_run_email(state, region, availability_domains, domain_results)
                    state["start_email_sent"] = True
                # Error real (no de capacidad): no tiene sentido seguir
                # probando otros AD, propagamos para que main() lo reporte.
                raise

    # Si llegamos aquí, ningún AD tuvo capacidad en esta corrida.
    print("⏳ Sin capacidad disponible todavía en ningún dominio. Se reintentará en la próxima corrida.")

    if not state["start_email_sent"]:
        send_first_run_email(state, region, availability_domains, domain_results)
        state["start_email_sent"] = True
        save_state(state)

    raise last_capacity_error


def main():
    state = load_state()
    availability_domains = get_availability_domains()
    region = get_env("OCI_REGION")

    # Primera corrida: registramos hora de inicio.
    # OJO: el correo de "inicio" ya NO se envía aquí, sino dentro de
    # run_attempt, después de probar los AD, para poder incluir el detalle
    # de qué pasó en cada uno.
    if state["started_at"] is None:
        state["started_at"] = now_iso()
    state["attempts"] += 1

    # Correo de resumen cada SUMMARY_INTERVAL_HOURS horas.
    last_summary = state["last_summary_email_at"]
    should_send_summary = (
        last_summary is None
        and hours_since(state["started_at"]) >= SUMMARY_INTERVAL_HOURS
    ) or (
        last_summary is not None
        and hours_since(last_summary) >= SUMMARY_INTERVAL_HOURS
    )
    if should_send_summary and not state["finished"]:
        send_summary_email(state, region)
        state["last_summary_email_at"] = now_iso()

    # Guardamos el estado ya actualizado ANTES de intentar el launch, para que
    # quede registrado el intento incluso si la corrida falla por timeout.
    save_state(state)

    config = build_config()

    for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
        try:
            run_attempt(config, state, availability_domains, region)
            return  # run_attempt termina el proceso por sí mismo (sys.exit)

        except oci.exceptions.ServiceError as e:
            if is_out_of_capacity_error(e):
                # Ya se probaron todos los AD dentro de run_attempt.
                sys.exit(1)
            else:
                print_service_error_details(e)
                sys.exit(1)

        except (oci.exceptions.ConnectTimeout, oci.exceptions.RequestException) as e:
            print(
                f"⏳ Intento {attempt}/{NETWORK_RETRY_ATTEMPTS}: "
                f"problema de red transitorio al hablar con Oracle."
            )
            print(f"Detalle: {e}")
            if attempt < NETWORK_RETRY_ATTEMPTS:
                print(f"Esperando {NETWORK_RETRY_WAIT_SECONDS}s antes de reintentar...")
                time.sleep(NETWORK_RETRY_WAIT_SECONDS)
            else:
                print(
                    "Se agotaron los reintentos de red en esta corrida. "
                    "El cron volverá a intentar en 10 minutos."
                )
                sys.exit(1)

        except Exception as e:
            print(f"❌ Error no esperado: {type(e).__name__}: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
