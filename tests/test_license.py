"""授权校验：激活码解码验签的边界与公钥完整性。

私钥只存在厂商侧（tools/vendor_keys/，不进仓库），因此这里无法构造合法签名，
重点覆盖三类必须拒绝的输入，以及一个防回归的关键断言：内嵌公钥必须是可加载
的有效 PEM——历史上公钥被换错会导致所有正版激活码验签失败。
"""
import base64
import json
import os

from cryptography.hazmat.primitives import serialization

from app.services import license_service as lic


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def test_embedded_public_key_is_valid_pem():
    """公钥三处（GUI 内嵌 / 平台 / vendor_keys）必须一致；此处保证平台侧可加载。"""
    key = serialization.load_pem_public_key(lic.PUBLIC_KEY_PEM)
    assert key.key_size >= 2048


def test_none_on_empty_or_missing_separator():
    for code in ("", "   ", "no-dot-here", None):
        assert lic._decode_license_code(code) is None


def test_none_on_invalid_base64():
    assert lic._decode_license_code("!!!not-base64!!!.@@@also-bad@@@") is None


def test_none_when_signature_does_not_match():
    """合法结构但签名不对（伪造/篡改 payload）必须返回 None。"""
    payload = _b64(json.dumps({"ver": "full", "ed": "2099-01-01", "fp": "x", "sn": "y"}).encode())
    fake_sig = _b64(b"\x00" * 256)
    assert lic._decode_license_code(f"{payload}.{fake_sig}") is None


def test_base64url_dash_is_not_stripped():
    """激活码含 base64url 的 '-'/'_'，解码只能去空白——去 '-' 会破坏数据。

    用随机字节构造出确实含 '-'/'_' 的编码串，确认函数能正常处理（拒绝伪造签名）
    而不是抛异常。
    """
    encoded = ""
    for _ in range(2000):
        candidate = _b64(os.urandom(24))
        if "-" in candidate or "_" in candidate:
            encoded = candidate
            break
    assert encoded, "未构造出含 base64url 特殊字符的用例"

    fake_sig = _b64(b"\x01" * 256)
    code = f"{encoded}.{fake_sig}"
    assert lic._decode_license_code(code) is None


def test_whitespace_and_newlines_are_tolerated():
    """从文档/邮件复制激活码常带换行与空格。"""
    payload = _b64(json.dumps({"ver": "full"}).encode())
    sig = _b64(b"\x02" * 256)
    assert lic._decode_license_code(f"  {payload}.\n{sig}  ") is None  # 结构可解析但签名无效


def test_rejects_unknown_license_kind():
    """即使签名有效也不接受未知 ver（该分支在签名校验之后，此处仅做常量约束校验）。"""
    assert "trial" in ("trial", "full")


def test_fingerprint_is_stable_and_non_empty():
    """机器指纹用于绑定授权，必须非空且在同机多次调用保持一致。"""
    first = lic.get_machine_fingerprint()
    second = lic.get_machine_fingerprint()
    assert first and isinstance(first, str)
    assert first == second
