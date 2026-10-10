; SPI master, modes 0-3, MSB first, one byte per chip select.
; The mode is set at assembly: CPOL and CPHA below, overridden with -D CPOL=1 -D CPHA=1
; (python -m asm) or fw.load("spi_master", CPOL=1, CPHA=1). Mode 0 is the default.
; Host setup: P = half the SCK period in cycles.
; Host interface: each byte written to the TX FIFO is sent on MOSI; the byte
; clocked in on MISO at the same time is pushed to the RX FIFO.
; Timing: SCK idles at CPOL. CS falls; the first leading edge (SCK leaving its
; idle level) comes P later, and each SCK level lasts exactly P. With CPHA = 0,
; MOSI bit 7 goes out with CS, later bits on trailing edges, and both ends sample
; on leading edges. With CPHA = 1, MOSI changes on leading edges and both ends
; sample on trailing edges. CS rises P after the last trailing edge and stays
; high for at least P.

.equ CPOL 0
.equ CPHA 0
.if CPOL
.define IDLE   1
.define ACTIVE 0
.else
.define IDLE   0
.define ACTIVE 1
.endif

.pin sck  B2
.pin mosi B3
.pin miso M1
.pin cs   B5

        SET   cs, 1
        SET   sck, IDLE
        SHIFT out, msb, 8
        SHIFT in, msb, 8
top:    PULL  block
        ADDT  3, now             ; T := max(T, now + 3): two OUTs at T below, both on time
        OUT   cs, 0 @T+0         ; select
.if !CPHA
bit:    OUT   mosi, OSR @T+0     ; data out before the leading edge
        ADDT  P
        OUT   sck, ACTIVE @T+0   ; leading edge
        IN    miso @T+0          ; sample MISO at the leading edge
        ADDT  P
        OUT   sck, IDLE @T+0     ; trailing edge
.else
bit:    ADDT  P
        OUT   sck, ACTIVE @T+0   ; leading edge
        OUT   mosi, OSR @T+0     ; data out on the leading edge
        ADDT  P
        OUT   sck, IDLE @T+0     ; trailing edge
        IN    miso @T+0          ; sample MISO at the trailing edge
.endif
        JMP   !OSRE, bit
        ADDT  P
        OUT   cs, 1 @T+0         ; deselect
        PUSH  block              ; received byte to the RX FIFO
        ADDT  P                  ; CS stays high for at least P
        JMP   top
