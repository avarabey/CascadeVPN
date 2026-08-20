"""Offline checks for the pinned TrustTunnel v1.0.33 routing contract.

Only metadata and our description of the relevant public contract are kept in
the repository.  The optional ``verify-trusttunnel-routing-source.sh`` script
can download the commit-pinned upstream file and verify its recorded digest.
"""

import json
import unittest
from pathlib import Path
from urllib.parse import urlsplit


CONTRACT_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "trusttunnel-v1.0.33-routing-contract.json"
)


def load_contract() -> dict:
    return json.loads(CONTRACT_FIXTURE.read_text(encoding="utf-8"))


def uri_path(method: str, request_target: str) -> str:
    """Return the URI path relevant to TrustTunnel's demultiplexer.

    TrustTunnel protocol CONNECT requests use authority-form (``host:port``
    or a reserved authority such as ``_udp2``) and omit ``:path``.  Rust's
    ``http::Uri`` therefore exposes an empty path.  Ordinary browser requests
    use origin-form and carry a slash-prefixed path.
    """

    if method == "CONNECT":
        if request_target.startswith("/"):
            raise ValueError("CONNECT target must use authority-form")
        return ""
    return urlsplit(request_target).path


def select_channel(path: str, contract: dict) -> str:
    """Model the path predicate and fallback recorded in our contract."""

    return (
        "ReverseProxy"
        if path.startswith(contract["path_mask"])
        else contract["fallback_channel"]
    )


class TrustTunnelRoutingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = load_contract()
        cls.upstream = fixture["upstream"]
        cls.contract = fixture["contract"]

    def test_contract_metadata_is_pinned_to_v1_0_33(self):
        self.assertEqual(self.upstream["tag"], "v1.0.33")
        self.assertEqual(
            self.upstream["commit"],
            "c33f2a6a3a9490058ca76a4274f4c96da9ec51e6",
        )
        self.assertIn(self.upstream["commit"], self.upstream["source_url"])
        self.assertEqual(len(self.upstream["source_sha256"]), 64)
        self.assertEqual(self.contract["path_mask"], "/")
        self.assertEqual(
            self.contract["reverse_proxy_predicate"],
            "Route to ReverseProxy when the request URI path starts with path_mask.",
        )
        self.assertEqual(self.contract["fallback_channel"], "Tunnel")
        self.assertEqual(
            self.contract["connect_request_target_form"], "authority-form"
        )
        self.assertEqual(self.contract["connect_uri_path"], "")

    def test_root_mask_routes_ordinary_browser_paths_to_reverse_proxy(self):
        for target in ("/", "/tools", "/rss?unread=1", "/s/abc123"):
            with self.subTest(target=target):
                path = uri_path("GET", target)
                self.assertTrue(path.startswith("/"))
                self.assertEqual(
                    select_channel(path, self.contract), "ReverseProxy"
                )

    def test_root_mask_leaves_protocol_connect_authorities_in_tunnel(self):
        # TCP, IPv4, IPv6 and the three reserved protocol authorities from
        # TrustTunnel v1.0.33 PROTOCOL.md all omit a slash-prefixed :path.
        for authority in (
            "example.com:443",
            "192.0.2.1:443",
            "[2001:db8::1]:443",
            "_udp2",
            "_icmp",
            "_check",
        ):
            with self.subTest(authority=authority):
                path = uri_path("CONNECT", authority)
                self.assertEqual(path, self.contract["connect_uri_path"])
                self.assertEqual(select_channel(path, self.contract), "Tunnel")

    def test_path_form_connect_is_rejected_by_contract_helper(self):
        with self.assertRaises(ValueError):
            uri_path("CONNECT", "/")


if __name__ == "__main__":
    unittest.main()
