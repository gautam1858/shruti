; I2C target (slave) receiver: any address, the host decides each ACK.
; There is no compare instruction, so the program does not match addresses itself:
; it pushes every byte, the address byte included, to the RX FIFO and holds SCL
; low (clock stretching) until the host has queued its answer for that byte in
; the TX FIFO: 0x00 to ACK, 0xFF to NACK (one bit is used, so the bit order does
; not matter). A host that knows what is coming can queue answers ahead, and
; then SCL is not stretched at all. Writes only: the host NACKs a read address
; (R/W = 1).
; SYNC flag 2 is set at each START or repeated START (the next byte is an
; address) and flag 3 at each STOP.
; Host setup: none; CFG must keep the reset OUT word length of 8 bits.
; Both lines are open-drain. Only the first bit of a byte can turn out to be a
; STOP or a repeated START, so only there does the program watch SDA while SCL
; is high, polling both lines every 2 cycles; an SDA change counts as data if
; SCL is seen low by the next poll, so a master with no data hold time is safe.
; The ACK goes out 4 cycles after the target sees SCL fall, and SCL is released
; 16 cycles (320 ns at 50 MHz) after that. The program is exactly 32 words.

.pin scl B4
.pin sda B5

        SET   scl, 1, od         ; release both lines (open-drain)
        SET   sda, 1, od
idle:   WAIT  sda, fall
        JMP   LOW scl, idle      ; SDA fell while SCL was low: not a START
start:  SHIFT in, msb, 8         ; MSB first; empties the ISR of a stray bit
        SYNC  set, 2             ; START: the next byte is an address
byte:   WAIT  scl, rise          ; first bit of a byte, or a STOP or repeated START
        IN    sda @snap
        JMP   HIGH sda, b7hi
b7lo:   JMP   LOW scl, rest      ; SCL fell: it was a data bit
        JMP   LOW sda, b7lo
        JMP   LOW scl, rest      ; SDA rose with SCL already low: still data
        SYNC  set, 3             ; SDA rose while SCL high: STOP
        JMP   idle
b7hi:   JMP   LOW scl, rest
        JMP   HIGH sda, b7hi
        JMP   LOW scl, rest
        JMP   start              ; SDA fell while SCL high: repeated START
rest:   SET   X, 6
bit:    WAIT  scl, rise          ; snapshot latched at the rise
        IN    sda @snap
        JMP   X--, bit
        PUSH                     ; the byte (non-blocking: OVF if the host lags)
        WAIT  scl, fall          ; end of the eighth clock
        OUT   scl, 0 @T+2        ; hold SCL low while the host decides
        PULL  block              ; the host's answer
        ADDT  2, now
        OUT   sda, OSR @T+0      ; ACK (0) or NACK (1)
        OUT   scl, 1 @T+16       ; release SCL after the data setup time
        WAIT  scl, fall          ; end of the ACK clock
        OUT   sda, 1 @T+8        ; release SDA
        JMP   byte
