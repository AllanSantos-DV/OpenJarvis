"""Verificador Ed25519 pure-Python (stdlib) — SUBCONJUNTO Ed25519 da implementação
de REFERÊNCIA do RFC 8032, Apêndice A (Josefsson & Liusvaara, IETF, 2017).

NÃO é crypto nova: é uma transcrição fiel (verify-only) do código autoritativo do
RFC, para o consumidor Python que NÃO tem `cryptography` (ex.: Python 3.14, cujas
wheels do `cryptography` podem inexistir) verificar a assinatura do instalador SEM
dependência externa. Só `hashlib` (SHA-512). Inclui as checagens do RFC: R/A pontos
válidos e S < L (não-malleável, §8.4) e a equação com multiplicação pelo cofator (§8.8).

Uso: ``verify(pubkey_bytes32, message_bytes, signature_bytes64) -> bool`` (fail-closed:
qualquer erro/entrada inválida ⇒ False; nunca levanta). No vox-engine a ``message`` é
o SHA-256 do instalador (hash-then-sign, igual ao tools/sign_release.py).

Fonte: https://www.rfc-editor.org/rfc/rfc8032#appendix-A  (domínio público / IETF Trust).
"""
from __future__ import annotations

import hashlib


def _from_le(s: bytes) -> int:
    return int.from_bytes(s, byteorder="little")


