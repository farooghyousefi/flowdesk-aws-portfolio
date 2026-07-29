from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3


# Project root is resolved from the repository checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Unsere bereits angelegte Konfigurationsdatei.
CONFIG_PATH = PROJECT_ROOT / "data/config/s3-storage.json"


@dataclass(frozen=True)
class S3StorageConfig:
    enabled: bool
    provider: str
    bucket: str
    region: str
    profile: str
    prefix: str
    delete_local_after_archive: bool
    cache_max_bytes: int


def load_config() -> S3StorageConfig:
    """Liest und validiert die lokale S3-Konfiguration."""

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"S3-Konfiguration nicht gefunden: {CONFIG_PATH}"
        )

    raw: dict[str, Any] = json.loads(
        CONFIG_PATH.read_text(encoding="utf-8")
    )

    required = ["bucket", "region", "profile"]
    missing = [name for name in required if not raw.get(name)]

    if missing:
        raise ValueError(
            "Fehlende S3-Konfiguration: " + ", ".join(missing)
        )

    return S3StorageConfig(
        enabled=bool(raw.get("enabled", False)),
        provider=str(raw.get("provider", "aws-s3")),
        bucket=str(raw["bucket"]),
        region=str(raw["region"]),
        profile=str(raw["profile"]),
        prefix=str(raw.get("prefix", "flowdesk")).strip("/"),
        delete_local_after_archive=bool(
            raw.get("deleteLocalAfterArchive", False)
        ),
        cache_max_bytes=int(
            raw.get("cacheMaxBytes", 5 * 1024**3)
        ),
    )


def create_s3_client(config: S3StorageConfig):
    """Erstellt einen S3-Client mit dem lokalen AWS-Profil."""

    session = boto3.Session(
        profile_name=config.profile,
        region_name=config.region,
    )

    return session.client("s3")


def connection_status() -> dict[str, Any]:
    """Prüft die Konfiguration und die Erreichbarkeit des Buckets."""

    config = load_config()

    if not config.enabled:
        return {
            "enabled": False,
            "connected": False,
            "reason": "S3 storage is disabled",
        }

    client = create_s3_client(config)
    client.head_bucket(Bucket=config.bucket)

    return {
        "enabled": True,
        "connected": True,
        "provider": config.provider,
        "bucket": config.bucket,
        "region": config.region,
        "profile": config.profile,
        "prefix": config.prefix,
        "deleteLocalAfterArchive": (
            config.delete_local_after_archive
        ),
        "cacheMaxBytes": config.cache_max_bytes,
    }


if __name__ == "__main__":
    print(json.dumps(connection_status(), indent=2))


def sha256_file(path: Path) -> str:
    """Berechnet die SHA-256-Prüfsumme einer Datei blockweise."""

    import hashlib

    digest = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def archive_file(local_path: str | Path, object_key: str) -> dict[str, Any]:
    """Lädt eine Datei sicher nach S3 und kontrolliert das Ergebnis."""

    path = Path(local_path)

    if not path.is_file():
        raise FileNotFoundError(f"Lokale Datei nicht gefunden: {path}")

    config = load_config()

    if not config.enabled:
        raise RuntimeError("S3-Speicher ist deaktiviert.")

    client = create_s3_client(config)

    file_size = path.stat().st_size
    checksum = sha256_file(path)

    normalized_key = object_key.lstrip("/")

    client.upload_file(
        str(path),
        config.bucket,
        normalized_key,
        ExtraArgs={
            "ServerSideEncryption": "AES256",
            "Metadata": {
                "sha256": checksum,
            },
        },
    )

    remote = client.head_object(
        Bucket=config.bucket,
        Key=normalized_key,
    )

    remote_size = int(remote["ContentLength"])
    remote_checksum = remote.get("Metadata", {}).get("sha256")

    if remote_size != file_size:
        raise RuntimeError(
            f"S3-Größe stimmt nicht: lokal={file_size}, remote={remote_size}"
        )

    if remote_checksum != checksum:
        raise RuntimeError(
            "S3-Prüfsumme stimmt nicht mit der lokalen Datei überein."
        )

    local_deleted = False

    if config.delete_local_after_archive:
        path.unlink()
        local_deleted = True

    return {
        "uploaded": True,
        "bucket": config.bucket,
        "objectKey": normalized_key,
        "sizeBytes": file_size,
        "sha256": checksum,
        "localDeleted": local_deleted,
    }


def restore_file(
    object_key: str,
    local_path: str | Path,
) -> dict[str, Any]:
    """Lädt ein S3-Objekt sicher zurück und prüft Größe und SHA-256."""

    config = load_config()

    if not config.enabled:
        raise RuntimeError("S3-Speicher ist deaktiviert.")

    client = create_s3_client(config)

    normalized_key = object_key.lstrip("/")
    destination = Path(local_path)
    temporary = destination.with_name(destination.name + ".part")

    destination.parent.mkdir(parents=True, exist_ok=True)

    remote = client.head_object(
        Bucket=config.bucket,
        Key=normalized_key,
    )

    expected_size = int(remote["ContentLength"])
    expected_checksum = remote.get("Metadata", {}).get("sha256")

    if not expected_checksum:
        raise RuntimeError(
            "Das S3-Objekt besitzt keine gespeicherte SHA-256-Prüfsumme."
        )

    try:
        client.download_file(
            config.bucket,
            normalized_key,
            str(temporary),
        )

        downloaded_size = temporary.stat().st_size
        downloaded_checksum = sha256_file(temporary)

        if downloaded_size != expected_size:
            raise RuntimeError(
                "Downloadgröße stimmt nicht: "
                f"erwartet={expected_size}, erhalten={downloaded_size}"
            )

        if downloaded_checksum != expected_checksum:
            raise RuntimeError(
                "SHA-256 des Downloads stimmt nicht mit S3 überein."
            )

        temporary.replace(destination)

    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return {
        "restored": True,
        "bucket": config.bucket,
        "objectKey": normalized_key,
        "localPath": str(destination),
        "sizeBytes": expected_size,
        "sha256": expected_checksum,
    }
