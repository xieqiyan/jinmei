#include "config_store.h"

#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define CONFIG_LINE_MAX 96

typedef enum {
    CONFIG_PARAM_NODE_NO = 1,
    CONFIG_PARAM_BUSINESS_IP,
    CONFIG_PARAM_RADIO_FREQ,
    CONFIG_PARAM_RADIO_POWER,
    CONFIG_PARAM_WORK_FREQ_MODE,
    CONFIG_PARAM_RADIO_RATE,
    CONFIG_PARAM_HOST_PRIME
} ConfigParam;

static int validate_u8(uint32_t value)
{
    return value <= UINT8_MAX;
}

static int validate_u16(uint32_t value)
{
    return value <= UINT16_MAX;
}

static int validate_param_value(ConfigParam param, uint32_t value)
{
    switch (param) {
    case CONFIG_PARAM_NODE_NO:
    case CONFIG_PARAM_HOST_PRIME:
        return validate_u8(value);
    case CONFIG_PARAM_BUSINESS_IP:
        return value != 0;
    case CONFIG_PARAM_RADIO_FREQ:
        return validate_u16(value);
    case CONFIG_PARAM_RADIO_POWER:
        return value <= 3u;
    case CONFIG_PARAM_WORK_FREQ_MODE:
        return value <= 2u;
    case CONFIG_PARAM_RADIO_RATE:
        return value <= 3u;
    default:
        return 0;
    }
}

static const char *param_name(ConfigParam param)
{
    switch (param) {
    case CONFIG_PARAM_NODE_NO:
        return "nodeNo";
    case CONFIG_PARAM_BUSINESS_IP:
        return "businessIp";
    case CONFIG_PARAM_RADIO_FREQ:
        return "radioFreq";
    case CONFIG_PARAM_RADIO_POWER:
        return "radioPower";
    case CONFIG_PARAM_WORK_FREQ_MODE:
        return "workFreqMode";
    case CONFIG_PARAM_RADIO_RATE:
        return "radioRate";
    case CONFIG_PARAM_HOST_PRIME:
        return "hostPrime";
    default:
        return NULL;
    }
}

static int parse_param_name(const char *name, ConfigParam *out_param)
{
    ConfigParam param;

    if (name == NULL || out_param == NULL) {
        return CONFIG_ERR_ARG;
    }

    for (param = CONFIG_PARAM_NODE_NO; param <= CONFIG_PARAM_HOST_PRIME; param++) {
        const char *candidate = param_name(param);

        if (candidate != NULL && strcmp(name, candidate) == 0) {
            *out_param = param;
            return CONFIG_OK;
        }
    }

    return CONFIG_ERR_PARSE;
}

static int config_set_param(RadioConfig *config, ConfigParam param, uint32_t value)
{
    if (config == NULL) {
        return CONFIG_ERR_ARG;
    }
    if (!validate_param_value(param, value)) {
        return CONFIG_ERR_RANGE;
    }

    switch (param) {
    case CONFIG_PARAM_NODE_NO:
        config->nodeNo = (uint8_t)value;
        return CONFIG_OK;
    case CONFIG_PARAM_BUSINESS_IP:
        config->businessIp = value;
        return CONFIG_OK;
    case CONFIG_PARAM_RADIO_FREQ:
        config->radioFreq = (uint16_t)value;
        return CONFIG_OK;
    case CONFIG_PARAM_RADIO_POWER:
        config->radioPower = (uint8_t)value;
        return CONFIG_OK;
    case CONFIG_PARAM_WORK_FREQ_MODE:
        config->workFreqMode = (uint8_t)value;
        return CONFIG_OK;
    case CONFIG_PARAM_RADIO_RATE:
        config->radioRate = (uint8_t)value;
        return CONFIG_OK;
    case CONFIG_PARAM_HOST_PRIME:
        config->hostPrime = (uint8_t)value;
        return CONFIG_OK;
    default:
        return CONFIG_ERR_ARG;
    }
}

static int parse_uint32(const char *text, uint32_t *out_value)
{
    char *end = NULL;
    unsigned long value;

    if (text == NULL || out_value == NULL || *text == '\0') {
        return CONFIG_ERR_PARSE;
    }

    errno = 0;
    value = strtoul(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value > UINT32_MAX) {
        return CONFIG_ERR_PARSE;
    }

    *out_value = (uint32_t)value;
    return CONFIG_OK;
}

static int parse_ipv4(const char *text, uint32_t *out_value)
{
    struct in_addr addr;

    if (text == NULL || out_value == NULL || *text == '\0') {
        return CONFIG_ERR_PARSE;
    }
    if (inet_pton(AF_INET, text, &addr) != 1 || addr.s_addr == 0) {
        return CONFIG_ERR_PARSE;
    }

    *out_value = addr.s_addr;
    return CONFIG_OK;
}

static int format_ipv4(uint32_t value, char *out_text, size_t out_size)
{
    struct in_addr addr;

    if (out_text == NULL || out_size == 0 || value == 0) {
        return CONFIG_ERR_ARG;
    }

    addr.s_addr = value;
    if (inet_ntop(AF_INET, &addr, out_text, out_size) == NULL) {
        return CONFIG_ERR_PARSE;
    }

    return CONFIG_OK;
}

