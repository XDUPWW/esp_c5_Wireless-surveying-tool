#pragma once

#include <stdbool.h>
#include <stddef.h>

typedef enum { BURST_SERIAL_USB, BURST_SERIAL_UART, BURST_SERIAL_COUNT } burst_serial_port_t;

void burst_serial_init(void);
/* 1: complete line, -1: overlong line, 0: idle. Each port has its own parser.
 * A complete line selects the port for all subsequent replies and payloads.
 * BAUD? and BAUD <1000000|2000000> are consumed internally. Baud changes
 * last until reset; OK BAUD <rate> is sent at the previous rate. */
int burst_serial_poll_line(char *line, size_t capacity);
burst_serial_port_t burst_serial_port(void);
/* The port the session's commands arrive on (lease identity), which can
 * differ from burst_serial_port() once OUTPUT pins the data elsewhere. */
burst_serial_port_t burst_serial_input_port(void);
unsigned burst_serial_baud(void);
bool burst_serial_send(const void *data, size_t size);
/* Bounded send: false if the whole buffer could not be handed to the
 * transport within `ms`. High-rate streams use this so a host that stops
 * reading costs frames (and a truncated frame the host resyncs on) instead
 * of blocking the shared TX path and the command channel. */
bool burst_serial_send_timeout(const void *data, size_t size, unsigned ms);
/* Write only what fits now; never wait for the transport. */
size_t burst_serial_try_send(const void *data, size_t size);

/* Consume a stream stop request on the owning port only. */
bool burst_serial_stop_requested(void);

/* Read commands forever and pass each line to `handle`. One port at a time
 * owns the receiver: the other gets "ERR busy" until the owner sends RELEASE
 * or stays silent for five seconds. RELEASE itself is answered here. */
void burst_serial_serve(void (*handle)(char *line));
