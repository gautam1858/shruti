; I2C master write: START, one or more bytes each with an ACK check, STOP.
; The speed grade is set at assembly: FAST = 0 (default) holds SCL low for P and
; high for P, for standard mode; FAST = 1 (-D FAST=1, or fw.load("i2c_master",
; FAST=1)) holds it low for P and high for P/2, the shape fast mode needs
; (tLOW 1.3 us against tHIGH 0.6 us), so 400 kHz is reachable.
; Host setup: P = the SCL low time in cycles; CFG = OSR MSB-first, 8-bit
; words (EMx_CFG), because the program has no room for its own SHIFT.
; SCL is high for P (or P/2) plus the 2 cycles it takes to see it rise, so at
; 50 MHz (measured on the ISS): FAST = 0 with P = 249 gives 100.0 kHz (low 4.98 us,
; high 5.02 us); FAST = 1 with P = 82 gives 400.0 kHz (low 1.64 us, high 0.86 us).
; Host interface: write the address byte (R/W = 0) and the data bytes to the
; TX FIFO before or while the transfer runs; the transfer ends with a STOP
; when the FIFO is empty after an ACK. A NACK ends the transfer with a STOP
; and sets SYNC flag 1. After a NACK the host must flush the TX FIFO (or
; reset the EM): bytes still queued would start a new transaction.
; Both lines are open-drain. Every SCL release waits until SCL is seen rising,
; so slaves may stretch the clock. SDA changes 8 cycles after SCL falls.
; The waits are on edges, not levels: the program runs ahead of its scheduled
; outputs, so when it reaches a WAIT the pin still shows its old level for up to
; L cycles, and a level wait would match at once.
; The program is exactly 32 words: after the STOP the PC wraps to 0.

.equ FAST 0
.if FAST
.define THIGH P/2
.else
.define THIGH P
.endif

.pin scl B6
.pin sda B7

        SET   scl, 1, od         ; release both lines (open-drain)
        SET   sda, 1, od
top:    PULL  block              ; address byte
        ADDT  P, now             ; bus free for at least P since the last STOP
        OUT   sda, 0 @T+0        ; START: SDA falls while SCL is high
bit:    ADDT  THIGH              ; START hold, or SCL high since seen rising
        OUT   scl, 0 @T+0
        OUT   sda, OSR @T+8      ; data bit, 8 cycles after SCL falls
        ADDT  P
        OUT   scl, 1 @T+0        ; release SCL
        WAIT  scl, rise          ; clock stretching: T := SCL seen rising
        JMP   !OSRE, bit
        ADDT  THIGH              ; ACK clock
        OUT   scl, 0 @T+0
        OUT   sda, 1 @T+8        ; release SDA for the slave
        ADDT  P
        OUT   scl, 1 @T+0
        WAIT  scl, rise
        JMP   HIGH sda, nack     ; SDA high while SCL high: NACK
        PULL                     ; next byte, if the host queued one
        JMP   !OSRE, bit
        JMP   stop
nack:   SYNC  set, 1
stop:   ADDT  THIGH
        OUT   scl, 0 @T+0
        OUT   sda, 0 @T+8
        ADDT  P
        OUT   scl, 1 @T+0
        WAIT  scl, rise
        ADDT  P
        OUT   sda, 1 @T+0        ; STOP: SDA rises while SCL is high
        WAIT  sda, rise          ; T := STOP seen; the next START is at least P later
