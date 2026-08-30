"""Repository-external encrypted storage for replay material.

The portable case manifest deliberately contains no recoverable conversation
or tool payload.  This module stores that material in a small, authenticated
capsule using only Python's standard library so the offline harness remains
installable without another runtime dependency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


CAPSULE_FORMAT = "lanscoder-eval-capsule"
CAPSULE_VERSION = 1
_KDF_ITERATIONS = 600_000
_SALT_BYTES = 16
_NONCE_BYTES = 16
_KEY_BYTES = 32


class CapsuleError(ValueError):
    """The capsule is invalid, cannot be decrypted, or violates its policy."""


def write_capsule(
    path: str | Path,
    payload: Mapping[str, Any],
    passphrase: str,
    *,
    repository_root: str | Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Encrypt and write one capsule, refusing accidental repository writes."""

    target = Path(path)
    _validate_passphrase(passphrase)
    protected_root = Path.cwd() if repository_root is None else Path(repository_root)
    if _is_within(target, protected_root):
        raise CapsuleError("encrypted capsules must be stored outside the repository")
    if target.exists() and not overwrite:
        raise FileExistsError(f"capsule already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    plaintext = _canonical_json(dict(payload)).encode("utf-8")
    salt = secrets.token_bytes(_SALT_BYTES)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    encryption_key, mac_key = _derive_keys(passphrase, salt)
    ciphertext = _xor_stream(plaintext, encryption_key, nonce)
    header = {
        "format": CAPSULE_FORMAT,
        "version": CAPSULE_VERSION,
        "kdf": {"name": "pbkdf2_hmac_sha256", "iterations": _KDF_ITERATIONS, "salt": _b64(salt)},
        "cipher": {"name": "hmac_sha256_keystream_xor", "nonce": _b64(nonce)},
    }
    tag = hmac.new(mac_key, _authenticated_bytes(header, ciphertext), hashlib.sha256).digest()
    envelope = {
        **header,
        "ciphertext": _b64(ciphertext),
        "tag": _b64(tag),
    }
    encoded = (_canonical_json(envelope) + "\n").encode("utf-8")
    _atomic_write(target, encoded, overwrite=overwrite)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def read_capsule(path: str | Path, passphrase: str) -> dict[str, Any]:
    """Authenticate, decrypt, and decode one capsule without exposing its file."""

    _validate_passphrase(passphrase)
    source = Path(path)
    try:
        envelope = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CapsuleError(f"cannot read capsule {source}: {exc}") from exc
    if not isinstance(envelope, dict):
        raise CapsuleError("capsule envelope must be a JSON object")
    _validate_envelope(envelope)
    header = {
        "format": envelope["format"],
        "version": envelope["version"],
        "kdf": envelope["kdf"],
        "cipher": envelope["cipher"],
    }
    try:
        salt = _unb64(envelope["kdf"]["salt"])
        nonce = _unb64(envelope["cipher"]["nonce"])
        ciphertext = _unb64(envelope["ciphertext"])
        tag = _unb64(envelope["tag"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CapsuleError("capsule contains invalid base64 data") from exc
    encryption_key, mac_key = _derive_keys(passphrase, salt)
    expected = hmac.new(mac_key, _authenticated_bytes(header, ciphertext), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise CapsuleError("capsule authentication failed; check the passphrase or file")
    plaintext = _xor_stream(ciphertext, encryption_key, nonce)
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CapsuleError("capsule plaintext is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise CapsuleError("capsule payload must be a JSON object")
    return payload


def capsule_payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the stable digest used to link a portable manifest to its capsule."""

    return hashlib.sha256(_canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def _validate_passphrase(passphrase: str) -> None:
    if not isinstance(passphrase, str) or not passphrase:
        raise CapsuleError("capsule passphrase must be a non-empty string")


def _validate_envelope(envelope: dict[str, Any]) -> None:
    if envelope.get("format") != CAPSULE_FORMAT or envelope.get("version") != CAPSULE_VERSION:
        raise CapsuleError("unsupported capsule format or version")
    kdf = envelope.get("kdf")
    cipher = envelope.get("cipher")
    if not isinstance(kdf, dict) or not isinstance(cipher, dict):
        raise CapsuleError("capsule is missing KDF or cipher metadata")
    if kdf.get("name") != "pbkdf2_hmac_sha256" or kdf.get("iterations") != _KDF_ITERATIONS:
        raise CapsuleError("unsupported capsule KDF")
    if cipher.get("name") != "hmac_sha256_keystream_xor":
        raise CapsuleError("unsupported capsule cipher")
    if not isinstance(envelope.get("ciphertext"), str) or not isinstance(envelope.get("tag"), str):
        raise CapsuleError("capsule is missing ciphertext or authentication tag")


def _derive_keys(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    material = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, _KDF_ITERATIONS, dklen=_KEY_BYTES * 2)
    return material[:_KEY_BYTES], material[_KEY_BYTES:]


def _xor_stream(value: bytes, key: bytes, nonce: bytes) -> bytes:
    output = bytearray(len(value))
    for offset in range(0, len(value), hashlib.sha256().digest_size):
        counter = offset // hashlib.sha256().digest_size
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        for index, byte in enumerate(value[offset : offset + len(block)]):
            output[offset + index] = byte ^ block[index]
    return bytes(output)


def _authenticated_bytes(header: dict[str, Any], ciphertext: bytes) -> bytes:
    return _canonical_json(header).encode("utf-8") + b"\n" + ciphertext


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise TypeError("base64 value must be a string")
    return base64.b64decode(value.encode("ascii"), validate=True)


def _atomic_write(path: Path, data: bytes, *, overwrite: bool) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and not overwrite:
            raise FileExistsError(f"capsule already exists: {path}")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
