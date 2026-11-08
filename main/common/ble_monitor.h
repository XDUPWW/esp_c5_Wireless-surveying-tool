#pragma once
#include <stdbool.h>
#include <stddef.h>

/* BLE passive-scan monitor (observer only; no SCAN_REQ is sent). itvl and
 * window are in 0.625 ms units. The controller and NimBLE host stay
 * initialized after the first start; only the discovery procedure toggles. */
bool ble_monitor_start(unsigned itvl, unsigned window, char *err, size_t err_size);
void ble_monitor_stop(void);
