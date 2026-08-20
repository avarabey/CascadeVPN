#!/usr/bin/env python3
"""
ttx-bridge — связующий слой между TrustTunnel endpoint и 3x-ui (Xray).

Принцип: НИ ОДИН файл исходных репозиториев не редактируется.
Мост работает только с их публичными интерфейсами:
  * 3x-ui   — REST API панели (/login, /panel/api/inbounds/*)
  * TrustTunnel — формат конфигурации vpn.toml (документированный контракт)
                  и systemd-юнит (управление через systemctl)

Что делает reconcile-цикл:
  1. Логинится в панель 3x-ui.
  2. Идемпотентно создаёт/проверяет служебный inbound "ingress"
     (socks|mixed, слушает на loopback) — точку входа трафика из TrustTunnel.
  3. Рендерит итоговый vpn.toml = базовый конфиг оператора + секция
     [forward_protocol.socks5], указывающая на этот inbound.
  4. Если результат отличается от текущего — валидирует TOML, атомарно
     записывает, перезапускает и проверяет TrustTunnel. При ошибке возвращает
     прежний файл и пытается снова запустить прежнюю конфигурацию.

Зависимости: только стандартная библиотека Python 3.11+.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import math
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("ttx-bridge")

DEFAULT_CONFIG = "/etc/ttx/bridge.json"
MARKER = "# --- managed by ttx-bridge: do not edit below this line ---"


# --------------------------------------------------------------------------- #
# Конфигурация моста
# --------------------------------------------------------------------------- #

class Config:
    def __init__(self, raw: dict):
        self.raw = raw
        p = raw.get("panel", {})
        self.panel_url: str = p.get("base_url", "http://127.0.0.1:2053").rstrip("/")
        self.panel_path: str = "/" + p.get("base_path", "/").strip("/")
        self.panel_user: str = p.get("username", "")
        self.panel_pass: str = p.get("password", "")
        self.panel_api_token: str = p.get("api_token", "")
        self.panel_verify_tls: bool = bool(p.get("verify_tls", True))

        if not self.panel_api_token and not (self.panel_user and self.panel_pass):
            raise ValueError(
                "panel: задайте api_token либо одновременно username и password")

        i = raw.get("ingress", {})
        self.ing_remark: str = i.get("remark", "TTX-Ingress")
        self.ing_listen: str = i.get("listen", "127.0.0.1")
        self.ing_port: int = int(i.get("port", 10800))
        self.ing_protocol: str = i.get("protocol", "socks")   # socks | mixed
        self.ing_udp: bool = bool(i.get("udp", True))
        self.ing_sniffing: bool = bool(i.get("sniffing", True))
        self.ing_manage: bool = bool(i.get("manage", True))

        t = raw.get("trusttunnel", {})
        self.tt_base: Path = Path(t.get("base_config", "/etc/ttx/vpn.base.toml"))
        self.tt_target: Path = Path(t.get("target_config", "/opt/trusttunnel/vpn.toml"))
        self.tt_reach_host: str = t.get("reach_host", "")     # как TT видит inbound
        self.tt_service: str = t.get("service", "trusttunnel.service")
        self.tt_binary: str = t.get("binary", "/opt/trusttunnel/trusttunnel_endpoint")
        self.tt_extended_auth: bool = bool(t.get("extended_auth", False))
        self.tt_restart: bool = bool(t.get("restart_on_change", True))
        self.tt_command_timeout: float = float(t.get("command_timeout_secs", 30))

        c = raw.get("compat", {})
        self.compat_file: Path = Path(c.get("file", "/etc/ttx/compat.json"))
        self.compat_strict: bool = bool(c.get("strict", False))

        self.interval: int = int(raw.get("interval_secs", 60))

        if not 1 <= self.ing_port <= 65535:
            raise ValueError("ingress.port должен быть в диапазоне 1..65535")
        if self.ing_protocol not in ("socks", "mixed"):
            raise ValueError("ingress.protocol должен быть socks или mixed")
        if self.interval < 1:
            raise ValueError("interval_secs должен быть положительным")
        if not math.isfinite(self.tt_command_timeout) or self.tt_command_timeout <= 0:
            raise ValueError("trusttunnel.command_timeout_secs должен быть положительным")
        if self.tt_base.resolve() == self.tt_target.resolve():
            raise ValueError("base_config и target_config должны быть разными файлами")

    @property
    def socks_address(self) -> str:
        host = self.tt_reach_host or self.ing_listen
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{self.ing_port}"

    @staticmethod
    def load(path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            return Config(json.load(fh))


# --------------------------------------------------------------------------- #
# Клиент 3x-ui (только публичный REST API панели)
# --------------------------------------------------------------------------- #

class PanelError(RuntimeError):
    pass


class PanelClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        ctx = ssl.create_default_context()
        if not cfg.panel_verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self._logged_in = False
        self._csrf_token = ""

    # -- низкий уровень ----------------------------------------------------- #

    def _url(self, path: str) -> str:
        base = self.cfg.panel_path.rstrip("/")
        return f"{self.cfg.panel_url}{base}/{path.lstrip('/')}"

    def _request(self, path: str, data: Optional[dict] = None, form: bool = False) -> dict:
        url = self._url(path)
        body, headers = None, {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.cfg.panel_api_token:
            headers["Authorization"] = f"Bearer {self.cfg.panel_api_token}"
        elif self._csrf_token:
            headers["X-CSRF-Token"] = self._csrf_token
        if data is not None:
            if form:
                body = urllib.parse.urlencode(data).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body = json.dumps(data).encode()
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if data is not None else "GET")
        try:
            with self.opener.open(req, timeout=20) as resp:
                payload = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise PanelError(f"{path}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise PanelError(f"{path}: {exc.reason}") from exc
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PanelError(f"{path}: ответ не JSON (проверьте base_path панели)") from exc

    # -- операции ----------------------------------------------------------- #

    def fetch_csrf_token(self) -> None:
        """Получает CSRF-токен новых 3x-ui; старые версии могут не иметь endpoint."""
        try:
            res = self._request("/csrf-token")
        except PanelError as exc:
            if "HTTP 404" in str(exc):
                LOG.debug("панель: /csrf-token отсутствует, используется legacy login")
                return
            raise
        token = res.get("obj")
        if isinstance(token, str) and token:
            self._csrf_token = token
            return
        raise PanelError("csrf-token: панель не вернула токен")

    def login(self) -> None:
        if self.cfg.panel_api_token:
            self._logged_in = True
            LOG.debug("панель: используется Bearer API token")
            return
        self.fetch_csrf_token()
        res = self._request("/login",
                            {"username": self.cfg.panel_user,
                             "password": self.cfg.panel_pass},
                            form=True)
        if not res.get("success"):
            raise PanelError(f"login: {res.get('msg', 'отказано')}")
        self._logged_in = True
        LOG.debug("панель: аутентификация успешна")

    def ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def list_inbounds(self) -> list[dict]:
        self.ensure_login()
        res = self._request("/panel/api/inbounds/list")
        if not res.get("success"):
            raise PanelError(f"inbounds/list: {res.get('msg')}")
        return res.get("obj") or []

    def add_inbound(self, payload: dict) -> dict:
        self.ensure_login()
        res = self._request("/panel/api/inbounds/add", payload)
        if not res.get("success"):
            raise PanelError(f"inbounds/add: {res.get('msg')}")
        return res.get("obj") or {}

    def update_inbound(self, inbound_id: int, payload: dict) -> dict:
        self.ensure_login()
        res = self._request(f"/panel/api/inbounds/update/{inbound_id}", payload)
        if not res.get("success"):
            raise PanelError(f"inbounds/update: {res.get('msg')}")
        return res.get("obj") or {}


# --------------------------------------------------------------------------- #
# Описание ingress-inbound
# --------------------------------------------------------------------------- #

def build_ingress_payload(cfg: Config) -> dict:
    settings = {
        "auth": "noauth",
        "accounts": [],
        "udp": cfg.ing_udp,
        "ip": cfg.ing_listen if cfg.ing_listen != "0.0.0.0" else "127.0.0.1",
    }
    sniffing = {
        "enabled": cfg.ing_sniffing,
        "destOverride": ["http", "tls", "quic"],
        "metadataOnly": False,
        "routeOnly": False,
    }
    return {
        "up": 0, "down": 0, "total": 0,
        "remark": cfg.ing_remark,
        "enable": True,
        "expiryTime": 0,
        "listen": cfg.ing_listen,
        "port": cfg.ing_port,
        "protocol": cfg.ing_protocol,
        "settings": json.dumps(settings),
        "streamSettings": json.dumps({"network": "tcp", "security": "none"}),
        "sniffing": json.dumps(sniffing),
        "allocate": json.dumps({"strategy": "always", "refresh": 5, "concurrency": 3}),
    }


def find_ingress(inbounds: list[dict], cfg: Config) -> Optional[dict]:
    for ib in inbounds:
        if ib.get("remark") == cfg.ing_remark:
            return ib
    return None


def json_field(value: object) -> object:
    """Нормализует поля 3x-ui: старые версии возвращают JSON-строку, новые — объект."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def contains_fields(actual: object, expected: object) -> bool:
    """Сравнивает только поля, которыми владеет мост, игнорируя новые defaults API."""
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return actual == expected
    return all(key in actual and actual[key] == value for key, value in expected.items())


