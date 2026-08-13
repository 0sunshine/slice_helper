from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shlex
from pathlib import Path
from typing import Any

import paramiko
from cryptography.fernet import Fernet, InvalidToken

from .archive_agent import ARCHIVER_AGENT_VERSION


class ServiceManagementError(RuntimeError):
    pass


class CredentialCipher:
    def __init__(self, data_dir: Path) -> None:
        configured = os.getenv("SERVICE_CREDENTIAL_KEY", "").strip().encode("ascii")
        key_path = data_dir.resolve() / "service-credential.key"
        if configured:
            key = configured
        else:
            key_path.parent.mkdir(parents=True, exist_ok=True)
            if key_path.exists():
                key = key_path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                temporary = key_path.with_suffix(".key.partial")
                temporary.write_bytes(key + b"\n")
                os.chmod(temporary, 0o600)
                os.replace(temporary, key_path)
                os.chmod(key_path, 0o600)
        try:
            self.fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ServiceManagementError("SERVICE_CREDENTIAL_KEY 不是有效的 Fernet 密钥") from exc

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii") if value else ""

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise ServiceManagementError("SSH 凭据无法解密，请重新填写密码") from exc


def _catalog_http_base(instance: dict[str, Any]) -> str:
    configured = str(instance.get("archive_http_base") or "").rstrip("/")
    if configured:
        return configured
    catalog = str(instance.get("archive_catalog_url") or "")
    if catalog.endswith("/catalog.json"):
        return catalog[: -len("/catalog.json")]
    return catalog.rsplit("/", 1)[0].rstrip("/")


def render_agent_config(instance: dict[str, Any], agent_root: str, public_base_url: str) -> str:
    source_id = str(instance["source_id"])
    agent_root = agent_root.rstrip("/")
    remote_root = str(instance.get("archive_remote_root") or "").rstrip("/")
    if not remote_root or remote_root == "/":
        remote_root = f"/mpeg/mpeg2/codex/archive/sources/{source_id}"
    values = {
        "source_id": source_id,
        "source_name": str(instance["name"]),
        "islice_base_url": str(instance["base_url"]),
        "slice_helper_base_url": public_base_url.rstrip("/"),
        "islice_database": str(instance["islice_database_path"]),
        "storage_root": str(instance["storage_root"]),
        "state_database": f"{agent_root}/archive.db",
        "manifest_root": f"{agent_root}/manifests",
        "reset_backup_root": f"{agent_root}/reset-backups",
        "lock_path": f"{agent_root}/archive.lock",
        "remote_host": str(instance["archive_remote_host"]),
        "remote_user": str(instance["archive_remote_user"]),
        "remote_root": remote_root,
        "remote_http_base": _catalog_http_base(instance),
        "ssh_key": str(instance["archive_ssh_key"]),
        "known_hosts": str(instance["archive_known_hosts"]),
    }
    if not values["remote_http_base"]:
        raise ServiceManagementError("必须配置归档 HTTP 或 catalog 地址")
    lines = ["[archiver]"]
    lines.extend(f"{name} = {value}" for name, value in values.items())
    lines.extend(
        [
            "delete_delay_hours = 24",
            "retry_delay_minutes = 30",
            "max_tasks_per_run = 4",
            "command_timeout_seconds = 21600",
            "http_timeout_seconds = 30",
            "",
        ]
    )
    return "\n".join(lines)


