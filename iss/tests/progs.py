"""Instruction constructors for tests, encoded through the isa.yaml tables."""

from iss.isa import load_isa

_E = load_isa().encode


def HALT():
    return _E("HALT")


def WAIT(pin, mode, timeout=0):
    return _E("WAIT", pin=pin, mode=mode, timeout=timeout)


def OUT(pin, value, d=0):
    return _E("OUT", pin=pin, value=str(value), d=d)


def IN(pin, d=0, snap=0):
    return _E("IN", pin=pin, snap=snap, d=d)


def ADDT(d, now=0):
    return _E("ADDT", d=d, now=now)


def ADDTP(shift=0, now=0):
    return _E("ADDTP", shift=shift, now=now)


def SHIFT(direction, order, n, auto=0):
    return _E("SHIFT", dir=direction, msb=order, n=n - 1, auto=auto)


def PUSH(block=1):
    return _E("PUSH", block=block)


def PULL(block=1):
    return _E("PULL", block=block)


def SETPIN(pin, level, od=0):
    return _E("SET", target="pin", pin=pin, level=level, od=od)


def SETR(reg, imm):
    return _E("SET", target=reg, imm=imm)


def JMP(target, cond="flag", sel="always"):
    return _E("JMP", cond=cond, pin=sel, target=target)


def SYNC(op, flag):
    return _E("SYNC", op=op, flag=flag)


def uart_tx(tx=0):
    """The 12-word UART transmit program from the spec (bit period in P)."""
    return [
        SETPIN(tx, 1),             # 0        line idles high
        SHIFT("out", "lsb", 8),    # 1
        PULL(1),                   # 2 top:   next byte -> OSR
        ADDT(2, now=1),            # 3        T := max(T, now + 2)
        OUT(tx, 0, 0),             # 4        start bit
        ADDTP(0),                  # 5 bit:   T += P
        OUT(tx, "OSR", 0),         # 6        data bit
        JMP(5, "flag", "!OSRE"),   # 7        8 data bits
        ADDTP(0),                  # 8
        OUT(tx, 1, 0),             # 9        stop bit
        ADDTP(0),                  # 10       T = end of stop bit
        JMP(2),                    # 11
    ]