def reconcile_ingress(panel: PanelClient, cfg: Config, dry_run: bool) -> dict:
    """Создаёт или чинит ingress-inbound. Возвращает актуальное описание."""
    inbounds = panel.list_inbounds()
    existing = find_ingress(inbounds, cfg)
    payload = build_ingress_payload(cfg)

    if existing is None:
        if not cfg.ing_manage:
            raise PanelError(f"inbound '{cfg.ing_remark}' не найден, а manage=false")
        LOG.info("ingress отсутствует — создаю '%s' на %s:%d",
                 cfg.ing_remark, cfg.ing_listen, cfg.ing_port)
        if dry_run:
            return payload
        return panel.add_inbound(payload)

    drift = []
    if int(existing.get("port", 0)) != cfg.ing_port:
        drift.append(f"port {existing.get('port')}→{cfg.ing_port}")
    if not existing.get("enable", False):
        drift.append("enable false→true")
    if existing.get("listen", "") != cfg.ing_listen:
        drift.append(f"listen '{existing.get('listen')}'→'{cfg.ing_listen}'")
    if existing.get("protocol") != cfg.ing_protocol:
        drift.append(f"protocol '{existing.get('protocol')}'→'{cfg.ing_protocol}'")
    if not contains_fields(json_field(existing.get("settings")),
                           json_field(payload["settings"])):
        drift.append("settings")
    if not contains_fields(json_field(existing.get("sniffing")),
                           json_field(payload["sniffing"])):
        drift.append("sniffing")

    if drift and cfg.ing_manage:
        LOG.warning("ingress разошёлся с эталоном (%s) — исправляю", ", ".join(drift))
        if not dry_run:
            # Не обнуляем накопленные счётчики при конфигурационном обновлении.
            for key in ("up", "down", "total", "expiryTime"):
                if key in existing:
                    payload[key] = existing[key]
            return panel.update_inbound(int(existing["id"]), payload)
    elif drift:
        LOG.warning("ingress разошёлся с эталоном (%s), manage=false — не трогаю",
                    ", ".join(drift))
    return existing


