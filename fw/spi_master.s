; SPI master, mode 0 (CPOL = 0, CPHA = 0), MSB first, one byte per chip select.
; Host setup: P = half the SCK period in cycles.
; Host interface: each byte written to the TX FIFO is sent on MOSI; the byte
; clocked in on MISO at the same time is pushed to the RX FIFO.
; Timing: CS falls with MOSI bit 7; SCK rises P later (both ends sample on the
; rising edge); MOSI changes on the falling edge; CS rises P after the last
; falling edge and stays high for at least P.

.pin sck  B2
.pin mosi B3
.pin miso M1
.pin cs   B5

        SET   cs, 1
        SET   sck, 0
        SHIFT out, msb, 8
        SHIFT in, msb, 8
top:    PULL  block
        ADDT  3, now             ; T := max(T, now + 3): two OUTs at T below, both on time
        OUT   cs, 0 @T+0         ; select
bit:    OUT   mosi, OSR @T+0     ; data out while SCK is low
        ADDT  P
        OUT   sck, 1 @T+0        ; rising edge
        IN    miso @T+0          ; sample MISO at the rising edge
        ADDT  P
        OUT   sck, 0 @T+0        ; falling edge
        JMP   !OSRE, bit
        ADDT  P
        OUT   cs, 1 @T+0         ; deselect
        PUSH  block              ; received byte to the RX FIFO
        ADDT  P                  ; CS stays high for at least P
        JMP   top
