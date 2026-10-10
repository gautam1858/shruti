; SPI slave, modes 0-3, MSB first, one byte per chip select.
; The mode is set at assembly: CPOL and CPHA below, overridden with -D CPOL=1 -D CPHA=1
; (python -m asm) or fw.load("spi_slave", CPOL=1, CPHA=1). Mode 0 is the default.
; Host setup: none (the master sets the clock).
; Host interface: queue the reply byte in the TX FIFO before the master
; selects the slave; the byte received on MOSI is pushed to the RX FIFO. If no
; reply byte is queued, the slave waits for one and ignores that selection.
; MISO is released (open-drain, high) while CS is high, so the bus can be
; shared, and driven push-pull while selected.
; MOSI is read from the pin snapshot taken at the sampling edge, so it is
; sampled exactly at the edge whatever the instruction timing. SCK idles at
; CPOL; the leading edge leaves the idle level. CPHA = 0: bit 7 goes out after
; CS falls, MOSI is sampled on leading edges and later bits go out after
; trailing edges. CPHA = 1: each bit goes out after a leading edge and MOSI is
; sampled on trailing edges.
; Timing (measured on the ISS): MISO bit 7 is on the pin 5 cycles after CS
; falls (CPHA = 0) and each other bit 4 cycles after the edge that shifts it
; (2 cycles of input latency plus the program). A master that samples MISO at
; the pin needs SCK phases of at least 4 cycles in every mode, and from CS
; falling to the first SCK edge at least 5 cycles (CPHA = 0) or 3 (CPHA = 1).
; The EM master (fw/spi_master.s) sees MISO 2 cycles late; paired with this
; slave over wiring that adds a cycle each way it needs P >= 9 (CPHA = 0) or
; P >= 8 (CPHA = 1), and one less reads wrong bits without any flag being set.

.equ CPOL 0
.equ CPHA 0
.if CPOL
.define LEAD  fall
.define TRAIL rise
.else
.define LEAD  rise
.define TRAIL fall
.endif

.pin sck  M0
.pin mosi M2
.pin cs   M3
.pin miso B4

        SHIFT out, msb, 8
        SHIFT in, msb, 8
idle:   SET   miso, 1, od        ; release MISO while deselected
        PULL  block              ; reply byte
        WAIT  cs, fall           ; T := CS seen falling
        SET   miso, 1, pp        ; drive MISO from here on
.if !CPHA
        OUT   miso, OSR @T+3     ; bit 7 before the first leading edge
        SET   X, 7
bit:    WAIT  sck, LEAD          ; T := leading edge seen; snapshot latched
        IN    mosi @snap         ; MOSI as it was at the edge
        JMP   X--, next
        JMP   done
next:   WAIT  sck, TRAIL
        OUT   miso, OSR @T+2     ; next bit after the trailing edge (T+1 would be late)
        JMP   bit
.else
        SET   X, 7
bit:    WAIT  sck, LEAD          ; T := leading edge seen
        OUT   miso, OSR @T+2     ; this bit after the leading edge
        WAIT  sck, TRAIL         ; snapshot latched at the trailing edge
        IN    mosi @snap         ; MOSI as it was at the edge
        JMP   X--, bit
.endif
done:   PUSH                     ; received byte (non-blocking: OVF if full)
        WAIT  cs, high           ; level, so a deselect before this point is not missed
        JMP   idle
