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
  4. Если результат отличается от текущего — атомарно записывает и
     перезапускает сервис TrustTunnel.

Зависимости: только стандартная библиотека Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

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
        self.panel_user: str = p["username"]
        self.panel_pass: str = p["password"]
        self.panel_verify_tls: bool = bool(p.get("verify_tls", True))

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

        c = raw.get("compat", {})
        self.compat_file: Path = Path(c.get("file", "/etc/ttx/compat.json"))
        self.compat_strict: bool = bool(c.get("strict", False))

        self.interval: int = int(raw.get("interval_secs", 60))

    @property
    def socks_address(self) -> str:
        host = self.tt_reach_host or self.ing_listen
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

    # -- низкий уровень ----------------------------------------------------- #

    def _url(self, path: str) -> str:
        base = self.cfg.panel_path.rstrip("/")
        return f"{self.cfg.panel_url}{base}/{path.lstrip('/')}"

    def _request(self, path: str, data: dict | None = None, form: bool = False) -> dict:
        url = self._url(path)
        body, headers = None, {"Accept": "application/json"}
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

    def login(self) -> None:
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


def find_ingress(inbounds: list[dict], cfg: Config) -> dict | None:
    for ib in inbounds:
        if ib.get("remark") == cfg.ing_remark:
            return ib
    for ib in inbounds:
        if int(ib.get("port", 0)) == cfg.ing_port and \
           ib.get("protocol") in ("socks", "mixed", "http"):
            return ib
    return None


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

    if drift and cfg.ing_manage:
        LOG.warning("ingress разошёлся с эталоном (%s) — исправляю", ", ".join(drift))
        if not dry_run:
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


def write_if_changed(path: Path, content: str, dry_run: bool) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == content:
        return False
    LOG.info("конфигурация TrustTunnel изменилась → %s", path)
    if dry_run:
        sys.stdout.write(content)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if current is not None:
        shutil.copy2(path, path.with_suffix(path.suffix + ".ttx-bak"))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ttx-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)
    return True


def restart_trusttunnel(cfg: Config, dry_run: bool) -> None:
    if not cfg.tt_restart:
        LOG.info("restart_on_change=false — перезапуск пропущен")
        return
    cmd = ["systemctl", "restart", cfg.tt_service]
    LOG.info("перезапуск: %s", " ".join(cmd))
    if dry_run:
        return
    subprocess.run(cmd, check=True)


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
        out = subprocess.run([cfg.tt_binary, "--version"],
                             capture_output=True, text=True, timeout=15).stdout
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

def cmd_reconcile(cfg: Config, dry_run: bool) -> int:
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
    if write_if_changed(cfg.tt_target, rendered, dry_run):
        restart_trusttunnel(cfg, dry_run)
    else:
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
        PanelClient(cfg).login()
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
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    cfg = Config.load(args.config)

    if args.command == "reconcile":
        return cmd_reconcile(cfg, args.dry_run)
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "doctor":
        return cmd_doctor(cfg)

    LOG.info("watch: интервал %d с", cfg.interval)
    while True:
        try:
            cmd_reconcile(cfg, args.dry_run)
        except (PanelError, OSError, subprocess.SubprocessError) as exc:
            LOG.error("цикл согласования не удался: %s", exc)
        time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.exit(main())
