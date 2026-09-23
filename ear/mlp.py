"""Bit-exact reference of the Ear's ternary classifier and its weight format.

    acc_j  = B[j] + sum_i W1[j][i] * f[i]     16-bit signed, i = 0..71, j = 0..7
    h_j    = min(255, max(0, acc_j) >> SHIFT)  8-bit
    o_k    = sum_j W2[k][j] * h_j             12-bit signed, k = 0..7
    class  = argmax_k o_k (lowest k on ties)
    confident = best - second_best >= MARGIN
    ood       = best < FLOOR  or  window edges < MIN_EDGES

Weights are ternary, 2 bits each: 00 = 0, 01 = +1, 11 = -1 (10 is treated as 0 and flagged).
B[j] are 8-bit signed per-hidden-unit thresholds (the accumulator starts at B[j]).
The chain is the 640 weights in the order W1[0][0..71], W1[1][0..71], ..., W1[7][..],
W2[0][0..7], ..., W2[7][..] (weight n in bits 2n+1:2n), then B[0..7] as bytes: 1,344 bits,
stored as 168 bytes, least significant byte first. The hardware evaluates one weight per cycle
into a single accumulator (640 cycles) and then takes the argmax (8 cycles).
Pure Python, no dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence

from .features import N_FEATURES

HIDDEN = 8
CLASSES_N = 8
N_WEIGHTS = N_FEATURES * HIDDEN + HIDDEN * CLASSES_N      # 640
N_BYTES = N_WEIGHTS // 4 + HIDDEN                        # 160 + 8 = 168
_ENC = {0: 0b00, 1: 0b01, -1: 0b11}
_DEC = {0b00: 0, 0b01: 1, 0b11: -1, 0b10: 0}


@dataclass
class Config:
    shift: int = 4          # 0..15
    margin: int = 8         # 0..255
    floor: int = 0          # signed 12-bit
    min_edges: int = 24     # 0..256, activity gate


@dataclass
class Result:
    cls: int
    confident: bool
    ood: bool
    logits: List[int]
    hidden: List[int]


@dataclass
class Model:
    w1: List[List[int]]                              # [HIDDEN][N_FEATURES], values -1/0/+1
    w2: List[List[int]]                              # [CLASSES_N][HIDDEN]
    cfg: Config = field(default_factory=Config)
    bias: List[int] = field(default_factory=lambda: [0] * HIDDEN)   # -128..127

    def infer(self, f: Sequence[int], edges: int = 256) -> Result:
        assert len(f) == N_FEATURES and all(0 <= x <= 255 for x in f)
        hidden = []
        for j in range(HIDDEN):
            acc = self.bias[j] + sum(w * x for w, x in zip(self.w1[j], f))
            assert -32768 <= acc <= 32767
            hidden.append(min(255, max(0, acc) >> self.cfg.shift))
        logits = [sum(w * h for w, h in zip(self.w2[k], hidden)) for k in range(CLASSES_N)]
        best_k = max(range(CLASSES_N), key=lambda k: (logits[k], -k))
        best = logits[best_k]
        second = max(v for k, v in enumerate(logits) if k != best_k)
        return Result(best_k, best - second >= self.cfg.margin,
                      best < self.cfg.floor or edges < self.cfg.min_edges, logits, hidden)

    # -- weight chain ---------------------------------------------------------------------

    def weights(self) -> List[int]:
        return [w for row in self.w1 for w in row] + [w for row in self.w2 for w in row]

    def to_bytes(self) -> bytes:
        chain = 0
        for n, w in enumerate(self.weights()):
            chain |= _ENC[w] << (2 * n)
        assert all(-128 <= b <= 127 for b in self.bias)
        return chain.to_bytes(N_WEIGHTS // 4, "little") + bytes(b & 0xFF for b in self.bias)

    @classmethod
    def from_bytes(cls, data: bytes, cfg: Config = None) -> "Model":
        if len(data) != N_BYTES:
            raise ValueError(f"expected {N_BYTES} bytes, got {len(data)}")
        chain = int.from_bytes(data[:N_WEIGHTS // 4], "little")
        bias = [b - 256 if b >= 128 else b for b in data[N_WEIGHTS // 4:]]
        codes = [(chain >> (2 * n)) & 3 for n in range(N_WEIGHTS)]
        if 0b10 in codes:
            raise ValueError("weight code 10 is reserved")
        ws = [_DEC[c] for c in codes]
        n1 = N_FEATURES * HIDDEN
        w1 = [ws[j * N_FEATURES:(j + 1) * N_FEATURES] for j in range(HIDDEN)]
        w2 = [ws[n1 + k * HIDDEN:n1 + (k + 1) * HIDDEN] for k in range(CLASSES_N)]
        return cls(w1, w2, cfg or Config(), bias)

    def to_hex(self) -> str:
        """One line per 16 bytes, least significant byte first (the host loads it in order)."""
        b = self.to_bytes()
        return "".join(b[i:i + 16].hex() + "\n" for i in range(0, len(b), 16))

    @classmethod
    def from_hex(cls, text: str, cfg: Config = None) -> "Model":
        return cls.from_bytes(bytes.fromhex("".join(text.split())), cfg)
