; SPI slave, mode 0 (CPOL = 0, CPHA = 0), MSB first, one byte per chip select.
; Host setup: none (the master sets the clock).
; Host interface: queue the reply byte in the TX FIFO before the master
; selects the slave; the byte received on MOSI is pushed to the RX FIFO. If no
; reply byte is queued, the slave waits for one and ignores that selection.
; MISO is released (open-drain, high) while CS is high, so the bus can be
; shared, and driven push-pull while selected.
; MOSI is read from the pin snapshot taken at the SCK rising edge, so it is
; sampled exactly at the edge whatever the instruction timing.
; Timing (measured on the ISS): MISO bit 7 is on the pin 5 cycles after CS
; falls and each later bit 4 cycles after SCK falls (2 cycles of input latency
; plus the program). A master that samples MISO at the pin needs at least 5
; cycles from CS falling to the first SCK rise and SCK phases of at least 4
; cycles. The EM master (fw/spi_master.s) sees MISO 2 cycles late; paired
; with this slave over wiring that adds a cycle each way it needs P >= 9, and
; at P = 8 it reads wrong bits without any flag being set.

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
        OUT   miso, OSR @T+3     ; bit 7 before the first rising edge
        SET   X, 7
bit:    WAIT  sck, rise          ; T := SCK seen rising; snapshot latched
        IN    mosi @snap         ; MOSI as it was at the edge
        JMP   X--, next
        JMP   done
next:   WAIT  sck, fall
        OUT   miso, OSR @T+2     ; next bit after the falling edge (T+1 would be late)
        JMP   bit
done:   PUSH                     ; received byte (non-blocking: OVF if full)
        WAIT  cs, rise
        JMP   idle
