"""Signed dispute packs (app/pack_signing.py) and the offline checker (scripts/verify_pack.py)."""

import io
import json
import os
import stat
import zipfile

import boto3
import pytest
from botocore.stub import Stubber
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

import app.main as main
from app import pack_signing as ps
from scripts import verify_pack as vp
from tests import aws_fakes

aws = aws_fakes.aws  # pytest fixture

KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab"


def unsigned_zip(files=None) -> bytes:
    """A pack zip the way app/dispute.py makes one: files plus manifest.sha256."""
    import hashlib
    files = files or [("README.txt", b"readme"), ("pack.txt", b"the pack"), ("pack.json", b"{}")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files:
            z.writestr(n, d)
        z.writestr("manifest.sha256", "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in files))
    return buf.getvalue()


def rewrite(blob: bytes, name: str, data: bytes | None, add: tuple[str, bytes] | None = None) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(blob))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n in src.namelist():
            if n == name and data is None:
                continue
            z.writestr(n, data if n == name else src.read(n))
        if add:
            z.writestr(*add)
    return out.getvalue()


# --- local Ed25519 ----------------------------------------------------------------------------------------
def test_local_key_is_made_once_0600_and_signs_a_verifiable_zip(tmp_path):
    signer = ps.LocalSigner(tmp_path / "pack-signing-key.pem")
    assert not signer.key_file.exists()
    blob = ps.sign_zip(unsigned_zip(), signer, "p.zip")
    assert stat.S_IMODE(os.stat(signer.key_file).st_mode) == 0o600
    jwk = signer.jwk()
    assert jwk["kty"] == "OKP" and jwk["crv"] == "Ed25519" and jwk["alg"] == "EdDSA" and "d" not in jwk
    assert ps.LocalSigner(signer.key_file).jwk() == jwk  # the same key on the next start

    z = zipfile.ZipFile(io.BytesIO(blob))
    assert {"manifest.sha256", "manifest.sig", "VERIFY.txt"} <= set(z.namelist())
    assert z.read("VERIFY.txt").decode().splitlines()[0] == f"python3 verify_pack.py p.zip --kid {jwk['kid']}"
    problems, facts = vp.verify(blob, kid=jwk["kid"])
    assert problems == [] and facts["alg"] == "EdDSA"
    assert vp.verify(blob, jwk=jwk)[0] == []
    assert ps.sign_zip(unsigned_zip(), signer) == ps.sign_zip(unsigned_zip(), signer)  # Ed25519 is deterministic


def test_thumbprint_is_rfc7638():
    # RFC 8037 appendix A.3: the RFC 7638 thumbprint of the RFC 8032 test key
    okp = {"kty": "OKP", "crv": "Ed25519", "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo", "d": "ignored"}
    assert ps.thumbprint(okp) == vp.thumbprint(okp) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"


@pytest.mark.parametrize("attack", ["edit", "remove", "add", "resign", "wrong_kid"])
def test_tampering_is_caught(tmp_path, attack):
    signer = ps.LocalSigner(tmp_path / "k.pem")
    blob = ps.sign_zip(unsigned_zip(), signer)
    kid = signer.jwk()["kid"]
    if attack == "edit":
        bad, want = rewrite(blob, "pack.txt", b"the pack, edited"), "pack.txt was changed"
    elif attack == "remove":
        bad, want = rewrite(blob, "pack.json", None), "pack.json is listed in the manifest but missing"
    elif attack == "add":
        bad, want = rewrite(blob, "x", None, ("extra.jpg", b"x")), "extra.jpg was added"
    elif attack == "resign":  # someone rebuilds the manifest and signs it with their own key
        other = ps.LocalSigner(tmp_path / "other.pem")
        edited = unsigned_zip([("README.txt", b"readme"), ("pack.txt", b"forged"), ("pack.json", b"{}")])
        bad, want = ps.sign_zip(edited, other), "not the"
    else:
        bad, want, kid = blob, "not the", "someone-elses-key-id"
    problems, _ = vp.verify(bad, kid=kid)
    assert problems and any(want in p for p in problems), problems


def test_unpinned_key_only_proves_self_consistency(tmp_path, capsys):
    signer = ps.LocalSigner(tmp_path / "k.pem")
    path = tmp_path / "p.zip"
    path.write_bytes(ps.sign_zip(unsigned_zip(), signer))
    assert vp.main([str(path)]) == 2
    assert "Compare its ID" in capsys.readouterr().out
    assert vp.main([str(path), "--kid", signer.jwk()["kid"]]) == 0
    keyfile = tmp_path / "key.json"
    keyfile.write_text(json.dumps(signer.jwk()))
    assert vp.main([str(path), "--key", str(keyfile)]) == 0
    path.write_bytes(rewrite(path.read_bytes(), "pack.txt", b"edited"))
    assert vp.main([str(path), "--kid", signer.jwk()["kid"]]) == 1


# --- AWS KMS (ECC_NIST_P256, ECDSA_SHA_256), with botocore's Stubber ----------------------------------------
def kms_stub(manifest: bytes):
    private = ec.generate_private_key(ec.SECP256R1())
    der_pub = private.public_key().public_bytes(serialization.Encoding.DER,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)
    client = boto3.client("kms", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    stub = Stubber(client)
    stub.add_response("get_public_key", {"KeyId": KEY_ARN, "PublicKey": der_pub, "KeySpec": "ECC_NIST_P256",
                                         "KeyUsage": "SIGN_VERIFY", "SigningAlgorithms": ["ECDSA_SHA_256"]},
                      {"KeyId": KEY_ARN})
    stub.add_response("sign", {"KeyId": KEY_ARN, "Signature": private.sign(manifest, ec.ECDSA(hashes.SHA256())),
                               "SigningAlgorithm": "ECDSA_SHA_256"},
                      {"KeyId": KEY_ARN, "Message": manifest, "MessageType": "RAW", "SigningAlgorithm": "ECDSA_SHA_256"})
    return client, stub


def test_kms_signer_signs_es256_that_verifies_offline():
    blob = unsigned_zip()
    manifest = zipfile.ZipFile(io.BytesIO(blob)).read("manifest.sha256")
    client, stub = kms_stub(manifest)
    with stub:
        signer = ps.KmsSigner(KEY_ARN, client)
        signed = ps.sign_zip(blob, signer)
        stub.assert_no_pending_responses()
    jwk = signer.jwk()  # cached: no second GetPublicKey
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256" and jwk["alg"] == "ES256"
    sig = json.loads(zipfile.ZipFile(io.BytesIO(signed)).read("manifest.sig"))
    assert sig["signer"] == "kms" and len(vp.unb64u(sig["signature"])) == 64  # JOSE r || s, not DER
    assert vp.verify(signed, kid=jwk["kid"])[0] == []


def test_kms_key_of_the_wrong_kind_is_refused():
    client = boto3.client("kms", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    stub = Stubber(client)
    stub.add_response("get_public_key", {"KeyId": KEY_ARN, "PublicKey": b"x", "KeySpec": "RSA_2048",
                                         "SigningAlgorithms": ["RSASSA_PSS_SHA_256"]}, {"KeyId": KEY_ARN})
    with stub, pytest.raises(ValueError, match="ECC_NIST_P256"):
        ps.KmsSigner(KEY_ARN, client).jwk()


def test_make_signer_picks_kms_only_with_a_key(tmp_path):
    from dataclasses import replace
    assert isinstance(ps.make_signer(replace(main.settings, signing="local"), tmp_path), ps.LocalSigner)
    with pytest.raises(ValueError):
        ps.make_signer(replace(main.settings, signing="kms", signing_key_arn=""), tmp_path)
    kms = ps.make_signer(replace(main.settings, signing="kms", signing_key_arn=KEY_ARN), tmp_path)
    assert isinstance(kms, ps.KmsSigner) and kms.client.meta.region_name == "us-east-1"
    with pytest.raises(ValueError):
        ps.make_signer(replace(main.settings, signing="gpg"), tmp_path)


# --- the app ---------------------------------------------------------------------------------------------
def test_pack_zip_from_the_app_is_signed_and_the_key_is_public(monkeypatch):
    from tests.test_dispute_pack import DAY, confirmed_lunch
    monkeypatch.setattr(main, "_visits", lambda d: confirmed_lunch())
    owner = TestClient(main.app)
    r = owner.get("/dispute.zip", params={"date": DAY.isoformat(), "at": "12:40", "claim": "business_closed"})
    kid = r.headers["X-Pack-Key-Id"]
    assert vp.verify(r.content, kid=kid)[0] == []
    verify_txt = zipfile.ZipFile(io.BytesIO(r.content)).read("VERIFY.txt").decode()
    assert "shopfront-dispute-2026-10-14-1240.zip --kid " + kid in verify_txt

    outsider = TestClient(main.app, client=("203.0.113.9", 5000))
    jwk = outsider.get("/.well-known/shopfront-pack-key").json()  # public, like the webhook
    assert jwk["kid"] == kid and "d" not in jwk
    assert vp.verify(r.content, jwk=jwk)[0] == []
    setup = owner.get("/setup").text
    assert kid in setup and "/.well-known/shopfront-pack-key" in setup


def test_key_endpoint_says_when_kms_is_unreachable(monkeypatch):
    class Broken:
        name = "kms"

        def jwk(self):
            raise RuntimeError("no route to KMS")
    monkeypatch.setattr(main, "pack_signer", Broken())
    r = TestClient(main.app).get("/.well-known/shopfront-pack-key")
    assert r.status_code == 503 and r.json()["error"] == "KEY_UNAVAILABLE"
    assert TestClient(main.app).get("/setup").status_code == 200  # the page still renders, without the key line


def test_signing_key_never_leaves_the_data_folder(tmp_path):
    signer = ps.LocalSigner(tmp_path / "k.pem")
    blob = ps.sign_zip(unsigned_zip(), signer)
    private = signer.key_file.read_bytes()
    assert b"PRIVATE KEY" in private
    z = zipfile.ZipFile(io.BytesIO(blob))
    assert all(b"PRIVATE" not in z.read(n) for n in z.namelist())


# --- infra/template.yaml: the KMS key -----------------------------------------------------------------------
def test_template_key_lets_only_the_app_sign():
    template = aws_fakes.template
    t = template()
    key = t["Resources"]["PackSigningKey"]
    assert key["Condition"] == "HasAppPrincipal"
    props = key["Properties"]
    assert props["KeySpec"] == "ECC_NIST_P256" and props["KeyUsage"] == "SIGN_VERIFY"
    stmts = {s["Sid"]: s for s in props["KeyPolicy"]["Statement"]}
    admin = stmts["AccountAdministersButDoesNotSign"]["Action"]
    assert not any(a in ("kms:*", "kms:Sign", "kms:CreateGrant") or a.endswith("*") for a in admin)
    assert stmts["OnlyTheAppSigns"]["Principal"] == {"AWS": {"Ref": "AppPrincipalArn"}}
    assert "kms:Sign" in stmts["OnlyTheAppSigns"]["Action"]
    deny = stmts["NobodyElseSigns"]
    assert deny["Effect"] == "Deny" and deny["Action"] == "kms:Sign" and deny["Principal"] == "*"
    assert deny["Condition"] == {"StringNotEquals": {"aws:PrincipalArn": {"Ref": "AppPrincipalArn"}}}
    assert t["Outputs"]["PackSigningKeyArn"]["Value"] == {"GetAtt": "PackSigningKey.Arn"}


def test_kms_signer_against_moto_with_the_templates_key(aws):
    template = aws_fakes.template
    props = template()["Resources"]["PackSigningKey"]["Properties"]
    kms = boto3.client("kms", region_name="us-east-1")
    arn = kms.create_key(KeySpec=props["KeySpec"], KeyUsage=props["KeyUsage"])["KeyMetadata"]["Arn"]
    signer = ps.KmsSigner(arn, kms)
    signed = ps.sign_zip(unsigned_zip(), signer)
    assert vp.verify(signed, kid=signer.jwk()["kid"])[0] == []
