; UART receiver, 8N1, LSB first.
; Host setup: P = bit period in cycles.
; Host interface: received bytes appear in the RX FIFO (non-blocking push: a
; full FIFO drops the byte and sets OVF). A frame whose stop bit is low is
; dropped and sets SYNC flag 0 (framing error). A low pulse shorter than half
; a bit is ignored.
; Speed limit (measured on the ISS): back-to-back frames need B >= 9 cycles.
; Faster input loses bytes without setting any flag.

.pin rx M0

idle:   SHIFT in, lsb, 8         ; clear the ISR
        WAIT  rx, fall           ; start bit edge: T := its timestamp
        ADDT  P/2                ; middle of the start bit
        IN    rx @T+0            ; wait until then (the sample is discarded below)
        JMP   HIGH rx, idle      ; line high again: a glitch, not a start bit
        SHIFT in, lsb, 8         ; drop the start-bit sample
        SET   X, 7
bit:    ADDT  P                  ; middle of the next data bit
        IN    rx @T+0
        JMP   X--, bit           ; 8 data bits
        ADDT  P                  ; middle of the stop bit
        IN    rx @T+0            ; wait until then (ISR is full, the sample is dropped)
        JMP   LOW rx, ferr
        PUSH                     ; byte to the RX FIFO
        JMP   idle
ferr:   SYNC  set, 0             ; framing error
        JMP   idle
