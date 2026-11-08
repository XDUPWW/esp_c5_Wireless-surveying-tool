/* Continuous gapless complex baseband via the modem's IQ estimator,
 * ported from cyber-murmel/esp-sdr-zephyr (esp_sdr_paced.c). The C5 has no
 * circular dump-engine path (its SRAM bank ownership cannot ping-pong), so
 * gapless sampling uses the estimator instead: hardware sums the 80 MHz
 * baseband samples continuously, and differencing the running sums at the
 * output cadence yields boxcar-decimated I/Q.
 *
 * Architecture note: the estimator window is at most 32767 input samples
 * (~409 us), so it must be serviced at the output cadence. A CPU-spinning
 * task cannot do that here: at priority above the command loop it starves
 * the IDLE task (task watchdog panic) and the loop itself (IQS OFF would be
 * undeliverable); vTaskDelay(1) quantizes to 1 ms and misses windows. So a
 * microsecond esp_timer ISR paces, reads and re-arms the estimator into a
 * lock-free ring, and a plain task drains 256-sample blocks to the shared
 * monitor worker for output. */
#include "iq_stream.h"

#include <stdio.h>
#include <string.h>

#include "esp_cpu.h"
#include "esp_attr.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mode_manager.h"

/* C5 IQ estimator registers (esp-sdr-zephyr esp_sdr_priv.h; confirmed
 * against libphy phy_iq_est_enable_new / phy_dc_iq_est_new). */
#define IQE_CTRL0_REG  0x600a044cu
#define IQE_CTRL0_MODE 0x0c000000u /* bits 27:26 */
#define IQE_CTRL1_REG  0x600a0450u
#define IQE_CTRL1_SRC  0x00180000u /* bits 20:19 */
#define IQE_CTRL1_CNT  0x0001fffcu /* bits 16:2  */
#define IQE_CTRL1_START 0x2u
#define IQE_CTRL1_EN   0x1u
#define IQE_SRC_IQ     2u
#define IQE_SUM_I_REG  0x600a0464u
#define IQE_SUM_Q_REG  0x600a0468u
#define IQE_COUNT_MAX  32768u
/* An estimator run (IQE_COUNT_MAX inputs at 80 MHz) lasts ~409 us; re-arm
 * at ~350 us. Fires are at the esp_timer floor of 50 us, so the re-arm
 * cadence is in fires, independent of the output rate. */
#define IQE_REARM_FIRES 7u

#define IQ_CPU_HZ      240000000u
#define IQ_INPUT_HZ    80000000u
#define IQ_BLOCK       128u        /* samples per output block (int32 pairs, 1024 B) */
#define IQ_RATE_MIN    10000u
#define IQ_RATE_MAX    80000u      /* transport-bound; the 20 kfires/s floor is
                                    * handled by owed-sampling */
#define IQ_RING_PAIRS  4096u       /* power of two, >= 2 blocks */
#define IQ_FIRE_HZ     20000u      /* esp_timer floor: 50 us periodic */
#define IQ_FIRE_PERIOD_US (1000000u / IQ_FIRE_HZ)

static TaskHandle_t task;
static SemaphoreHandle_t done;
static volatile bool stop_req;
static bool running;
static unsigned rate_hz;
static uint64_t stats_samples;
static uint32_t stats_late, stats_events, stats_dropped;

static void iq_task(void *arg);

/* Lock-free ring: single producer (ISR), single consumer (task). Lives in
 * PSRAM: internal SRAM would overflow the RF-dump bank guard, and
 * systimer-ISR access to PSRAM is cache-backed and fast enough here. */
static EXT_RAM_BSS_ATTR int32_t ring[IQ_RING_PAIRS * 2];
static volatile unsigned ring_head; /* pairs written */
static unsigned ring_tail;          /* pairs read */

static uint32_t iq_ctrl1;           /* estimator config snapshot */
static unsigned iq_left;            /* fires until re-arm */
static int32_t iq_pi, iq_pq;
static unsigned iq_owed_acc;        /* fractional-sample accumulator */
static unsigned long long isr_late_total, isr_late_max;

extern void sdr_ensure_rx(void);

static inline uint32_t cycles(void) { return esp_cpu_get_cycle_count(); }

