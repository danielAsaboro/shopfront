#!/usr/bin/env python3
"""Check a Shopfront dispute pack offline: the shop's signature, and that no file in it changed.

    python3 verify_pack.py shopfront-dispute-2026-10-21-1240.zip --kid <key ID the shop gave you>
    python3 verify_pack.py pack.zip --key shopfront-pack-key.json     # the JWK from /.well-known/shopfront-pack-key

Needs only Python 3 and the `cryptography` package (pip install cryptography); no network, no Shopfront
install. It checks:
1. manifest.sig: an EdDSA (Ed25519) or ES256 (P-256, AWS KMS) signature over manifest.sha256's exact bytes;
2. the key: its RFC 7638 thumbprint equals --kid, or it is the --key you were given. With neither, the key
   inside the zip is used, which proves only that the zip is self-consistent: say so, and exit 2;
3. every file listed in manifest.sha256 is in the zip with that SHA-256, and nothing else was added.

Exit 0: verified. 1: something failed (each problem printed). 2: consistent, but the key wasn't pinned.
This file is standalone on purpose (stdlib + cryptography), so a carrier or supplier can run it as is.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import zipfile

MANIFEST, SIG, VERIFY = "manifest.sha256", "manifest.sig", "VERIFY.txt"


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def thumbprint(jwk: dict) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url)."""
    required = {"OKP": ("crv", "kty", "x"), "EC": ("crv", "kty", "x", "y")}[jwk["kty"]]
    canon = json.dumps({k: jwk[k] for k in required}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(hashlib.sha256(canon.encode()).digest()).rstrip(b"=").decode()


def check_signature(jwk: dict, data: bytes, signature: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        if jwk.get("kty") == "OKP" and jwk.get("crv") == "Ed25519":
            ed25519.Ed25519PublicKey.from_public_bytes(unb64u(jwk["x"])).verify(signature, data)
            return True
        if jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
            if len(signature) != 64:  # JOSE ES256: r || s
                return False
            key = ec.EllipticCurvePublicNumbers(int.from_bytes(unb64u(jwk["x"]), "big"),
                                                int.from_bytes(unb64u(jwk["y"]), "big"), ec.SECP256R1()).public_key()
            der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
            key.verify(der, data, ec.ECDSA(hashes.SHA256()))
            return True
    except (InvalidSignature, ValueError, KeyError):
        return False
    return False


def verify(zip_bytes: bytes, kid: str | None = None, jwk: dict | None = None) -> tuple[list[str], dict]:
    """(problems, facts). No problems: the pack verifies."""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = z.namelist()
        manifest = z.read(MANIFEST)
        sig = json.loads(z.read(SIG))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        return [f"not a signed Shopfront pack ({exc})"], {}
    if not isinstance(sig, dict):
        return ["manifest.sig is not a signature record"], {}
    problems: list[str] = []
    key = jwk or sig.get("jwk") or {}
    try:
        key_kid = thumbprint(key)
    except (KeyError, TypeError):
        return ["no usable public key (need an Ed25519 or P-256 JWK)"], {}
    if jwk and isinstance(sig.get("jwk"), dict):
        try:
            if thumbprint(sig["jwk"]) != key_kid:
                problems.append("the pack was signed with a different key than the one you gave")
        except (KeyError, TypeError):
            pass
    if kid and kid != key_kid:
        problems.append(f"the signing key's ID is {key_kid}, not the {kid} you expected")
    if sig.get("kid") != key_kid:
        problems.append("manifest.sig names a different key ID than its key")
    try:
        signature = unb64u(str(sig.get("signature", "")))
    except ValueError:
        signature = b""
    if not check_signature(key, manifest, signature):
        problems.append("the signature over manifest.sha256 does not verify")
    listed: set[str] = set()
    for line in manifest.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        listed.add(name)
        if name not in names:
            problems.append(f"{name} is listed in the manifest but missing from the zip")
        elif hashlib.sha256(z.read(name)).hexdigest() != digest:
            problems.append(f"{name} was changed after the pack was made")
    for extra in sorted(set(names) - listed - {MANIFEST, SIG, VERIFY}):
        problems.append(f"{extra} was added after the pack was made")
    return problems, {"kid": key_kid, "alg": sig.get("alg"), "files": sorted(listed),
                      "pinned": bool(kid or jwk)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify a Shopfront dispute pack zip offline.")
    ap.add_argument("zip")
    ap.add_argument("--kid", help="the key ID the shop gave you (shown on its Set up page)")
    ap.add_argument("--key", help="a file holding the shop's public key JWK (/.well-known/shopfront-pack-key)")
    a = ap.parse_args(argv)
    try:
        data = open(a.zip, "rb").read()
        jwk = json.load(open(a.key)) if a.key else None
    except (OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1
    problems, facts = verify(data, a.kid, jwk)
    if problems:
        print("FAILED: " + a.zip)
        for p in problems:
            print("  - " + p)
        return 1
    print(f"OK: {len(facts['files'])} files match manifest.sha256, signed {facts['alg']} by key {facts['kid']}.")
    if not facts["pinned"]:
        print("  The key came from inside the zip. Compare its ID with the one the shop publishes, "
              "or run again with --kid.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
