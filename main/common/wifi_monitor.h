#pragma once
#include <stdbool.h>
#include <stddef.h>

/* Promiscuous 802.11 monitor. The Wi-Fi stack is already started by
 * app_main in WIFI_MODE_NULL; NULL mode with promiscuous enabled is the IDF
 * sniffer configuration, no station association needed. Channels: 2.4 GHz
 * 1-13 and 5 GHz 36-177 (UNII). Frames arrive in the
 * Wi-Fi task context and are copied into the shared monitor queue. */
bool wifi_monitor_start(unsigned channel, unsigned hop_ms, char *err, size_t err_size);
void wifi_monitor_stop(void);
unsigned wifi_monitor_channel(void);
