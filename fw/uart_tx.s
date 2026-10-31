; UART transmitter, 8N1, LSB first.
; Host setup: P = bit period in cycles (e.g. 5208 for 9600 baud at 50 MHz).
; P is 16 bits, so one step per bit reaches down to 763 baud at 50 MHz. The SLOW
; build (-D SLOW=1, or fw.load("uart_tx", SLOW=1)) takes three steps per bit and
; P = a third of the bit period: 300 baud is P = 55,556.
; Host interface: write bytes to the TX FIFO; each becomes one frame.
; Back-to-back bytes are sent with no gap; after idle the start bit follows
; 2 cycles after the byte arrives.

.equ SLOW 0

.pin tx B0

        SET   tx, 1              ; line idles high
        SHIFT out, lsb, 8        ; LSB first, 8 data bits
top:    PULL  block              ; next byte -> OSR
        ADDT  2, now             ; T := max(T, now + 2): never before the previous stop bit ends
        OUT   tx, 0 @T+0         ; start bit
bit:    ADDT  P                  ; T += B
.if SLOW
        ADDT  P                  ; SLOW: P is a third of the bit period
        ADDT  P
.endif
        OUT   tx, OSR @T+0       ; data bit, consumes one OSR bit
        JMP   !OSRE, bit         ; 8 data bits
        ADDT  P
.if SLOW
        ADDT  P                  ; SLOW: P is a third of the bit period
        ADDT  P
.endif
        OUT   tx, 1 @T+0         ; stop bit
        ADDT  P                  ; T = end of stop bit
.if SLOW
        ADDT  P                  ; SLOW: P is a third of the bit period
        ADDT  P
.endif
        JMP   top