# --------------------------------------------------------------------------- #
# Рендер конфигурации TrustTunnel
# --------------------------------------------------------------------------- #

TOP_TABLE_RE = re.compile(r"^\s*\[\[?([A-Za-z0-9_\-.]+)")


def strip_tables(text: str, names: set[str]) -> str:
    """Удаляет из TOML-текста таблицы верхнего уровня с указанными префиксами."""
    out, skipping = [], False
    for line in text.splitlines():
        m = TOP_TABLE_RE.match(line)
        if m:
            root = m.group(1).split(".")[0]
            skipping = root in names
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def render_vpn_config(cfg: Config) -> str:
    """Итог = пользовательский базовый конфиг + управляемая секция forward_protocol."""
    base = cfg.tt_base.read_text(encoding="utf-8")
    base = base.split(MARKER)[0]
    body = strip_tables(base, {"forward_protocol"})
    managed = [
        "",
        MARKER,
        "# Секцию генерирует ttx-bridge. Правьте vpn.base.toml, а не этот файл.",
        "[forward_protocol.socks5]",
        f'address = "{cfg.socks_address}"',
        f"extended_auth = {'true' if cfg.tt_extended_auth else 'false'}",
        "",
    ]
    return body + "\n".join(managed)


def guard_loop(cfg: Config, rendered: str) -> None:
    """Защита от самозацикливания: TT не должен слушать порт своего же upstream."""
    m = re.search(r'^\s*listen_address\s*=\s*"([^"]+)"', rendered, re.M)
    if not m:
        return
    listen_port = m.group(1).rsplit(":", 1)[-1]
    if listen_port == str(cfg.ing_port):
        raise SystemExit(
            f"ОТКАЗ: listen_address TrustTunnel и порт ingress совпадают ({listen_port}) — "
            "это создаст петлю трафика")


