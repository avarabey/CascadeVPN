from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "reality_smoke_config", ROOT / "tests/reality_smoke_config.py"
)
smoke = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(smoke)


RFC_PRIVATE_HEX = "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
RFC_PUBLIC_HEX = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
RFC_PRIVATE = base64.urlsafe_b64encode(bytes.fromhex(RFC_PRIVATE_HEX)).rstrip(b"=").decode()
RFC_PUBLIC = base64.urlsafe_b64encode(bytes.fromhex(RFC_PUBLIC_HEX)).rstrip(b"=").decode()


def server_config(
    *,
    private_key: str = RFC_PRIVATE,
    public_key: str | None = None,
    tag: str = "in-443-tcp-2",
    sni: str = "cloud.ru",
    short_ids: list[str] | None = None,
) -> dict:
    client_defaults = {"fingerprint": "chrome", "spiderX": "/"}
    if public_key is not None:
        client_defaults["publicKey"] = public_key
    return {
        "inbounds": [
            {
                "tag": tag,
                "protocol": "vless",
                "settings": {
                    "decryption": "none",
                    "clients": [
                        {"id": "first-client-id", "flow": "xtls-rprx-vision"},
                        {"id": "second-client-id", "flow": "xtls-rprx-vision"},
                    ],
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "tcpSettings": {"header": {"type": "none"}},
                    "realitySettings": {
                        "privateKey": private_key,
                        "serverNames": [sni, "content.cloud.ru"],
                        "shortIds": short_ids if short_ids is not None else ["abcd", "1234"],
                        "settings": client_defaults,
                    },
                },
            }
        ]
    }


class RealitySmokeConfigTests(unittest.TestCase):
    def write_source(self, root: Path, value: dict) -> Path:
        path = root / "server.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def build(self, root: Path, value: dict, **kwargs) -> dict:
        return smoke.build_client_config(
            self.write_source(root, value),
            public_server=kwargs.pop("public_server", "ffknd.ru"),
            public_port=kwargs.pop("public_port", 443),
            socks_port=kwargs.pop("socks_port", 32123),
            **kwargs,
        )

    def test_x25519_derivation_matches_rfc_7748_vector(self) -> None:
        self.assertEqual(smoke.derive_x25519_public(RFC_PRIVATE), RFC_PUBLIC)

    def test_build_derives_public_key_without_subprocess_or_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.build(Path(tmp), server_config(public_key=None))
        reality = config["outbounds"][0]["streamSettings"]["realitySettings"]
        self.assertEqual(reality["publicKey"], RFC_PUBLIC)
        self.assertNotIn(RFC_PRIVATE, json.dumps(config))

    def test_build_uses_first_client_first_sni_and_first_short_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.build(Path(tmp), server_config(public_key=RFC_PUBLIC))
        outbound = config["outbounds"][0]
        user = outbound["settings"]["vnext"][0]["users"][0]
        reality = outbound["streamSettings"]["realitySettings"]
        self.assertEqual(user["id"], "first-client-id")
        self.assertEqual(user["flow"], "xtls-rprx-vision")
        self.assertEqual(reality["serverName"], "cloud.ru")
        self.assertEqual(reality["shortId"], "abcd")
        self.assertEqual(outbound["settings"]["vnext"][0]["address"], "ffknd.ru")
        self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")

    def test_empty_first_short_id_is_valid_and_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.build(Path(tmp), server_config(short_ids=["", "abcd"]))
        reality = config["outbounds"][0]["streamSettings"]["realitySettings"]
        self.assertEqual(reality["shortId"], "")

    def test_ambiguous_reality_inbounds_require_explicit_tag(self) -> None:
        config = server_config(tag="wanted")
        config["inbounds"].append(server_config(tag="other")["inbounds"][0])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(smoke.ConfigError, "exactly one"):
                self.build(Path(tmp), config)
            selected = self.build(Path(tmp), config, inbound_tag="wanted")
        self.assertEqual(
            selected["outbounds"][0]["settings"]["vnext"][0]["users"][0]["id"],
            "first-client-id",
        )

    def test_portal_sni_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(smoke.ConfigError, "collides"):
                self.build(Path(tmp), server_config(sni="ffknd.ru"))

    def test_malformed_first_short_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(smoke.ConfigError, "shortId"):
                self.build(Path(tmp), server_config(short_ids=["not-hex"]))

    def test_private_writer_requires_0700_parent_and_creates_0600_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.chmod(root, 0o700)
            destination = root / "client.json"
            smoke.write_private_json(destination, {"secret": "test-only"})
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            with self.assertRaises(smoke.ConfigError):
                smoke.write_private_json(destination, {"secret": "replacement"})

    def test_trace_validation_requires_tunnel_response_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace"
            path.write_text("ip=203.0.113.7\ncolo=CDG\ntls=TLSv1.3\n", encoding="utf-8")
            self.assertTrue(smoke.validate_cloudflare_trace(path))
            path.write_text("<html>not a trace</html>\n", encoding="utf-8")
            self.assertFalse(smoke.validate_cloudflare_trace(path))

    def test_shell_never_passes_live_secrets_to_xray_argv(self) -> None:
        shell = (ROOT / "tests/reality-e2e-smoke.sh").read_text(encoding="utf-8")
        self.assertIn('"$XRAY_BINARY" run -c "$CLIENT_CONFIG"', shell)
        self.assertNotIn("x25519 -i", shell)
        self.assertNotIn("privateKey", shell)
        self.assertIn("mktemp -d", shell)
        self.assertIn("chmod 0700", shell)
        self.assertIn("trap cleanup EXIT", shell)
        self.assertIn("ulimit -c 0", shell)
        self.assertNotIn("REALITY_SMOKE_URL", shell)
        self.assertIn('--output "$CURL_BODY" --stderr "$CURL_ERROR"', shell)


if __name__ == "__main__":
    unittest.main()
