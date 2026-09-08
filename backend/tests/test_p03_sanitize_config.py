# -*- coding: utf-8 -*-
"""P0-3 回归：厂商显示配置脱敏 sanitize_config。

覆盖 H3C Comware / 华为 VRP 密文哈希、明文口令、community 团体字、PEM 私钥，
及非敏感结构词（password aging 等）免误伤；并验证送外部 LLM 前无凭据残留。
"""
import sys
import unittest

sys.path.insert(0, "backend")

from app.services.ai_service import sanitize, sanitize_config  # noqa: E402


class TestSanitizeConfig(unittest.TestCase):
    def assert_no_secret(self, text: str, secrets):
        for s in secrets:
            self.assertNotIn(s, text, f"凭据残留: {s!r}")

    def test_h3c_password_cipher(self):
        cfg = 'local-user admin class manage\n password cipher $c$3$W3x7fQ8jL5kP2mN9==\n'
        out = sanitize_config(cfg)
        self.assert_no_secret(out, ["$c$3$W3x7fQ8jL5kP2mN9=="])
        self.assertIn("local-user admin", out)
        self.assertIn("password", out)

    def test_h3c_password_hash(self):
        cfg = ' password hash $h$6$A1B2C3D4E5F6==\n'
        out = sanitize_config(cfg)
        self.assert_no_secret(out, ["$h$6$A1B2C3D4E5F6=="])

    def test_h3c_irreversible_cipher(self):
        cfg = ' password irreversible-cipher $c$3$Qq9ZzXx2Kj==\n'
        out = sanitize_config(cfg)
        self.assert_no_secret(out, ["$c$3$Qq9ZzXx2Kj=="])

    def test_snmp_community_marker(self):
        cfg = 'snmp-agent community read cipher $c$3$c0mmUnityTok3n==\n'
        out = sanitize_config(cfg)
        self.assert_no_secret(out, ["c0mmUnityTok3n=="])

    def test_snmp_community_plain(self):
        cfg = 'snmp-agent community write aiops-public-2026\n'
        out = sanitize_config(cfg)
        self.assert_no_secret(out, ["aiops-public-2026"])

    def test_huawei_preshared_marker(self):
        cfg = 'ike peer site-a\n pre-shared-key cipher %^%#kXp0Sd9Lq2VbN7==%^%#\n'
        out = sanitize_config(cfg)
        self.assert_no_secret(out, ["%^%#kXp0Sd9Lq2VbN7==%^%#"])
        self.assertIn("ike peer site-a", out)

    def test_plain_simple_password(self):
        cfg = 'local-user admin class manage\n password simple Admin@2026!\n'
        out = sanitize_config(cfg)
        self.assert_no_secret(out, ["Admin@2026!"])

    def test_password_aging_not_masked(self):
        cfg = ' password aging 30\n password min-length 10\n'
        out = sanitize_config(cfg)
        self.assertIn("password aging 30", out)
        self.assertIn("password min-length 10", out)

    def test_pem_block_masked(self):
        cfg = (
            "interface g1/0/1\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj\n"
            "-----END RSA PRIVATE KEY-----\n"
            "return\n"
        )
        out = sanitize_config(cfg)
        self.assertNotIn("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj", out)
        self.assertIn("interface g1/0/1", out)

    def test_generic_key_value_still_covered(self):
        cfg = '{"community": "aiops123", "device": "SW1"}'
        out = sanitize_config(cfg)
        self.assert_no_secret(out, ["aiops123"])
        self.assertIn("SW1", out)

    def test_benign_config_untouched(self):
        cfg = (
            "sysname SW-CORE\n"
            "vlan 10\n"
            "interface GigabitEthernet1/0/1\n"
            " port link-type trunk\n"
            "undo shutdown\n"
        )
        self.assertEqual(sanitize_config(cfg), cfg)


class TestLegacySanitize(unittest.TestCase):
    def test_legacy_still_works(self):
        self.assertNotIn("p@ss", sanitize('password: p@ss'))
        self.assertNotIn("c0mm", sanitize('snmp community=c0mm'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