class TrustTunnelApplyError(RuntimeError):
    """Итоговый конфиг не удалось безопасно применить."""


class FileState:
    """Снимок файла перед apply, достаточный для атомарного rollback."""

    __slots__ = ("existed", "content", "mode", "uid", "gid")

    def __init__(
            self, existed: bool, content: bytes = b"", mode: int = 0o600,
            uid: int | None = None, gid: int | None = None):
        self.existed = existed
        self.content = content
        self.mode = mode
        self.uid = uid
        self.gid = gid


def validate_vpn_config(content: str) -> None:
    """Отклоняет некорректный TOML до любого изменения target-файла."""
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise TrustTunnelApplyError(f"итоговый vpn.toml невалиден: {exc}") from exc


def capture_file_state(path: Path) -> FileState:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return FileState(existed=False)
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise TrustTunnelApplyError(
                f"target_config не является обычным файлом: {path}")
        content = fh.read()
    return FileState(
        existed=True,
        content=content,
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
        gid=info.st_gid,
    )


def _fsync_directory(path: Path) -> None:
    """Фиксирует rename/unlink в каталоге; best effort для необычных FS."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, content: bytes, state: FileState | None = None) -> None:
    """Пишет файл через fsync + os.replace, сохраняя прежние метаданные."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".ttx-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
            if state and state.existed:
                os.fchmod(fh.fileno(), state.mode)
                if state.uid is not None and state.gid is not None:
                    try:
                        os.fchown(fh.fileno(), state.uid, state.gid)
                    except PermissionError:
                        # Непривилегированный тест/контейнер обычно уже владеет
                        # файлом; режим при этом всё равно сохранён.
                        pass
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def restore_file_state(path: Path, previous: FileState) -> None:
    """Атомарно возвращает файл в состояние непосредственно перед apply."""
    if previous.existed:
        atomic_write(path, previous.content, previous)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _fsync_directory(path.parent)


