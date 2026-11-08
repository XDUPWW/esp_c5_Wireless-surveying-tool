#include "mode_manager.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "burst_serial.h"
#include "esp_attr.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "ble_monitor.h"
#include "iq_stream.h"
#include "wifi_monitor.h"

/* The pool lives in PSRAM on the N16R8 (see sdkconfig.defaults.esp32c5):
 * monitor frames must not push internal BSS past the RF-dump bank guard. */
static EXT_RAM_BSS_ATTR mon_frame_t pool[MON_POOL_SIZE];
static QueueHandle_t free_q;
static QueueHandle_t work_q;
static bool wifi_on;
static bool ble_on;
static bool iq_on;
static unsigned drops_wifi;
static unsigned drops_iq;
static unsigned drops_ble;
/* Bounded TX wait for monitor frames: backpressure becomes a dropped frame
 * instead of a blocked command channel. */
#define MON_TX_TIMEOUT_MS 150u
static unsigned tx_fail;

static void mon_send(const char *text) { burst_serial_send(text, strlen(text)); }

/* Single writer, so the header and payload below always travel in one
 * burst_serial_send transfer and cannot interleave with a text reply. */
static void worker_main(void *arg) {
    static EXT_RAM_BSS_ATTR uint8_t tx[48 + MON_FRAME_MAX];
    (void)arg;
    for (;;) {
        mon_frame_t *frame;
        xQueueReceive(work_q, &frame, portMAX_DELAY);
        uint32_t crc = esp_rom_crc32_le(0, frame->data, frame->len);
        int header;
        if (frame->type == MON_TYPE_WIFI) {
            header = snprintf((char *)tx, sizeof(tx), "WIFIFRAME %u %d %u %08" PRIx32 "\n",
                              (unsigned)frame->len, (int)frame->rssi, (unsigned)frame->channel, crc);
        } else if (frame->type == MON_TYPE_IQ) {
            header = snprintf((char *)tx, sizeof(tx), "IQFRAME %u %u %08" PRIx32 "\n",
                              (unsigned)frame->len, iq_stream_rate(), crc);
        } else {
            header = snprintf((char *)tx, sizeof(tx), "BLEFRAME %u %d %u %08" PRIx32 "\n",
                              (unsigned)frame->len, (int)frame->rssi, (unsigned)frame->channel, crc);
        }
        if (header > 0 && (size_t)header < sizeof(tx)) {
            memcpy(tx + header, frame->data, frame->len);
            /* Bounded wait: a host that stops draining must lose frames, not
             * wedge the command channel (text replies keep the long path). */
            int64_t t0 = esp_timer_get_time();
            if (!burst_serial_send_timeout(tx, (size_t)header + frame->len,
                                           MON_TX_TIMEOUT_MS))
                tx_fail++;
            else if (esp_timer_get_time() - t0 > 2000)
                /* The send had to wait for the bus. This task outranks the
                 * command loop, so without a yield it re-takes tx_mutex first
                 * every time and a pending text reply starves - with a host
                 * that stopped reading, that starved the loop that drains the
                 * RX FIFO and the session wedged. */
                vTaskDelay(1);
        }
        xQueueSend(free_q, &frame, 0);
    }
}

void mode_manager_init(void) {
    free_q = xQueueCreate(MON_POOL_SIZE, sizeof(mon_frame_t *));
    work_q = xQueueCreate(MON_POOL_SIZE, sizeof(mon_frame_t *));
    for (unsigned j = 0; j < MON_POOL_SIZE; j++) {
        mon_frame_t *slot = &pool[j];
        xQueueSend(free_q, &slot, 0);
    }
    xTaskCreate(worker_main, "mon_tx", 4096, NULL, 8, NULL);
}

bool mon_frame_push(mon_type_t type, const void *data, uint16_t len, int8_t rssi, uint8_t channel) {
    mon_frame_t *frame;
    bool dropped = false;
    if (xQueueReceive(free_q, &frame, 0) != pdTRUE) dropped = true;
    if (!dropped) {
        frame->type = (uint8_t)type;
        frame->channel = channel;
        frame->rssi = rssi;
        frame->len = len;
        memcpy(frame->data, data, len);
        if (xQueueSend(work_q, &frame, 0) != pdTRUE) {
            xQueueSend(free_q, &frame, 0);
            dropped = true;
        }
    }
    if (dropped) {
        if (type == MON_TYPE_WIFI) drops_wifi++;
        else if (type == MON_TYPE_BLE) drops_ble++;
        else drops_iq++;
    }
    return !dropped;
}

