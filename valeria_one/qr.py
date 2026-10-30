"""A QR code for a short link (a pay link on the register's screen), drawn as SVG — no extra package, nothing online.

QR Code Model 2 (ISO/IEC 18004), byte mode, error correction level M (15 % of the code can be covered or smudged and
it still reads), versions 1-10 (up to 213 bytes — our own links are ~50). The mask is chosen by the standard's penalty
rules. Kept small on purpose: it only ever encodes this server's own short addresses.
"""
from __future__ import annotations

# error correction level M: per version, the error-correction codewords per block and the blocks as (count, data codewords)
_M = {1: (10, ((1, 16),)), 2: (16, ((1, 28),)), 3: (26, ((1, 44),)), 4: (18, ((2, 32),)), 5: (24, ((2, 43),)),
      6: (16, ((4, 27),)), 7: (18, ((4, 31),)), 8: (22, ((2, 38), (2, 39))), 9: (22, ((3, 36), (2, 37))),
      10: (26, ((4, 43), (1, 44)))}
_ALIGN = {1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34), 7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46),
          10: (6, 28, 50)}
_FORMAT_M = 0                                               # the two format bits of level M


class TooLong(ValueError):
    pass


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    out = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            out[j] = _gf_mul(out[j], root)
            if j + 1 < degree:
                out[j] ^= out[j + 1]
        root = _gf_mul(root, 0x02)
    return out


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    out = [0] * len(divisor)
    for b in data:
        factor = b ^ out.pop(0)
        out.append(0)
        for i, coef in enumerate(divisor):
            out[i] ^= _gf_mul(coef, factor)
    return out


def _capacity(version: int) -> int:
    return sum(n * d for n, d in _M[version][1])


def _codewords(data: bytes) -> tuple[int, list[int]]:
    """Pick the smallest version that holds the data; -> (version, the final interleaved codewords)."""
    for version in _M:
        count_bits = 8 if version < 10 else 16
        cap_bits = _capacity(version) * 8
        if 4 + count_bits + 8 * len(data) <= cap_bits:
            break
    else:
        raise TooLong("too long for a small QR code")
    bits: list[int] = []

    def put(v: int, n: int) -> None:
        bits.extend((v >> i) & 1 for i in reversed(range(n)))
    put(0b0100, 4)
    put(len(data), count_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap_bits - len(bits)))
    put(0, (8 - len(bits) % 8) % 8)
    pad = 0xEC
    while len(bits) < cap_bits:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    ec_len, groups = _M[version]
    div = _rs_divisor(ec_len)
    blocks, i = [], 0
    for n, d in groups:
        for _ in range(n):
            blocks.append(words[i:i + d])
            i += d
    ecs = [_rs_remainder(b, div) for b in blocks]
    out = [b[k] for k in range(max(len(b) for b in blocks)) for b in blocks if k < len(b)]
    out += [e[k] for k in range(ec_len) for e in ecs]
    return version, out


class _Grid:
    def __init__(self, version: int):
        self.v = version
        self.n = n = 17 + 4 * version
        self.m = [[False] * n for _ in range(n)]
        self.fn = [[False] * n for _ in range(n)]
        self.info: set[tuple[int, int]] = set()              # format and version cells (light while masks are judged)

    def set(self, x: int, y: int, dark: bool) -> None:
        self.m[y][x] = dark
        self.fn[y][x] = True

    def functions(self) -> None:
        n = self.n
        for i in range(n):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _ALIGN[self.v]
        last = len(pos) - 1
        for i, cx in enumerate(pos):
            for j, cy in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)                                  # reserve the format area (drawn for real after masking)
        if self.v >= 7:
            rem = self.v
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.v << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self.set(a, b, bit)
                self.set(b, a, bit)
                self.info |= {(a, b), (b, a)}

    def format_bits(self, mask: int) -> None:
        n = self.n
        data = _FORMAT_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        b = lambda i: (bits >> i) & 1 == 1                   # noqa: E731
        cells = [((8, i), b(i)) for i in range(6)] + [((8, 7), b(6)), ((8, 8), b(7)), ((7, 8), b(8))]
        cells += [((14 - i, 8), b(i)) for i in range(9, 15)] + [((n - 1 - i, 8), b(i)) for i in range(8)]
        cells += [((8, n - 15 + i), b(i)) for i in range(8, 15)] + [((8, n - 8), True)]
        for (x, y), dark in cells:
            self.set(x, y, dark)
            self.info.add((x, y))

    def place(self, words: list[int]) -> None:
        n, i, total = self.n, 0, len(words) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not self.fn[y][x] and i < total:
                        self.m[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def mask(self, k: int) -> None:
        f = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0, lambda x, y: (x + y) % 3 == 0,
             lambda x, y: (x // 3 + y // 2) % 2 == 0, lambda x, y: x * y % 2 + x * y % 3 == 0,
             lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0, lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)[k]
        for y in range(self.n):
            for x in range(self.n):
                if not self.fn[y][x] and f(x, y):
                    self.m[y][x] = not self.m[y][x]

    def penalty(self) -> int:
        """The standard's four penalties (N1 runs, N2 blocks, N3 finder-like 1:1:3:1:1, N4 dark share), judged with the
        format and version cells light, as ISO/IEC 18004 7.8 places them only after the mask is chosen."""
        n = self.n
        m = [[False if (x, y) in self.info else self.m[y][x] for x in range(n)] for y in range(n)]
        score = 0
        lines = m + [[m[y][x] for y in range(n)] for x in range(n)]
        pat = [True, False, True, True, True, False, True]
        for line in lines:
            run, prev = 0, None
            for c in line:
                if c == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, c
            if run >= 5:
                score += run - 2
            idx = self._find(line, pat, 0)
            while idx != -1:
                end = idx + 7
                if idx in (0, n - 7) or not any(line[max(idx - 4, 0):idx]) or not any(line[end:min(end + 4, n)]):
                    score += 40
                else:
                    end = idx + 4
                idx = self._find(line, pat, end)
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(r) for r in m)
        score += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
        return score

    @staticmethod
    def _find(line: list[bool], pat: list[bool], start: int) -> int:
        for i in range(start, len(line) - len(pat) + 1):
            if line[i:i + len(pat)] == pat:
                return i
        return -1


def matrix(text: str, mask: int | None = None) -> list[list[bool]]:
    """The modules (True = dark), without the quiet zone. mask: force one (0-7); default = the lowest penalty."""
    version, words = _codewords(text.encode("utf-8"))
    best = None
    for k in (range(8) if mask is None else (mask,)):
        g = _Grid(version)
        g.functions()
        g.place(words)
        g.mask(k)
        g.format_bits(k)
        p = g.penalty() if mask is None else 0
        if best is None or p < best[0]:
            best = (p, g)
    return best[1].m


def svg(text: str, size: int = 220) -> str:
    """An <svg> of the code (dark on white, with its quiet zone), to drop into a page."""
    m = matrix(text)
    n = len(m) + 8
    path = "".join(f"M{x + 4},{y + 4}h1v1h-1z" for y, row in enumerate(m) for x, dark in enumerate(row) if dark)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{size}" height="{size}" shape-rendering="crispEdges" '
            f'role="img"><rect width="{n}" height="{n}" fill="#fff"/><path fill="#000" d="{path}"/></svg>')
