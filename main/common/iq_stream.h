#pragma once
#include <stdbool.h>
#include <stddef.h>

/* Continuous gapless complex baseband streaming via the modem IQ estimator
 * (esp-sdr-zephyr paced method). Gapless up to the transport limit; blocks
 * are emitted as IQFRAME records by the shared monitor worker. */
bool iq_stream_start(unsigned rate_hz, char *err, size_t err_size);
void iq_stream_stop(void);
bool iq_stream_running(void);
unsigned iq_stream_rate(void);
void iq_stream_stats(char *buf, size_t size);
bool iq_stream_debug(char *reply, size_t size);