bool mode_manager_busy(void) { return wifi_on || ble_on || iq_on; }

/* Both monitors off: the PHY may have been reconfigured by the protocol
 * stacks, so the next snapshot must rebuild the RX test state. */
static void mon_idle_check(void) {
    if (!wifi_on && !ble_on) sdr_rx_invalidate();
}

bool mode_manager_command(const char *line) {
    char answer[96];
    unsigned a, b;
    char extra;
    if (!strcmp(line, "MON?")) {
        snprintf(answer, sizeof(answer), "MON WIFI:%s BLE:%s IQ:%s CH:%u DROPS:%u/%u\n",
                 wifi_on ? "on" : "off", ble_on ? "on" : "off",
                 iq_stream_running() ? "on" : "off", wifi_monitor_channel(),
                 drops_wifi, drops_ble);
        mon_send(answer);
        return true;
    }
    if (!strcmp(line, "IQDBG")) {
        iq_stream_debug(answer, sizeof(answer));
        /* TX drops are not part of the documented status lines; surface them
         * on this debug-only probe so backpressure stays diagnosable. */
        size_t used = strlen(answer);
        snprintf(answer + used, sizeof(answer) - used, " txfail=%u iqdrop=%u\n",
                 tx_fail, drops_iq);
        mon_send(answer);
        return true;
    }
    if (!strncmp(line, "IQS ON ", 7)) {
        unsigned rate = 0;
        char extra;
        if (sscanf(line + 7, "%u %c", &rate, &extra) != 1 || rate == 0) {
            mon_send("ERR iqs_args\n");
            return true;
        }
        char err[24] = "";
        if (!iq_stream_start(rate, err, sizeof(err))) {
            snprintf(answer, sizeof(answer), "ERR %s\n", err[0] ? err : "iqs_start");
            mon_send(answer);
            return true;
        }
        iq_on = true;
        mon_send("OK\n");
        return true;
    }
    if (!strcmp(line, "IQS OFF")) {
        iq_on = false;
        iq_stream_stop();
        mon_idle_check();
        mon_send("OK\n");
        return true;
    }
    if (!strcmp(line, "IQS?")) {
        iq_stream_stats(answer, sizeof(answer));
        strncat(answer, "\n", sizeof(answer) - strlen(answer) - 1);
        mon_send(answer);
        return true;
    }
    if (!strncmp(line, "MON WIFI ON ", 12)) {
        int fields = sscanf(line + 12, "%u %u %c", &a, &b, &extra);
        if (fields < 1 || fields > 2) {
            mon_send("ERR mon_args\n");
            return true;
        }
        char err[24] = "";
        if (!wifi_monitor_start(a, fields == 2 ? b : 0, err, sizeof(err))) {
            snprintf(answer, sizeof(answer), "ERR %s\n", err[0] ? err : "wifi_start");
            mon_send(answer);
            return true;
        }
        wifi_on = true;
        mon_send("OK\n");
        return true;
    }
    if (!strcmp(line, "MON WIFI OFF")) {
        wifi_on = false;
        wifi_monitor_stop();
        mon_idle_check();
        mon_send("OK\n");
        return true;
    }
    if (!strcmp(line, "MON BLE ON") || !strncmp(line, "MON BLE ON ", 11)) {
        a = 0x50;
        b = 0x30;
        if (line[10] != '\0') {
            int fields = sscanf(line + 10, " %u %u %c", &a, &b, &extra);
            if (fields == 0 || fields > 2) {
                mon_send("ERR mon_args\n");
                return true;
            }
        }
        char err[24] = "";
        if (!ble_monitor_start(a, b, err, sizeof(err))) {
            snprintf(answer, sizeof(answer), "ERR %s\n", err[0] ? err : "ble_start");
            mon_send(answer);
            return true;
        }
        ble_on = true;
        mon_send("OK\n");
        return true;
    }
    if (!strcmp(line, "MON BLE OFF")) {
        ble_on = false;
        ble_monitor_stop();
        mon_idle_check();
        mon_send("OK\n");
        return true;
    }
    if (!strncmp(line, "MON", 3) && (line[3] == '\0' || line[3] == ' ')) {
        mon_send("ERR command\n");
        return true;
    }
    return false;
}
