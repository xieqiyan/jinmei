#ifndef CONFIG_STORE_H
#define CONFIG_STORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifndef CONFIG_FILE_PATH
#define CONFIG_FILE_PATH "/tmp/radio_test/ckl_config.cfg"
#endif

typedef enum {
    CONFIG_OK = 0,
    CONFIG_ERR_ARG = -1,
    CONFIG_ERR_IO = -2,
    CONFIG_ERR_PARSE = -3,
    CONFIG_ERR_RANGE = -4
} ConfigResult;

typedef struct {
    uint8_t nodeNo;
    uint32_t businessIp;
    uint16_t radioFreq;
    uint8_t radioPower;
    uint8_t workFreqMode;
    uint8_t radioRate;
    uint8_t hostPrime;
} RadioConfig;

int config_read_all(RadioConfig *out_config);
int config_write_all(const RadioConfig *config);

void config_u16_to_be(uint16_t value, uint8_t out_bytes[2]);
uint16_t config_u16_from_be(const uint8_t bytes[2]);

#ifdef __cplusplus
}
#endif

#endif
