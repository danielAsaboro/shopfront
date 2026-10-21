"""Signed dispute packs: anyone holding the zip can check, offline, that the shop's Shopfront made it and that
nothing in it changed since.

The zip already carries `manifest.sha256` (the SHA-256 of every other file, app/dispute.py). This module signs
that manifest's exact bytes and adds three files next to it:

- `manifest.sig`  JSON: the algorithm, the key ID (the RFC 7638 JWK thumbprint), the public key as a JWK, and
                  the signature (base64url; for ES256 the JOSE form, r || s, 64 bytes).
- `VERIFY.txt`    the one-line command that checks it: `python3 verify_pack.py <zip> --kid <key ID>`.

The key ID is what a recipient compares against what the shop publishes: /.well-known/shopfront-pack-key
(the public JWK) and the /setup page. scripts/verify_pack.py verifies with only the Python standard library and
`cryptography`, no network.

Two signers:
- local (default): an Ed25519 key generated on first use, kept in the data folder 0600 (`pack-signing-key.pem`).
- kms (`SHOPFRONT_SIGNING=kms` + `SHOPFRONT_SIGNING_KEY_ARN`): an AWS KMS asymmetric key, ECC_NIST_P256 with
  ECDSA_SHA_256 (KMS Sign, MessageType RAW), so the private key never leaves KMS. infra/template.yaml makes
  the key with a key policy that lets only the app's principal Sign.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.files import write_private

MANIFEST = "manifest.sha256"
SIG = "manifest.sig"
VERIFY = "VERIFY.txt"
KEY_PATH = "/.well-known/shopfront-pack-key"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def thumbprint(jwk: dict) -> str:
    """RFC 7638: SHA-256 over the required members, sorted, no whitespace; base64url."""
    required = {"OKP": ("crv", "kty", "x"), "EC": ("crv", "kty", "x", "y")}[jwk["kty"]]
    canon = json.dumps({k: jwk[k] for k in required}, separators=(",", ":"), sort_keys=True)
    return b64u(hashlib.sha256(canon.encode()).digest())


def public_jwk(key) -> dict:
    """A public key (Ed25519 or P-256) as a JWK with kid, alg and use."""
    if isinstance(key, ed25519.Ed25519PublicKey):
        jwk = {"kty": "OKP", "crv": "Ed25519",
               "x": b64u(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))}
        alg = "EdDSA"
    elif isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == "secp256r1":
        n = key.public_numbers()
        jwk = {"kty": "EC", "crv": "P-256", "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big"))}
        alg = "ES256"
    else:
        raise ValueError("pack keys are Ed25519 or P-256")
    return {**jwk, "kid": thumbprint(jwk), "alg": alg, "use": "sig"}


class LocalSigner:
    """Ed25519, the private key in the data folder (0600), made the first time a pack is signed."""

    name = "local"

    def __init__(self, key_file: Path):
        self.key_file = key_file
        self._lock = threading.Lock()
        self._key: ed25519.Ed25519PrivateKey | None = None

    def _private(self) -> ed25519.Ed25519PrivateKey:
        with self._lock:
            if self._key is None:
                if not self.key_file.exists():
                    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
                        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                    write_private(self.key_file, pem)
                key = serialization.load_pem_private_key(self.key_file.read_bytes(), password=None)
                if not isinstance(key, ed25519.Ed25519PrivateKey):
                    raise ValueError(f"{self.key_file.name} is not an Ed25519 key")
                self._key = key
            return self._key

    def jwk(self) -> dict:
        return public_jwk(self._private().public_key())

    def sign(self, data: bytes) -> bytes:
        return self._private().sign(data)


class MemorySigner(LocalSigner):
    """An Ed25519 key that lives only in memory (the public demo, which writes nothing to disk)."""

    name = "demo"

    def __init__(self):
        super().__init__(Path("/nonexistent/public-demo-key.pem"))
        self._key = ed25519.Ed25519PrivateKey.generate()


class KmsSigner:
    """An AWS KMS asymmetric key (KeySpec ECC_NIST_P256, KeyUsage SIGN_VERIFY): KMS Sign with
    SigningAlgorithm ECDSA_SHA_256 and MessageType RAW (KMS hashes the message). KMS returns a DER signature;
    the pack carries it as JOSE ES256 (r || s)."""

    name = "kms"
    ALGORITHM = "ECDSA_SHA_256"

    def __init__(self, key_id: str, client):
        self.key_id = key_id
        self.client = client
        self._jwk: dict | None = None
        self._lock = threading.Lock()

    def jwk(self) -> dict:
        with self._lock:
            if self._jwk is None:
                resp = self.client.get_public_key(KeyId=self.key_id)
                spec = resp.get("KeySpec") or resp.get("CustomerMasterKeySpec")  # the second is KMS's older name
                if spec != "ECC_NIST_P256" or self.ALGORITHM not in resp.get("SigningAlgorithms", []):
                    raise ValueError("the KMS key must be ECC_NIST_P256 with ECDSA_SHA_256")
                self._jwk = public_jwk(serialization.load_der_public_key(resp["PublicKey"]))
            return self._jwk

    def sign(self, data: bytes) -> bytes:
        if len(data) > 4096:  # KMS Sign takes a RAW message of up to 4096 bytes; hash longer manifests ourselves
            raise ValueError("manifest too long for KMS RAW signing")
        resp = self.client.sign(KeyId=self.key_id, Message=data, MessageType="RAW", SigningAlgorithm=self.ALGORITHM)
        r, s = decode_dss_signature(resp["Signature"])
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def make_signer(settings, data_dir: Path):
    """SHOPFRONT_SIGNING=kms with SHOPFRONT_SIGNING_KEY_ARN: KMS; otherwise the local Ed25519 key."""
    if settings.signing == "kms":
        if not settings.signing_key_arn:
            raise ValueError("SHOPFRONT_SIGNING=kms needs SHOPFRONT_SIGNING_KEY_ARN")
        import boto3
        region = settings.signing_key_arn.split(":")[3] if settings.signing_key_arn.startswith("arn:") \
            else settings.aws_region
        return KmsSigner(settings.signing_key_arn, boto3.client("kms", region_name=region))
    if settings.signing not in ("", "local"):
        raise ValueError(f"SHOPFRONT_SIGNING must be local or kms, got {settings.signing!r}")
    return LocalSigner(data_dir / "pack-signing-key.pem")


def verify_line(zip_name: str, kid: str) -> str:
    return f"python3 verify_pack.py {zip_name} --kid {kid}"


def sign_zip(zip_bytes: bytes, signer, zip_name: str = "pack.zip") -> bytes:
    """The pack zip with manifest.sig and VERIFY.txt added. The manifest itself is untouched."""
    src = zipfile.ZipFile(io.BytesIO(zip_bytes))
    manifest = src.read(MANIFEST)
    jwk = signer.jwk()
    sig = {"signed_file": MANIFEST, "alg": jwk["alg"], "kid": jwk["kid"], "jwk": jwk,
           "signature": b64u(signer.sign(manifest)), "signer": signer.name, "key_url": KEY_PATH}
    verify = (f"{verify_line(zip_name, jwk['kid'])}\n\n"
              f"verify_pack.py is scripts/verify_pack.py in Shopfront's source; it needs Python 3 and the "
              f"`cryptography` package, and no network. It checks {SIG} (an {jwk['alg']} signature over "
              f"{MANIFEST}) and every file's SHA-256 in {MANIFEST}.\n"
              f"Compare the key ID above with the one the shop publishes (its Shopfront Set up page, and "
              f"{KEY_PATH}): this file travels inside the zip, so it can't vouch for itself.\n")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            if info.filename in (SIG, VERIFY):
                continue
            z.writestr(info, src.read(info.filename), zipfile.ZIP_DEFLATED)
        for name, data in ((SIG, json.dumps(sig, indent=1).encode()), (VERIFY, verify.encode())):
            z.writestr(zipfile.ZipInfo(name, ZIP_TIME), data, zipfile.ZIP_DEFLATED)
    return out.getvalue()