@contextlib.contextmanager
def apply_lock(path: Path, timeout_secs: float):
    """Сериализует watch/timer/manual apply и защищает rollback от гонок."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".ttx-bridge.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock_file:
        deadline = time.monotonic() + timeout_secs
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise TrustTunnelApplyError(
                        f"таймаут ожидания блокировки apply: {lock_path}") from exc
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def stage_config_if_changed(path: Path, content: str, dry_run: bool) -> FileState | None:
    """Пишет изменившийся конфиг и возвращает снимок для возможного rollback."""
    validate_vpn_config(content)
    previous = capture_file_state(path)
    encoded = content.encode("utf-8")
    if previous.existed and previous.content == encoded:
        return None
    LOG.info("конфигурация TrustTunnel изменилась → %s", path)
    if dry_run:
        sys.stdout.write(content)
        return previous
    if previous.existed:
        backup = path.with_suffix(path.suffix + ".ttx-bak")
        atomic_write(backup, previous.content, previous)
    atomic_write(path, encoded, previous)
    return previous


def write_if_changed(path: Path, content: str, dry_run: bool) -> bool:
    """Совместимая булева обёртка для атомарной записи без restart-транзакции."""
    return stage_config_if_changed(path, content, dry_run) is not None


def _systemctl(cfg: Config, action: str) -> None:
    cmd = ["systemctl", action, cfg.tt_service]
    LOG.info("systemctl: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, timeout=cfg.tt_command_timeout)


def restart_trusttunnel(cfg: Config, dry_run: bool) -> bool:
    if not cfg.tt_restart:
        LOG.info("restart_on_change=false — перезапуск пропущен")
        return False
    if dry_run:
        return True
    _systemctl(cfg, "restart")
    return True


def check_trusttunnel_health(cfg: Config, dry_run: bool) -> None:
    """Ограниченная по времени post-restart проверка systemd-сервиса."""
    if dry_run:
        return
    cmd = ["systemctl", "is-active", "--quiet", cfg.tt_service]
    LOG.info("проверка после рестарта: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, timeout=cfg.tt_command_timeout)


def recover_previous_config(cfg: Config, previous: FileState) -> str | None:
    """Восстанавливает файл и пытается загрузить прежнюю конфигурацию."""
    try:
        restore_file_state(cfg.tt_target, previous)
    except (OSError, TrustTunnelApplyError) as exc:
        return f"не удалось восстановить предыдущий файл: {exc}"
    try:
        _systemctl(cfg, "restart")
        check_trusttunnel_health(cfg, dry_run=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    return None


def apply_trusttunnel_config(
        cfg: Config, content: str, dry_run: bool, restart: bool = True) -> bool:
    """Транзакционно пишет, рестартует, проверяет и при ошибке откатывает."""
    validate_vpn_config(content)
    with apply_lock(cfg.tt_target, cfg.tt_command_timeout):
        previous = stage_config_if_changed(cfg.tt_target, content, dry_run)
        if previous is None:
            return False
        if dry_run:
            return True
        if not restart:
            LOG.info("--no-restart: перезапуск TrustTunnel пропущен")
            return True
        try:
            restarted = restart_trusttunnel(cfg, dry_run=False)
            if restarted:
                check_trusttunnel_health(cfg, dry_run=False)
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.error("новый vpn.toml не прошёл применение; выполняю rollback: %s", exc)
            recovery_error = recover_previous_config(cfg, previous)
            detail = "предыдущая конфигурация восстановлена и запущена"
            if recovery_error:
                detail = ("файл предыдущей конфигурации восстановлен, но её запуск "
                          f"тоже завершился ошибкой: {recovery_error}")
            raise TrustTunnelApplyError(
                f"применение нового vpn.toml не удалось; {detail}") from exc
        return True


# --------------------------------------------------------------------------- #
# Проверка совместимости версий
# --------------------------------------------------------------------------- #

def parse_version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def check_compat(cfg: Config) -> list[str]:
    """Возвращает список проблем совместимости (пустой список = всё хорошо)."""
    problems: list[str] = []
    if not cfg.compat_file.exists():
        return problems
    compat = json.loads(cfg.compat_file.read_text(encoding="utf-8"))

    min_tt = parse_version(compat.get("trusttunnel", {}).get("min_version", "0.0.0"))
    try:
        if not cfg.tt_binary:
            return problems
        proc = subprocess.run([cfg.tt_binary, "--version"],
                              capture_output=True, text=True, timeout=15)
        out = f"{proc.stdout}\n{proc.stderr}"
        have = parse_version(out)
        if have < min_tt:
            problems.append(
                f"TrustTunnel {'.'.join(map(str, have))} < минимально проверенной "
                f"{'.'.join(map(str, min_tt))} (нужна поддержка forward_protocol.socks5)")
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(f"не удалось определить версию TrustTunnel: {exc}")
    return problems


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #

def cmd_reconcile(cfg: Config, dry_run: bool, restart: bool = True) -> int:
    for problem in check_compat(cfg):
        if cfg.compat_strict:
            LOG.error("совместимость: %s", problem)
            return 3
        LOG.warning("совместимость: %s", problem)

    panel = PanelClient(cfg)
    ingress = reconcile_ingress(panel, cfg, dry_run)
    LOG.debug("ingress: %s", {k: ingress.get(k) for k in ("id", "remark", "port")})

    rendered = render_vpn_config(cfg)
    guard_loop(cfg, rendered)
    try:
        changed = apply_trusttunnel_config(cfg, rendered, dry_run, restart=restart)
    except (TrustTunnelApplyError, OSError, subprocess.SubprocessError) as exc:
        LOG.error("конфигурация TrustTunnel не применена: %s", exc)
        return 4
    if not changed:
        LOG.info("изменений нет — состояние согласовано")
    return 0


def cmd_status(cfg: Config) -> int:
    panel = PanelClient(cfg)
    ingress = find_ingress(panel.list_inbounds(), cfg)
    print(f"панель        : {cfg.panel_url}{cfg.panel_path}")
    print(f"ingress        : "
          f"{'найден id=%s порт=%s enable=%s' % (ingress.get('id'), ingress.get('port'), ingress.get('enable')) if ingress else 'ОТСУТСТВУЕТ'}")
    print(f"upstream для TT: socks5://{cfg.socks_address}")
    print(f"конфиг TT      : {cfg.tt_target} "
          f"({'управляется' if cfg.tt_target.exists() and MARKER in cfg.tt_target.read_text() else 'НЕ управляется'})")
    for problem in check_compat(cfg):
        print(f"внимание       : {problem}")
    return 0


def cmd_doctor(cfg: Config) -> int:
    ok = True
    checks: list[tuple[str, bool, str]] = []

    checks.append(("базовый конфиг TT существует", cfg.tt_base.exists(), str(cfg.tt_base)))
    try:
        panel = PanelClient(cfg)
        panel.list_inbounds()
        checks.append(("вход в панель 3x-ui", True, cfg.panel_url))
    except PanelError as exc:
        checks.append(("вход в панель 3x-ui", False, str(exc)))

    base_text = cfg.tt_base.read_text(encoding="utf-8") if cfg.tt_base.exists() else ""
    private = re.search(r"allow_private_network_connections\s*=\s*true", base_text)
    checks.append(("allow_private_network_connections = false", not private,
                   "true открывает клиентам доступ во внутреннюю сеть сервера"))

    for name, passed, detail in checks:
        print(f"[{'OK ' if passed else 'FAIL'}] {name} — {detail}")
        ok &= passed
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ttx-bridge")
    ap.add_argument("command", choices=["reconcile", "watch", "status", "doctor"])
    ap.add_argument("-c", "--config", default=os.environ.get("TTX_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-restart", action="store_true",
                    help="записать без systemctl restart (например, в контейнере)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    cfg = Config.load(args.config)

    if args.command == "reconcile":
        return cmd_reconcile(cfg, args.dry_run, restart=not args.no_restart)
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "doctor":
        return cmd_doctor(cfg)

    LOG.info("watch: интервал %d с", cfg.interval)
    while True:
        try:
            result = cmd_reconcile(cfg, args.dry_run, restart=not args.no_restart)
            if result:
                LOG.error("цикл согласования завершился с кодом %d", result)
        except (PanelError, OSError, subprocess.SubprocessError) as exc:
            LOG.error("цикл согласования не удался: %s", exc)
        time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.exit(main())
