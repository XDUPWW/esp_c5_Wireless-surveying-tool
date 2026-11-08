#include "wifi_monitor.h"

#include <stdio.h>
#include <string.h>

#include "esp_err.h"
#include "esp_timer.h"
#include "esp_wifi.h"

#include "mode_manager.h"

static bool running;
static unsigned cur_channel;
static unsigned hop_ms;
static bool hop_5g;
static unsigned hop_idx;
static esp_timer_handle_t hop_timer;

/* UNII hop set (channel numbers, no DFS dwell logic: sniffer only listens). */
const unsigned WIFI_HOP_5G[] = {36, 40, 44, 48, 52, 56, 60, 64,
                                100, 104, 108, 112, 116, 120, 124, 128,
                                132, 136, 140, 144, 149, 153, 157, 161, 165};
const unsigned WIFI_HOP_2G_COUNT = 13;
#define WIFI_HOP_5G_COUNT (sizeof(WIFI_HOP_5G) / sizeof(WIFI_HOP_5G[0]))

static bool channel_valid(unsigned ch) {
    return (ch >= 1 && ch <= 13) || (ch >= 36 && ch <= 177);
}

/* Radiotap header prepended to every frame so the payload drops straight into
 * a pcap (LINKTYPE_IEEE802_11_RADIO). Minimal field set: Flags, Rate, Channel,
 * dBm Antenna Signal. The 802.11 frame keeps its FCS (flag bit 0x02). */
#define MON_RTAP_LEN    15u
#define MON_RTAP_PRESENT 0x0000002Eu /* bit1 Flags, bit2 Rate, bit3 Channel, bit5 dBm AntSig */

static unsigned channel_freq_mhz(unsigned channel) {
    if (channel == 14) return 2484;
    if (channel < 14) return 2407 + 5 * channel;
    return 5000 + 5 * channel; /* 5 GHz channel numbers */
}

static unsigned mon_frame_max(void) { return MON_FRAME_MAX - MON_RTAP_LEN; }

/* Wi-Fi task context: copy the frame into the shared pool and return.
 * Nothing here may block, allocate, or print. */
static void rx_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
    (void)type;
    if (!running) return;
    const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t *)buf;
    const wifi_pkt_rx_ctrl_t *rx = &pkt->rx_ctrl;
    /* rx_ctrl.channel is a 4-bit field and cannot name 5 GHz channels, so
     * the radiotap/header channel always comes from the locked channel. */
    unsigned ch = cur_channel ? cur_channel : rx->channel;
    unsigned len = rx->sig_len;
    if (len > mon_frame_max()) len = mon_frame_max();
    uint8_t blob[MON_FRAME_MAX];
    uint8_t *rt = blob;
    rt[0] = 0; /* version */
    rt[1] = 0; /* pad */
    rt[2] = MON_RTAP_LEN;
    rt[3] = 0;
    rt[4] = MON_RTAP_PRESENT;
    rt[5] = rt[6] = rt[7] = 0;
    rt[8] = 0x02; /* Flags: FCS at end */
    rt[9] = rx->cur_bb_format == RX_BB_FORMAT_11B ? rx->rate : 0; /* legacy rate, 500 kbit/s units */
    unsigned freq = channel_freq_mhz(ch);
    rt[10] = freq;
    rt[11] = freq >> 8;
    unsigned chflags = (freq > 3000 ? 0x0100 : 0x0080) |
                       (rx->cur_bb_format == RX_BB_FORMAT_11B ? 0x0020 : 0x0040);
    rt[12] = chflags;
    rt[13] = chflags >> 8;
    rt[14] = (uint8_t)rx->rssi;
    memcpy(blob + MON_RTAP_LEN, pkt->payload, len);
    mon_frame_push(MON_TYPE_WIFI, blob, (uint16_t)(MON_RTAP_LEN + len), rx->rssi, (uint8_t)ch);
}

static bool set_channel(unsigned channel, char *err, size_t err_size) {
    if (esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE) != ESP_OK) {
        snprintf(err, err_size, "channel");
        return false;
    }
    cur_channel = channel;
    return true;
}

static void hop_cb(void *arg) {
    (void)arg;
    if (!running || !hop_ms) return;
    unsigned next;
    if (hop_5g) {
        hop_idx = (hop_idx + 1) % WIFI_HOP_5G_COUNT;
        next = WIFI_HOP_5G[hop_idx];
    } else {
        next = cur_channel >= 13 ? 1 : cur_channel + 1;
    }
    esp_wifi_set_promiscuous(false);
    if (esp_wifi_set_channel(next, WIFI_SECOND_CHAN_NONE) == ESP_OK) cur_channel = next;
    esp_wifi_set_promiscuous(true);
}

bool wifi_monitor_start(unsigned channel, unsigned hop, char *err, size_t err_size) {
    if (running) {
        snprintf(err, err_size, "busy");
        return false;
    }
    /* 2.4 GHz 1-13 or 5 GHz 36-177; esp_wifi_set_channel validates further
     * against the regulatory country table. */
    if (!channel_valid(channel)) {
        snprintf(err, err_size, "channel_range");
        return false;
    }
    esp_wifi_set_promiscuous(false);
    if (!set_channel(channel, err, err_size)) return false;
    const wifi_promiscuous_filter_t filter = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA |
                       WIFI_PROMIS_FILTER_MASK_CTRL,
    };
    const wifi_promiscuous_filter_t ctrl_filter = {.filter_mask = WIFI_PROMIS_CTRL_FILTER_MASK_ALL};
    if (esp_wifi_set_promiscuous_filter(&filter) != ESP_OK ||
        esp_wifi_set_promiscuous_ctrl_filter(&ctrl_filter) != ESP_OK ||
        esp_wifi_set_promiscuous_rx_cb(rx_cb) != ESP_OK) {
        snprintf(err, err_size, "wifi_filter");
        return false;
    }
    running = true;
    hop_ms = hop;
    hop_5g = channel >= 36;
    hop_idx = 0;
    while (hop_5g && hop_idx < WIFI_HOP_5G_COUNT - 1 && WIFI_HOP_5G[hop_idx] != channel)
        hop_idx++;
    if (hop_ms) {
        const esp_timer_create_args_t args = {.callback = hop_cb, .arg = NULL, .name = "mon_hop"};
        if (esp_timer_create(&args, &hop_timer) == ESP_OK)
            esp_timer_start_periodic(hop_timer, (uint64_t)hop_ms * 1000);
    }
    esp_wifi_set_promiscuous(true);
    return true;
}

void wifi_monitor_stop(void) {
    running = false;
    hop_ms = 0;
    if (hop_timer) {
        esp_timer_stop(hop_timer);
        esp_timer_delete(hop_timer);
        hop_timer = NULL;
    }
    esp_wifi_set_promiscuous_rx_cb(NULL);
    /* Back to the boot-time steady state the SDR dump path expects:
     * promiscuous on with no callback. */
    esp_wifi_set_promiscuous(true);
}

unsigned wifi_monitor_channel(void) { return running ? cur_channel : 0; }