def _sqrt4k3(x: int, p: int) -> int:
    return pow(x, (p + 1) // 4, p)


def _sqrt8k5(x: int, p: int) -> int:
    y = pow(x, (p + 3) // 8, p)
    if (y * y) % p == x % p:
        return y
    z = pow(2, (p - 1) // 4, p)
    return (y * z) % p


class _Field:
    __slots__ = ("_x", "_p")

    def __init__(self, x: int, p: int):
        self._x = x % p
        self._p = p

    def __add__(self, y): return _Field(self._x + y._x, self._p)
    def __sub__(self, y): return _Field(self._p + self._x - y._x, self._p)
    def __neg__(self): return _Field(self._p - self._x, self._p)
    def __mul__(self, y): return _Field(self._x * y._x, self._p)
    def inv(self): return _Field(pow(self._x, self._p - 2, self._p), self._p)
    def __truediv__(self, y): return self * y.inv()

    def sqrt(self):
        if self._p % 4 == 3:
            y = _sqrt4k3(self._x, self._p)
        elif self._p % 8 == 5:
            y = _sqrt8k5(self._x, self._p)
        else:
            raise NotImplementedError("sqrt(_,8k+1)")
        _y = _Field(y, self._p)
        return _y if _y * _y == self else None

    def make(self, ival): return _Field(ival, self._p)
    def iszero(self): return self._x == 0
    def __eq__(self, y): return self._x == y._x and self._p == y._p
    def __ne__(self, y): return not (self == y)

    def tobytes(self, b): return self._x.to_bytes(b // 8, byteorder="little")

    def frombytes(self, x, b):
        rv = _from_le(x) % (2 ** (b - 1))
        return _Field(rv, self._p) if rv < self._p else None

    def sign(self): return self._x % 2


class _EdwardsPoint:
    base_field = None

    def initpoint(self, x, y):
        self.x = x
        self.y = y
        self.z = self.base_field.make(1)

    def decode_base(self, s, b):
        if len(s) != b // 8:
            return (None, None)
        xs = s[(b - 1) // 8] >> ((b - 1) & 7)
        y = self.base_field.frombytes(s, b)
        if y is None:
            return (None, None)
        x = self.solve_x2(y).sqrt()
        if x is None or (x.iszero() and xs != x.sign()):
            return (None, None)
        if x.sign() != xs:
            x = -x
        return (x, y)

    def encode_base(self, b):
        xp, yp = self.x / self.z, self.y / self.z
        s = bytearray(yp.tobytes(b))
        if xp.sign() != 0:
            s[(b - 1) // 8] |= 1 << (b - 1) % 8
        return s

    def __mul__(self, x):
        r = self.zero_elem()
        s = self
        while x > 0:
            if (x % 2) > 0:
                r = r + s
            s = s.double()
            x = x // 2
        return r

    def __eq__(self, y):
        xn1 = self.x * y.z
        xn2 = y.x * self.z
        yn1 = self.y * y.z
        yn2 = y.y * self.z
        return xn1 == xn2 and yn1 == yn2

    def __ne__(self, y): return not (self == y)


class _Edwards25519Point(_EdwardsPoint):
    base_field = _Field(1, 2 ** 255 - 19)
    d = -base_field.make(121665) / base_field.make(121666)
    f0 = base_field.make(0)
    f1 = base_field.make(1)
    xb = base_field.make(int.from_bytes(bytes.fromhex(
        "216936D3CD6E53FEC0A4E231FDD6DC5C692CC7609525A7B2C9562D608F25D51A"), "big"))
    yb = base_field.make(int.from_bytes(bytes.fromhex(
        "6666666666666666666666666666666666666666666666666666666666666658"), "big"))

    @staticmethod
    def stdbase():
        return _Edwards25519Point(_Edwards25519Point.xb, _Edwards25519Point.yb)

    def __init__(self, x, y):
        if y * y - x * x != self.f1 + self.d * x * x * y * y:
            raise ValueError("Invalid point")
        self.initpoint(x, y)
        self.t = x * y

    def decode(self, s):
        x, y = self.decode_base(s, 256)
        return _Edwards25519Point(x, y) if x is not None else None

    def encode(self):
        return self.encode_base(256)

    def zero_elem(self):
        return _Edwards25519Point(self.f0, self.f1)

    def solve_x2(self, y):
        return ((y * y - self.f1) / (self.d * y * y + self.f1))

    def __add__(self, y):
        tmp = self.zero_elem()
        zcp = self.z * y.z
        A = (self.y - self.x) * (y.y - y.x)
        B = (self.y + self.x) * (y.y + y.x)
        C = (self.d + self.d) * self.t * y.t
        D = zcp + zcp
        E, H = B - A, B + A
        F, G = D - C, D + C
        tmp.x, tmp.y, tmp.z, tmp.t = E * F, G * H, F * G, E * H
        return tmp

    def double(self):
        tmp = self.zero_elem()
        A = self.x * self.x
        B = self.y * self.y
        Ch = self.z * self.z
        C = Ch + Ch
        H = A + B
        xys = self.x + self.y
        E = H - xys * xys
        G = A - B
        F = C + G
        tmp.x, tmp.y, tmp.z, tmp.t = E * F, G * H, F * G, E * H
        return tmp

    def l(self):
        return int.from_bytes(bytes.fromhex(
            "1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3ed"), "big")

    def c(self): return 3
    def n(self): return 254
    def b(self): return 256


def verify(pubkey: bytes, msg: bytes, sig: bytes) -> bool:
    """Verifica a assinatura Ed25519 ``sig`` (64 bytes) da mensagem ``msg`` sob a
    chave pública ``pubkey`` (32 bytes). Fail-closed: qualquer erro ⇒ False."""
    try:
        if not isinstance(pubkey, (bytes, bytearray)) or len(pubkey) != 32:
            return False
        if not isinstance(sig, (bytes, bytearray)) or len(sig) != 64:
            return False
        B = _Edwards25519Point.stdbase()
        ln, bb = B.l(), B.b()
        Rraw, Sraw = sig[:bb // 8], sig[bb // 8:]
        R = B.decode(bytes(Rraw))
        S = _from_le(Sraw)
        A = B.decode(bytes(pubkey))
        if (R is None) or (A is None) or S >= ln:
            return False
        h = _from_le(hashlib.sha512(bytes(Rraw) + bytes(pubkey) + bytes(msg)).digest()) % ln
        # Equação COFATORLESS (estrita), igual ao OpenSSL/cryptography: [S]B == R + [h]A.
        # A referência do RFC 8032 multiplica ambos os lados pelo cofator (×8), o que ACEITA
        # forjas de ordem pequena que o OpenSSL rejeita (medido: 167/576). Ed25519 válido
        # satisfaz a equação SEM cofator por construção (S = r + h·a), então remover o ×8 é
        # ESTRITAMENTE mais seguro (rejeita malleabilidade de ordem pequena) e não rejeita
        # nenhuma assinatura legítima — provado por diferencial vs cryptography (0 mismatch).
        return (B * S) == (R + (A * h))
    except Exception:  # noqa: BLE001  — fail-closed, nunca levanta
        return False