class ServiceManager:
    def __init__(self, database, data_dir: Path, public_base_url: str, package_dir: Path):
        self.database = database
        self.cipher = CredentialCipher(data_dir)
        self.public_base_url = public_base_url
        self.agent_source = package_dir / "archive_agent.py"
        self.monitor_task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.ssh_semaphore = asyncio.Semaphore(5)

    async def start(self) -> None:
        self.stop_event.clear()
        self.monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        self.stop_event.set()
        if self.monitor_task:
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
            self.monitor_task = None

    async def deploy(self, instance_id: str) -> dict[str, Any]:
        instance = await self.database.get_islice_instance_secret(instance_id)
        if instance is None:
            raise ServiceManagementError("服务不存在")
        await self.database.update_agent_health(instance_id, status="deploying", error="")
        try:
            async with self.ssh_semaphore:
                result = await asyncio.to_thread(self._deploy_sync, instance)
            await self.database.update_agent_health(
                instance_id,
                status="online",
                version=str(result["version"]),
                error="",
                host_key=str(result["hostKey"]),
                deployed=True,
            )
            return result
        except Exception as exc:
            await self.database.update_agent_health(
                instance_id, status="error", error=str(exc)[:2000]
            )
            if isinstance(exc, ServiceManagementError):
                raise
            raise ServiceManagementError(str(exc)) from exc

    async def probe(self, instance_id: str) -> dict[str, Any]:
        instance = await self.database.get_islice_instance_secret(instance_id)
        if instance is None:
            raise ServiceManagementError("服务不存在")
        try:
            async with self.ssh_semaphore:
                result = await asyncio.to_thread(self._probe_sync, instance)
            await self.database.update_agent_health(
                instance_id,
                status="online" if result["online"] else "offline",
                version=str(result.get("version") or ""),
                error=str(result.get("error") or ""),
                host_key=str(result.get("hostKey") or ""),
            )
            return result
        except Exception as exc:
            await self.database.update_agent_health(
                instance_id, status="offline", error=str(exc)[:2000]
            )
            return {"online": False, "error": str(exc)}

    async def _monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            instances = await self.database.list_islice_instances_secret()
            checks = [
                self.probe(str(instance["id"]))
                for instance in instances
                if instance.get("ssh_host") and instance.get("agent_install_path")
            ]
            if checks:
                await asyncio.gather(*checks, return_exceptions=True)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass

    def _connect(self, instance: dict[str, Any]) -> tuple[paramiko.SSHClient, str]:
        host = str(instance.get("ssh_host") or "")
        username = str(instance.get("ssh_username") or "")
        if not host or not username:
            raise ServiceManagementError("SSH 主机和账号不能为空")
        password = self.cipher.decrypt(str(instance.get("ssh_password_encrypted") or ""))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=int(instance.get("ssh_port") or 22),
                username=username,
                password=password or None,
                timeout=10,
                banner_timeout=10,
                auth_timeout=10,
                look_for_keys=not bool(password),
                allow_agent=not bool(password),
            )
        except Exception as exc:
            client.close()
            raise ServiceManagementError(f"SSH 连接失败：{exc}") from exc
        key = client.get_transport().get_remote_server_key()
        fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
        expected = str(instance.get("ssh_host_key_sha256") or "")
        if expected and expected != fingerprint:
            client.close()
            raise ServiceManagementError("SSH 主机密钥发生变化，已拒绝连接")
        return client, fingerprint

    @staticmethod
    def _run(client: paramiko.SSHClient, command: str, timeout: float = 30) -> str:
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode("utf-8", "replace")
        error = stderr.read().decode("utf-8", "replace")
        status = stdout.channel.recv_exit_status()
        if status:
            raise ServiceManagementError((error or output or f"远程命令退出 {status}").strip())
        return output.strip()

    def _deploy_sync(self, instance: dict[str, Any]) -> dict[str, Any]:
        client, fingerprint = self._connect(instance)
        try:
            root = str(instance.get("agent_install_path") or "").rstrip("/")
            if not root.startswith("/"):
                raise ServiceManagementError("必须填写绝对的代理安装目录")
            self._run(client, f"mkdir -p {shlex.quote(root)} {shlex.quote(root + '/manifests')}")
            sftp = client.open_sftp()
            script = f"{root}/islice_archiver.py"
            config = f"{root}/islice-archiver.ini"
            with sftp.file(script + ".partial", "wb") as target:
                target.write(self.agent_source.read_bytes())
            with sftp.file(config + ".partial", "wb") as target:
                target.write(
                    render_agent_config(instance, root, self.public_base_url).encode("utf-8")
                )
            sftp.chmod(script + ".partial", 0o700)
            sftp.chmod(config + ".partial", 0o600)
            sftp.close()
            self._run(
                client,
                f"mv {shlex.quote(script + '.partial')} {shlex.quote(script)} && "
                f"mv {shlex.quote(config + '.partial')} {shlex.quote(config)}",
            )
            checks = (
                f"version=$(python3 {shlex.quote(script)} --version) || exit $?; "
                "command -v rsync >/dev/null || { echo '未安装 rsync' >&2; exit 3; }; "
                f"test -r {shlex.quote(str(instance['islice_database_path']))} || "
                "{ echo 'iSlice 数据库不存在或当前 SSH 账号不可读' >&2; exit 3; }; "
                f"test -d {shlex.quote(str(instance['storage_root']))} || "
                "{ echo 'iSlice storage 目录不存在或当前 SSH 账号不可访问' >&2; exit 3; }; "
                f"test -r {shlex.quote(str(instance['archive_ssh_key']))} || "
                "{ echo '归档 SSH 私钥不存在或当前 SSH 账号不可读' >&2; exit 3; }; "
                f"test -r {shlex.quote(str(instance['archive_known_hosts']))} || "
                "{ echo 'known_hosts 不存在或当前 SSH 账号不可读' >&2; exit 3; }; "
                f"ssh -i {shlex.quote(str(instance['archive_ssh_key']))} -o BatchMode=yes "
                f"-o StrictHostKeyChecking=yes -o ConnectTimeout=10 "
                f"-o UserKnownHostsFile={shlex.quote(str(instance['archive_known_hosts']))} "
                f"{shlex.quote(str(instance['archive_remote_user']) + '@' + str(instance['archive_remote_host']))} true "
                "|| { echo '无法使用配置的私钥连接远端归档服务器' >&2; exit 3; }; "
                "printf '%s\\n' \"$version\""
            )
            version = self._run(client, checks)
            self._run(
                client,
                f"python3 {shlex.quote(script)} --config {shlex.quote(config)} publish-catalog",
                timeout=60,
            )
            pid_file = f"{root}/agent.pid"
            log_file = f"{root}/agent.log"
            stop = (
                f"if test -f {shlex.quote(pid_file)}; then pid=$(cat {shlex.quote(pid_file)}); "
                f"case \"$pid\" in *[!0-9]*|'') ;; *) "
                f"if kill -0 \"$pid\" 2>/dev/null && tr '\\0' ' ' < /proc/\"$pid\"/cmdline | grep -F {shlex.quote(script)} >/dev/null; "
                f"then kill \"$pid\"; for wait_step in 1 2 3 4 5 6 7 8 9 10; do "
                f"kill -0 \"$pid\" 2>/dev/null || break; sleep 0.2; done; "
                f"if kill -0 \"$pid\" 2>/dev/null; then echo '旧代理进程未退出' >&2; exit 4; fi; fi ;; esac; fi"
            )
            start = (
                f"if test -f {shlex.quote(log_file)} && test $(wc -c < {shlex.quote(log_file)}) -gt 52428800; "
                f"then mv {shlex.quote(log_file)} {shlex.quote(log_file + '.1')}; fi; "
                f"nohup python3 {shlex.quote(script)} --config {shlex.quote(config)} "
                f"run-forever --interval 300 >> {shlex.quote(log_file)} 2>&1 < /dev/null & "
                f"echo $! > {shlex.quote(pid_file)}; sleep 1; kill -0 $(cat {shlex.quote(pid_file)})"
            )
            self._run(client, f"{stop}; {start}")
            return {
                "online": True,
                "version": version.splitlines()[0] if version else ARCHIVER_AGENT_VERSION,
                "hostKey": fingerprint,
                "installPath": root,
                "pidFile": pid_file,
                "logFile": log_file,
            }
        finally:
            client.close()

    def _probe_sync(self, instance: dict[str, Any]) -> dict[str, Any]:
        client, fingerprint = self._connect(instance)
        try:
            root = str(instance.get("agent_install_path") or "").rstrip("/")
            if not root.startswith("/"):
                return {"online": False, "error": "未配置代理安装目录", "hostKey": fingerprint}
            script = f"{root}/islice_archiver.py"
            pid_file = f"{root}/agent.pid"
            command = (
                f"test -r {shlex.quote(pid_file)} && pid=$(cat {shlex.quote(pid_file)}); "
                f"case \"$pid\" in *[!0-9]*|'') exit 3 ;; esac; "
                f"kill -0 \"$pid\" && tr '\\0' ' ' < /proc/\"$pid\"/cmdline | "
                f"grep -F {shlex.quote(script)} >/dev/null && python3 {shlex.quote(script)} --version"
            )
            try:
                version = self._run(client, command)
                return {"online": True, "version": version.splitlines()[0], "hostKey": fingerprint}
            except ServiceManagementError as exc:
                return {"online": False, "version": "", "hostKey": fingerprint, "error": str(exc)}
        finally:
            client.close()
