#!/usr/bin/env python3
"""
Script que intenta crear una instancia VM.Standard.A1.Flex (ARM, Always Free)
en Oracle Cloud. Pensado para correr repetidamente desde GitHub Actions hasta
que Oracle tenga capacidad disponible.

Envía 3 tipos de correo (vía scripts/notify.py):
- Inicio: una sola vez, en la primera corrida del workflow.
- Resumen: cada ~12 horas mientras sigue intentando, con el número de intentos.
- Éxito: cuando la instancia se crea (incluye OCID e IP, y también cierra el flujo).

El estado entre corridas (si ya se envió el correo de inicio, cuántos intentos
van, etc.) se guarda en scripts/state.py / state.json, que el workflow de
GitHub Actions debe comitear de vuelta al repo tras cada corrida.

Maneja tres tipos de fallo de forma distinta:
- Sin capacidad ("Out of host capacity"): esperado, termina con exit code 1
  para que el cron de GitHub Actions reintente en 10 minutos.
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


def send_success_email(instance, public_ip: str, state: dict) -> None:
    subject = "✅ Tu instancia Oracle ARM ya está creada"
    body = (
        "¡Buenas noticias! Tu instancia VM.Standard.A1.Flex (4 OCPU, 24GB RAM) "
        "ya fue creada en Oracle Cloud.\n\n"
        f"Nombre: {instance.display_name}\n"
        f"OCID: {instance.id}\n"
        f"Estado: {instance.lifecycle_state}\n"
        f"IP pública: {public_ip}\n\n"
        f"Intentos totales hasta lograrlo: {state['attempts']}\n\n"
        "Próximo paso: conéctate por SSH con tu clave privada:\n"
        f"  ssh -i tu_clave_privada.key ubuntu@{public_ip}\n\n"
        "El workflow de reintento ya no es necesario, puedes desactivarlo "
        "desde la pestaña Actions → ⋯ → Disable workflow.\n\n"
        "Este es el último correo de este proceso. ¡Listo!"
    )
    send_email(subject, body)


def send_start_email(state: dict) -> None:
    subject = "🚀 Iniciando reintento automático de instancia Oracle ARM"
    body = (
        "Se acaba de activar el proceso de reintento automático para crear tu "
        "instancia VM.Standard.A1.Flex (4 OCPU, 24GB RAM) en Oracle Cloud.\n\n"
        "El script intentará crear la instancia cada 10 minutos hasta que haya "
        "capacidad disponible.\n\n"
        f"Hora de inicio (UTC): {state['started_at']}\n\n"
        f"Recibirás un correo de resumen cada {SUMMARY_INTERVAL_HOURS} horas, "
        "y un correo final cuando la instancia se cree con éxito."
    )
    send_email(subject, body)


def send_summary_email(state: dict) -> None:
    elapsed = hours_since(state["started_at"])
    subject = f"📊 Resumen: {state['attempts']} intentos en {elapsed:.1f}h"
    body = (
        "Resumen del proceso de reintento automático de tu instancia Oracle ARM.\n\n"
        f"Tiempo transcurrido: {elapsed:.1f} horas\n"
        f"Intentos realizados: {state['attempts']}\n"
        "Estado: todavía sin capacidad disponible en Oracle, el script sigue "
        "reintentando automáticamente cada 10 minutos.\n\n"
        "No necesitas hacer nada, te avisaremos en cuanto se cree la instancia."
    )
    send_email(subject, body)


def run_attempt(config: dict, state: dict):
    """
    Ejecuta un intento completo: revisar si ya existe, buscar imagen, y lanzar
    la instancia. Si tiene éxito (instancia nueva o ya existente), envía el
    correo de éxito y termina el proceso con sys.exit(0).
    """
    compartment_id = get_env("OCI_TENANCY_OCID")  # compartimento raíz
    subnet_id = get_env("OCI_SUBNET_OCID")
    availability_domain = get_env("OCI_AVAILABILITY_DOMAIN")
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
            send_success_email(existing, public_ip, state)
            state["finished"] = True
            save_state(state)
        sys.exit(0)

    image_id = get_latest_ubuntu_arm_image(compute_client, compartment_id)
    print(f"Usando imagen: {image_id}")

    launch_details = oci.core.models.LaunchInstanceDetails(
        availability_domain=availability_domain,
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

    print("Intentando crear la instancia...")
    response = compute_client.launch_instance(launch_details)
    instance = response.data
    print("✅ ¡Instancia creada con éxito!")
    print(f"OCID: {instance.id}")
    print(f"Estado: {instance.lifecycle_state}")

    # La IP pública puede tardar unos segundos en estar lista en la VNIC;
    # si no aparece todavía, el correo lo indica y se puede ver en la consola.
    public_ip = get_public_ip(compute_client, network_client, compartment_id, instance.id)
    send_success_email(instance, public_ip, state)
    state["finished"] = True
    save_state(state)
    sys.exit(0)


def main():
    state = load_state()

    # Primera corrida: registramos hora de inicio y enviamos correo de bienvenida.
    if state["started_at"] is None:
        state["started_at"] = now_iso()

    state["attempts"] += 1

    if not state["start_email_sent"]:
        send_start_email(state)
        state["start_email_sent"] = True

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
        send_summary_email(state)
        state["last_summary_email_at"] = now_iso()

    # Guardamos el estado ya actualizado ANTES de intentar el launch, para que
    # quede registrado el intento incluso si la corrida falla por timeout.
    save_state(state)

    config = build_config()

    for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
        try:
            run_attempt(config, state)
            return  # run_attempt termina el proceso por sí mismo (sys.exit)

        except oci.exceptions.ServiceError as e:
            if is_out_of_capacity_error(e):
                print("⏳ Sin capacidad disponible todavía. Se reintentará en la próxima corrida.")
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
