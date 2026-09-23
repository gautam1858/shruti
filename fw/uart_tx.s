; UART transmitter, 8N1, LSB first.
; Host setup: P = bit period in cycles (e.g. 5208 for 9600 baud at 50 MHz).
; Host interface: write bytes to the TX FIFO; each becomes one frame.
; Back-to-back bytes are sent with no gap; after idle the start bit follows
; 2 cycles after the byte arrives.

.pin tx B0

        SET   tx, 1              ; line idles high
        SHIFT out, lsb, 8        ; LSB first, 8 data bits
top:    PULL  block              ; next byte -> OSR
        ADDT  2, now             ; T := max(T, now + 2): never before the previous stop bit ends
        OUT   tx, 0 @T+0         ; start bit
bit:    ADDT  P                  ; T += B
        OUT   tx, OSR @T+0       ; data bit, consumes one OSR bit
        JMP   !OSRE, bit         ; 8 data bits
        ADDT  P
        OUT   tx, 1 @T+0         ; stop bit
        ADDT  P                  ; T = end of stop bit
        JMP   top