int config_read_all(RadioConfig *out_config)
{
    char line[CONFIG_LINE_MAX];
    int seen[CONFIG_PARAM_HOST_PRIME + 1] = {0};
    FILE *file;
    int result;

    if (out_config == NULL) {
        return CONFIG_ERR_ARG;
    }

    file = fopen(CONFIG_FILE_PATH, "r");
    if (file == NULL) {
        return CONFIG_ERR_IO;
    }

    memset(out_config, 0, sizeof(*out_config));

    while (fgets(line, sizeof(line), file) != NULL) {
        char *newline = strchr(line, '\n');
        char *equals;
        ConfigParam param;
        uint32_t value;

        if (newline != NULL) {
            *newline = '\0';
        }
        if (line[0] == '\0') {
            continue;
        }

        equals = strchr(line, '=');
        if (equals == NULL) {
            fclose(file);
            return CONFIG_ERR_PARSE;
        }
        *equals = '\0';

        result = parse_param_name(line, &param);
        if (result != CONFIG_OK) {
            fclose(file);
            return result;
        }

        if (param == CONFIG_PARAM_BUSINESS_IP) {
            result = parse_ipv4(equals + 1, &value);
        } else {
            result = parse_uint32(equals + 1, &value);
        }
        if (result != CONFIG_OK) {
            fclose(file);
            return result;
        }

        result = config_set_param(out_config, param, value);
        if (result != CONFIG_OK) {
            fclose(file);
            return result;
        }

        seen[param] = 1;
    }

    if (ferror(file)) {
        fclose(file);
        return CONFIG_ERR_IO;
    }
    fclose(file);

    for (result = CONFIG_PARAM_NODE_NO; result <= CONFIG_PARAM_HOST_PRIME; result++) {
        if (!seen[result]) {
            return CONFIG_ERR_PARSE;
        }
    }

    return CONFIG_OK;
}

/**
 * @brief 递归创建文件路径中的目录部分（类似 mkdir -p）
 * @param filepath 完整文件路径
 * @return CONFIG_OK 成功，CONFIG_ERR_IO 失败
 */
static int ensure_directory_exists(const char *filepath)
{
    char *path = strdup(filepath);
    if (path == NULL) {
        return CONFIG_ERR_IO;
    }

    char *last_slash = strrchr(path, '/');
    if (last_slash == NULL) {
        // 没有目录部分，视为当前目录已存在
        free(path);
        return CONFIG_OK;
    }

    *last_slash = '\0';     // 截断为目录路径
    char *dir = path;

    // 逐级检查并创建
    struct stat st;
    for (char *p = dir + 1; ; p++) {
        if (*p == '/' || *p == '\0') {
            char saved = *p;
            *p = '\0';

            if (stat(dir, &st) != 0) {
                if (mkdir(dir, 0755) != 0 && errno != EEXIST) {
                    free(path);
                    return CONFIG_ERR_IO;
                }
            } else if (!S_ISDIR(st.st_mode)) {
                // 已存在但非目录
                free(path);
                return CONFIG_ERR_IO;
            }

            *p = saved;
            if (saved == '\0') {
                break;
            }
        }
    }

    free(path);
    return CONFIG_OK;
}

int config_write_all(const RadioConfig *config)
{
    FILE *file;
    char business_ip[INET_ADDRSTRLEN];

    if (config == NULL) {
        return CONFIG_ERR_ARG;
    }

    // 确保目录存在，若不存在则自动创建
    if (ensure_directory_exists(CONFIG_FILE_PATH) != CONFIG_OK) {
        return CONFIG_ERR_IO;
    }

    if (!validate_param_value(CONFIG_PARAM_NODE_NO, config->nodeNo) ||
        !validate_param_value(CONFIG_PARAM_BUSINESS_IP, config->businessIp) ||
        !validate_param_value(CONFIG_PARAM_RADIO_FREQ, config->radioFreq) ||
        !validate_param_value(CONFIG_PARAM_RADIO_POWER, config->radioPower) ||
        !validate_param_value(CONFIG_PARAM_WORK_FREQ_MODE, config->workFreqMode) ||
        !validate_param_value(CONFIG_PARAM_RADIO_RATE, config->radioRate) ||
        !validate_param_value(CONFIG_PARAM_HOST_PRIME, config->hostPrime)) {
        return CONFIG_ERR_RANGE;
    }
    if (format_ipv4(config->businessIp, business_ip, sizeof(business_ip)) != CONFIG_OK) {
        return CONFIG_ERR_RANGE;
    }

    file = fopen(CONFIG_FILE_PATH, "w");
    if (file == NULL) {
        return CONFIG_ERR_IO;
    }

    if (fprintf(file,
                "nodeNo=%u\n"
                "businessIp=%s\n"
                "radioFreq=%u\n"
                "radioPower=%u\n"
                "workFreqMode=%u\n"
                "radioRate=%u\n"
                "hostPrime=%u\n",
                (unsigned int)config->nodeNo,
                business_ip,
                (unsigned int)config->radioFreq,
                (unsigned int)config->radioPower,
                (unsigned int)config->workFreqMode,
                (unsigned int)config->radioRate,
                (unsigned int)config->hostPrime) < 0) {
        fclose(file);
        return CONFIG_ERR_IO;
    }

    if (fclose(file) != 0) {
        return CONFIG_ERR_IO;
    }

    return CONFIG_OK;
}

void config_u16_to_be(uint16_t value, uint8_t out_bytes[2])
{
    if (out_bytes == NULL) {
        return;
    }

    out_bytes[0] = (uint8_t)((value >> 8) & 0xffu);
    out_bytes[1] = (uint8_t)(value & 0xffu);
}

uint16_t config_u16_from_be(const uint8_t bytes[2])
{
    if (bytes == NULL) {
        return 0;
    }

    return (uint16_t)(((uint16_t)bytes[0] << 8) | bytes[1]);
}