static void iqe_write(uint32_t reg, uint32_t val) {
    *(volatile uint32_t *)reg = val;
}
static uint32_t iqe_read(uint32_t reg) { return *(volatile uint32_t *)reg; }

static void iqe_start(void) {
    iqe_write(IQE_CTRL1_REG, iq_ctrl1 | IQE_CTRL1_EN);
    iqe_write(IQE_CTRL1_REG, iq_ctrl1 | IQE_CTRL1_EN | IQE_CTRL1_START);
}

/* systimer ISR (ESP_TIMER_ISR dispatch). Fires at the 50 us esp_timer
 * floor; the requested rate is synthesized: each fire emits k = accumulated
 * owed samples, each holding the window average (the estimator integrates
 * continuously, so the average is exact; per-sample detail inside a fire
 * window is not resolvable above 20 kS/s). */
static void iq_isr(void *arg) {
    (void)arg;
    uint32_t now = cycles();
    static uint32_t last;
    uint32_t late = now - last;
    last = now;
    const uint32_t fire_step = IQ_CPU_HZ / IQ_FIRE_HZ; /* 50 us nominal */
    if (late > fire_step) {
        isr_late_total += late / fire_step;
        if (late > isr_late_max) isr_late_max = late;
    }
    int32_t si = (int32_t)iqe_read(IQE_SUM_I_REG) >> 6;
    int32_t q1 = (int32_t)iqe_read(IQE_SUM_Q_REG) >> 6;
    int32_t sq = (q1 + ((int32_t)iqe_read(IQE_SUM_Q_REG) >> 6)) / 2;
    int32_t di = si - iq_pi;
    int32_t dq = sq - iq_pq;
    iq_pi = si;
    iq_pq = sq;

    iq_owed_acc += rate_hz;
    unsigned k = iq_owed_acc / IQ_FIRE_HZ;
    iq_owed_acc %= IQ_FIRE_HZ;
    if (k == 0) return;
    int32_t vi = di / (int32_t)k;
    int32_t vq = dq / (int32_t)k;
    unsigned space = IQ_RING_PAIRS - (ring_head - ring_tail);
    if (k > space) {
        stats_dropped += k - space;
        k = space;
    }
    for (unsigned n = 0; n < k; n++) {
        unsigned h = ring_head & (IQ_RING_PAIRS - 1);
        ring[h * 2] = vi;
        ring[h * 2 + 1] = vq;
        ring_head++;
    }
    stats_samples += k;
    if (--iq_left == 0) {
        /* Restart before the 32767-input window expires; the next diff
         * starts from zero again. */
        iqe_start();
        iq_pi = 0;
        iq_pq = 0;
        iq_left = IQE_REARM_FIRES;
        stats_events++;
    }
}

static void iq_configure(void) {
    iq_ctrl1 = (iqe_read(IQE_CTRL1_REG) & ~(IQE_CTRL1_SRC | IQE_CTRL1_CNT |
                                            IQE_CTRL1_START | IQE_CTRL1_EN)) |
               (IQE_SRC_IQ << 19) | (((IQE_COUNT_MAX - 1u) << 2) & IQE_CTRL1_CNT);
    uint32_t c0 = iqe_read(IQE_CTRL0_REG);
    iqe_write(IQE_CTRL0_REG, (c0 & ~IQE_CTRL0_MODE) | (1u << 26));
    iqe_write(IQE_CTRL1_REG, iq_ctrl1);
    iqe_write(IQE_CTRL1_REG, iq_ctrl1 | IQE_CTRL1_EN);
    esp_rom_delay_us(1);
    iqe_write(IQE_CTRL1_REG, iq_ctrl1 | IQE_CTRL1_EN | IQE_CTRL1_START);
}

static void iq_shutdown_regs(void) {
    iqe_write(IQE_CTRL1_REG, iq_ctrl1 | IQE_CTRL1_EN);
    esp_rom_delay_us(1);
    iqe_write(IQE_CTRL1_REG, iq_ctrl1);
}

