#pragma once
/* Three-mode coexistence framework (ESP32-C5): one RF owner at a time.
 * The SDR dump path owns the PHY in RX test mode and is exclusive; the two
 * protocol monitors share the radio through the Wi-Fi/BLE coexistence
 * scheduler. Monitor frames of both kinds funnel through one output worker
 * so the burst protocol keeps frame boundaries. */
#include <stdbool.h>
#include <stdint.h>

#define MON_FRAME_MAX 1600u /* monitor payloads are truncated to this */
#define MON_POOL_SIZE 8u    /* static frame slots shared by both monitors */

typedef enum { MON_TYPE_WIFI = 0, MON_TYPE_BLE = 1, MON_TYPE_IQ = 2 } mon_type_t;

/* One queued monitor frame. Slots live in a static pool and are passed by
 * pointer through the queues. */
typedef struct {
    uint8_t type;    /* mon_type_t */
    uint8_t channel; /* Wi-Fi channel the frame arrived on; 0 for BLE */
    int8_t rssi;     /* dBm; 127 when the stack reports none */
    uint16_t len;    /* bytes used in data[] */
    uint8_t data[MON_FRAME_MAX];
} mon_frame_t;

void mode_manager_init(void);
/* MON command parsing; returns true when `line` was a monitor command. */
bool mode_manager_command(const char *line);
/* A monitor is running: snapshot and spectrum commands must be refused. */
bool mode_manager_busy(void);
/* Copy one frame into the pool and hand it to the output worker. Called from
 * Wi-Fi and NimBLE task contexts: never blocks, drops on overflow. */
bool mon_frame_push(mon_type_t type, const void *data, uint16_t len, int8_t rssi, uint8_t channel);

/* Defined by the C5 receiver: force the next capture to rebuild the PHY RX
 * state after the protocol stacks have had the radio. */
void sdr_rx_invalidate(void);
