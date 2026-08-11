from __future__ import annotations

from pathlib import Path

import pytest

from slice_helper.database import Database
from slice_helper.service_manager import CredentialCipher, render_agent_config


def service_record(cipher: CredentialCipher) -> dict:
    return {
        "source_id": "islice-128",
        "name": "iSlice 128",
        "base_url": "http://192.168.104.128:8000",
        "archive_catalog_url": "http://archive.test/sources/islice-128/catalog.json",
        "schedulable": True,
        "ssh_host": "192.168.104.128",
        "ssh_port": 22,
        "ssh_username": "codex",
        "ssh_password_encrypted": cipher.encrypt("secret-password"),
        "agent_install_path": "/home/codex/custom-archiver",
        "islice_database_path": "/srv/islice/data/tasks.db",
        "storage_root": "/srv/islice/storage",
        "archive_remote_host": "192.168.6.200",
        "archive_remote_user": "archive",
        "archive_remote_root": "/archive/sources/islice-128",
        "archive_http_base": "http://archive.test/sources/islice-128",
        "archive_ssh_key": "/home/codex/.ssh/archive_ed25519",
        "archive_known_hosts": "/home/codex/.ssh/known_hosts",
    }


def test_service_credentials_are_encrypted_and_key_is_persistent(tmp_path: Path) -> None:
    first = CredentialCipher(tmp_path)
    encrypted = first.encrypt("ssh-password")

    assert encrypted != "ssh-password"
    assert first.decrypt(encrypted) == "ssh-password"
    assert (tmp_path / "service-credential.key").is_file()
    second = CredentialCipher(tmp_path)
    assert second.decrypt(encrypted) == "ssh-password"


def test_agent_config_uses_page_configured_install_directory(tmp_path: Path) -> None:
    cipher = CredentialCipher(tmp_path)
    config = render_agent_config(
        service_record(cipher),
        "/home/codex/custom-archiver",
        "http://helper.test",
    )

    assert "state_database = /home/codex/custom-archiver/archive.db" in config
    assert "manifest_root = /home/codex/custom-archiver/manifests" in config
    assert "islice_database = /srv/islice/data/tasks.db" in config
    assert "storage_root = /srv/islice/storage" in config
    assert "remote_root = /archive/sources/islice-128" in config
    assert "systemd" not in config


@pytest.mark.asyncio
async def test_database_never_returns_encrypted_ssh_password(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    cipher = CredentialCipher(tmp_path / "secrets")
    created = await database.create_islice_instance(service_record(cipher))

    assert created["has_ssh_password"] is True
    assert "ssh_password_encrypted" not in created
    listed = (await database.list_islice_instances())[0]
    assert listed["has_ssh_password"] is True
    assert "ssh_password_encrypted" not in listed
    secret = await database.get_islice_instance_secret(created["id"])
    assert cipher.decrypt(secret["ssh_password_encrypted"]) == "secret-password"