bool iq_stream_start(unsigned want_hz, char *err, size_t err_size) {
    if (running) {
        snprintf(err, err_size, "busy");
        return false;
    }
    if (want_hz < IQ_RATE_MIN || want_hz > IQ_RATE_MAX ||
        IQ_CPU_HZ % want_hz != 0 || IQ_INPUT_HZ % want_hz != 0) {
        snprintf(err, err_size, "rate_args");
        return false;
    }
    if (!done) done = xSemaphoreCreateBinary();
    sdr_ensure_rx();
    rate_hz = want_hz;
    iq_left = IQE_REARM_FIRES;
    iq_pi = iq_pq = 0;
    iq_owed_acc = 0;
    ring_head = ring_tail = 0;
    isr_late_total = isr_late_max = 0;
    stats_samples = 0;
    stats_late = stats_events = stats_dropped = 0;
    stop_req = false;
    iq_configure();
    if (xTaskCreate(iq_task, "iq_stream", 4096, NULL, 8, &task) != pdPASS) {
        iq_shutdown_regs();
        snprintf(err, err_size, "task");
        return false;
    }
    running = true;
    return true;
}

void iq_stream_stop(void) {
    if (!running) return;
    stop_req = true;
    xSemaphoreTake(done, pdMS_TO_TICKS(1500));
    running = false;
}

bool iq_stream_running(void) { return running; }

/* One-shot debug probe: run the estimator enable sequence in the caller's
 * (serve loop) context, sample once, and shut it down. Replies with the raw
 * sums; used to bisect register-access faults outside the streaming path. */
bool iq_stream_debug(char *reply, size_t size) {
    iq_configure();
    esp_rom_delay_us(200);
    uint32_t si = iqe_read(IQE_SUM_I_REG);
    uint32_t sq = iqe_read(IQE_SUM_Q_REG);
    uint32_t sc = iqe_read(0x600a046cu);
    iq_shutdown_regs();
    snprintf(reply, size, "IQDBG %lu %lu %lu", si, sq, sc);
    return true;
}

unsigned iq_stream_rate(void) { return rate_hz; }

void iq_stream_stats(char *buf, size_t size) {
    stats_late = (uint32_t)isr_late_total;
    snprintf(buf, size, "IQS %u %llu %lu %lu %lu", rate_hz,
             (unsigned long long)stats_samples, stats_late, stats_events,
             stats_dropped);
}

/* Drains the ring into 256-sample int16 blocks handed to the monitor
 * worker. Sleeps between drains, so the command loop and IDLE always run. */
static void iq_task(void *arg) {
    (void)arg;
    const esp_timer_create_args_t targs = {.callback = iq_isr, .arg = NULL,
                                           .name = "iq_pace",
                                           .dispatch_method = ESP_TIMER_ISR};
    esp_timer_handle_t timer;
    int32_t block[IQ_BLOCK * 2];
    unsigned used = 0;
    if (esp_timer_create(&targs, &timer) != ESP_OK) goto out;
    if (esp_timer_start_periodic(timer, IQ_FIRE_PERIOD_US) != ESP_OK) {
        esp_timer_delete(timer);
        goto out;
    }
    while (!stop_req) {
        vTaskDelay(pdMS_TO_TICKS(4));
        for (;;) {
            unsigned head = ring_head; /* single producer: snapshot is safe */
            if (ring_tail == head) break;
            unsigned idx = ring_tail & (IQ_RING_PAIRS - 1);
            block[used * 2] = ring[idx * 2];
            block[used * 2 + 1] = ring[idx * 2 + 1];
            ring_tail++;
            if (++used == IQ_BLOCK) {
                if (!mon_frame_push(MON_TYPE_IQ, block, sizeof(block), 0, 0))
                    stats_dropped += IQ_BLOCK;
                used = 0;
            }
        }
    }
    esp_timer_stop(timer);
    esp_timer_delete(timer);
    /* Flush a partial tail block. */
    if (used) {
        unsigned head = ring_head;
        while (ring_tail != head) { /* drain the rest into the tail block */
            unsigned idx = ring_tail & (IQ_RING_PAIRS - 1);
            if (used < IQ_BLOCK) {
                block[used * 2] = ring[idx * 2];
                block[used * 2 + 1] = ring[idx * 2 + 1];
                used++;
            }
            ring_tail++;
        }
        memset(&block[used * 2], 0, sizeof(block) - used * 2 * sizeof(int32_t));
        if (!mon_frame_push(MON_TYPE_IQ, block, sizeof(block), 0, 0))
            stats_dropped += used;
    }
out:
    iq_shutdown_regs();
    xSemaphoreGive(done);
    vTaskDelete(NULL);
}
