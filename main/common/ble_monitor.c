#include "ble_monitor.h"

#include <stdio.h>
#include <string.h>

#include "esp_err.h"
#include "host/ble_gap.h"
#include "host/ble_hs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"

#include "mode_manager.h"

/* Wire layout of a BLE monitor frame payload: a complete advertising-channel
 * PDU, 2-byte header (PDU type, TxAdd, length) plus AdvA and advertising
 * data, exactly as it left the modem. The hop to the three advertising
 * channels is invisible to the host stack, so the channel field is a fixed
 * 37 (hardware limitation; C5 scans 37/38/39 concurrently). */

static bool initialized;
static bool synced;
static bool running;
static struct ble_gap_disc_params params;

/* BLE_HCI_ADV_RPT_EVTYPE_* -> advertising PDU type. */
static const uint8_t adv_pdu_type[5] = {0, 1, 6, 2, 4}; /* IND, DIRECT, SCAN, NONCONN, SCAN_RSP */

static int gap_event(struct ble_gap_event *event, void *arg) {
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_DISC: {
        const struct ble_gap_disc_desc *d = &event->disc;
        uint8_t pdu[2 + 6 + 255];
        unsigned plen = 6 + d->length_data;
        unsigned type = d->event_type < 5 ? adv_pdu_type[d->event_type] : 0;
        unsigned txadd = d->addr.type == BLE_ADDR_RANDOM || d->addr.type == BLE_ADDR_RANDOM_ID;
        pdu[0] = (uint8_t)(type | (txadd << 6));
        pdu[1] = (uint8_t)plen;
        memcpy(pdu + 2, d->addr.val, 6);
        memcpy(pdu + 8, d->data, d->length_data);
        mon_frame_push(MON_TYPE_BLE, pdu, (uint16_t)(2 + plen), d->rssi, 37);
        break;
    }
    case BLE_GAP_EVENT_DISC_COMPLETE:
        running = false;
        break;
    default:
        break;
    }
    return 0;
}

static void host_task(void *param) {
    (void)param;
    nimble_port_run();
}

/* Host-controller sync, and resync after a controller reset: keep scanning
 * only while the monitor is on. */
static void on_sync(void) {
    synced = true;
    if (running) ble_gap_disc(BLE_OWN_ADDR_PUBLIC, BLE_HS_FOREVER, &params, gap_event, NULL);
}

bool ble_monitor_start(unsigned itvl, unsigned window, char *err, size_t err_size) {
    if (running) {
        snprintf(err, err_size, "busy");
        return false;
    }
    if (itvl < 4 || itvl > 0x4000 || window < 4 || window > itvl) {
        snprintf(err, err_size, "scan_args");
        return false;
    }
    if (!initialized) {
        if (nimble_port_init() != ESP_OK) {
            snprintf(err, err_size, "bt_init");
            return false;
        }
        ble_hs_cfg.sync_cb = on_sync;
        nimble_port_freertos_init(host_task);
        initialized = true;
    }
    params = (struct ble_gap_disc_params){
        .itvl = (uint16_t)itvl,
        .window = (uint16_t)window,
        .filter_policy = BLE_HCI_SCAN_FILT_NO_WL,
        .limited = 0,
        .passive = 1,
        .filter_duplicates = 0,
    };
    running = true;
    /* Before the first sync the start is deferred to on_sync(). */
    if (synced && ble_gap_disc(BLE_OWN_ADDR_PUBLIC, BLE_HS_FOREVER, &params, gap_event, NULL) != 0) {
        running = false;
        snprintf(err, err_size, "scan_start");
        return false;
    }
    return true;
}

void ble_monitor_stop(void) {
    if (!running) return;
    running = false;
    if (synced) ble_gap_disc_cancel();
}
