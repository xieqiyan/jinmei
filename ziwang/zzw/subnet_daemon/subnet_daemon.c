#define _GNU_SOURCE

#include <arpa/inet.h>
#include <errno.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define DEVICE_NAME "zzw"
#define DAEMON_NAME "zzw_daemon"
#define RADIO_TYPE 0x03
#define CONFIG_FILE_PATH "/tmp/radio_test/zzw_config.cfg"
#define EXTRA_FIELD_NAME "superiorNetNo"

#define RADIO_ETHERTYPE 0x88B5
#define FMT_VERSION 0x02
#define PAYLOAD_LEN 16
#define FRAME_LEN (ETH_HLEN + PAYLOAD_LEN)
#define NEIGHBOR_MAX 64
#define BROADCAST_INTERVAL_MS 1000
#define CONFIG_CHECK_INTERVAL_MS 500
#define NEIGHBOR_TIMEOUT_SEC 3
#define PEER_STATUS_PATH "/tmp/radio_test/peers.json"

typedef enum {
    PEER_REASON_OK = 0,
    PEER_REASON_PARAM_MISMATCH,
    PEER_REASON_NO_MASTER,
    PEER_REASON_MULTI_MASTER
} PeerReason;

typedef struct {
    uint8_t nodeNum;
    uint32_t businessIp;
    uint8_t radioPower;
    uint8_t freqType;
    uint32_t freqScaled;
    uint8_t userRate;
    uint8_t extra;
    uint8_t hostPrime;
} RadioConfig;

typedef struct {
    int used;
    RadioConfig cfg;
    uint8_t src_mac[ETH_ALEN];
    time_t last_seen;
    int reachable;
    PeerReason reason;
} Neighbor;

typedef struct {
    time_t mtime_sec;
    long mtime_nsec;
    off_t size;
    int valid;
} ConfigStat;

static volatile sig_atomic_t g_running = 1;

static void log_msg(const char *level, const char *fmt, ...)
{
    va_list ap;
    char time_buf[32];
    time_t now = time(NULL);
    struct tm tm_now;

    localtime_r(&now, &tm_now);
    strftime(time_buf, sizeof(time_buf), "%Y-%m-%d %H:%M:%S", &tm_now);
    fprintf(stderr, "[%s] [%s] ", time_buf, level);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

static void handle_signal(int sig)
{
    (void)sig;
    g_running = 0;
}

static uint32_t u32_from_be(const uint8_t bytes[4])
{
    return ((uint32_t)bytes[0] << 24) |
           ((uint32_t)bytes[1] << 16) |
           ((uint32_t)bytes[2] << 8) |
           (uint32_t)bytes[3];
}

static void u32_to_be(uint32_t value, uint8_t out[4])
{
    out[0] = (uint8_t)((value >> 24) & 0xff);
    out[1] = (uint8_t)((value >> 16) & 0xff);
    out[2] = (uint8_t)((value >> 8) & 0xff);
    out[3] = (uint8_t)(value & 0xff);
}

static int same_match_params(const RadioConfig *a, const RadioConfig *b)
{
    return a->radioPower == b->radioPower &&
           a->freqType == b->freqType &&
           a->freqScaled == b->freqScaled &&
           a->userRate == b->userRate &&
           a->extra == b->extra;
}

static int same_config(const RadioConfig *a, const RadioConfig *b)
{
    return a->nodeNum == b->nodeNum &&
           a->businessIp == b->businessIp &&
           a->radioPower == b->radioPower &&
           a->freqType == b->freqType &&
           a->freqScaled == b->freqScaled &&
           a->userRate == b->userRate &&
           a->extra == b->extra &&
           a->hostPrime == b->hostPrime;
}

static void encode_config(uint8_t payload[PAYLOAD_LEN], const RadioConfig *cfg)
{
    payload[0] = FMT_VERSION;
    payload[1] = RADIO_TYPE;
    payload[2] = cfg->nodeNum;
    memcpy(&payload[3], &cfg->businessIp, sizeof(cfg->businessIp));
    payload[7] = cfg->radioPower;
    payload[8] = cfg->freqType;
    u32_to_be(cfg->freqScaled, &payload[9]);
    payload[13] = cfg->userRate;
    payload[14] = cfg->extra;
    payload[15] = cfg->hostPrime;
}

static int decode_config(const uint8_t *payload, size_t len, RadioConfig *cfg)
{
    if (payload == NULL || cfg == NULL || len < PAYLOAD_LEN) {
        return -1;
    }
    if (payload[0] != FMT_VERSION || payload[1] != RADIO_TYPE) {
        return -1;
    }
    cfg->nodeNum = payload[2];
    memcpy(&cfg->businessIp, &payload[3], sizeof(cfg->businessIp));
    cfg->radioPower = payload[7];
    cfg->freqType = payload[8];
    cfg->freqScaled = u32_from_be(&payload[9]);
    cfg->userRate = payload[13];
    cfg->extra = payload[14];
    cfg->hostPrime = payload[15];
    return 0;
}

static int run_cmd(char *const argv[])
{
    pid_t pid = fork();
    int status;

    if (pid < 0) {
        return -1;
    }
    if (pid == 0) {
        execvp(argv[0], argv);
        _exit(127);
    }
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            return -1;
        }
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
}

static int format_ipv4(uint32_t value, char *out_text, size_t out_size)
{
    struct in_addr addr;

    if (out_text == NULL || out_size == 0 || value == 0) {
        return -1;
    }
    addr.s_addr = value;
    return inet_ntop(AF_INET, &addr, out_text, out_size) == NULL ? -1 : 0;
}

static void format_freq(uint32_t scaled, char *out_text, size_t out_size)
{
    size_t len;
    snprintf(out_text, out_size, "%u.%05u", scaled / 100000u, scaled % 100000u);
    len = strlen(out_text);
    while (len > 0 && out_text[len - 1] == '0') {
        out_text[--len] = '\0';
    }
    if (len > 0 && out_text[len - 1] == '.') {
        out_text[--len] = '\0';
    }
}

static int get_iface_ipv4(const char *ifname, uint32_t *out_value)
{
    int sock;
    struct ifreq ifr;
    struct sockaddr_in *addr;

    if (ifname == NULL || out_value == NULL) {
        return -1;
    }
    sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        return -1;
    }
    memset(&ifr, 0, sizeof(ifr));
    snprintf(ifr.ifr_name, sizeof(ifr.ifr_name), "%s", ifname);
    if (ioctl(sock, SIOCGIFADDR, &ifr) < 0) {
        close(sock);
        return -1;
    }
    addr = (struct sockaddr_in *)&ifr.ifr_addr;
    *out_value = addr->sin_addr.s_addr;
    close(sock);
    return *out_value == 0 ? -1 : 0;
}

static int parse_u32(const char *text, uint32_t max_value, uint32_t *out_value)
{
    char *end = NULL;
    unsigned long value;

    if (text == NULL || out_value == NULL || *text == '\0') {
        return -1;
    }
    errno = 0;
    value = strtoul(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value > max_value) {
        return -1;
    }
    *out_value = (uint32_t)value;
    return 0;
}

static int parse_freq_scaled(const char *text, uint32_t *out_value)
{
    char *end = NULL;
    double value;
    double scaled;

    if (text == NULL || out_value == NULL || *text == '\0') {
        return -1;
    }
    errno = 0;
    value = strtod(text, &end);
    if (errno != 0 || end == text || *end != '\0' || value < 0.0) {
        return -1;
    }
    scaled = value * 100000.0 + 0.5;
    if (scaled > 4294967295.0) {
        return -1;
    }
    *out_value = (uint32_t)scaled;
    return 0;
}

static int load_config(const char *path, RadioConfig *cfg)
{
    FILE *file;
    char line[128];
    int seen_node = 0, seen_ip = 0, seen_power = 0, seen_freq_type = 0;
    int seen_freq = 0, seen_rate = 0, seen_extra = 0, seen_prime = 0;

    if (path == NULL || cfg == NULL) {
        return -1;
    }
    file = fopen(path, "r");
    if (file == NULL) {
        return -1;
    }
    memset(cfg, 0, sizeof(*cfg));

    while (fgets(line, sizeof(line), file) != NULL) {
        char *newline = strchr(line, '\n');
        char *equals;
        const char *name;
        const char *value;
        uint32_t number;
        struct in_addr addr;

        if (newline != NULL) {
            *newline = '\0';
        }
        if (line[0] == '\0') {
            continue;
        }
        equals = strchr(line, '=');
        if (equals == NULL) {
            fclose(file);
            return -1;
        }
        *equals = '\0';
        name = line;
        value = equals + 1;

        if (strcmp(name, "nodeNum") == 0) {
            if (parse_u32(value, 255, &number) != 0) {
                fclose(file);
                return -1;
            }
            cfg->nodeNum = (uint8_t)number;
            seen_node = 1;
        } else if (strcmp(name, "businessIp") == 0) {
            if (inet_pton(AF_INET, value, &addr) != 1 || addr.s_addr == 0) {
                fclose(file);
                return -1;
            }
            cfg->businessIp = addr.s_addr;
            seen_ip = 1;
        } else if (strcmp(name, "radioPower") == 0) {
            if (parse_u32(value, 9, &number) != 0) {
                fclose(file);
                return -1;
            }
            cfg->radioPower = (uint8_t)number;
            seen_power = 1;
        } else if (strcmp(name, "freqType") == 0) {
            if (parse_u32(value, 2, &number) != 0) {
                fclose(file);
                return -1;
            }
            cfg->freqType = (uint8_t)number;
            seen_freq_type = 1;
        } else if (strcmp(name, "freq") == 0) {
            if (parse_freq_scaled(value, &cfg->freqScaled) != 0) {
                fclose(file);
                return -1;
            }
            seen_freq = 1;
        } else if (strcmp(name, "userRate") == 0) {
            if (parse_u32(value, 5, &number) != 0) {
                fclose(file);
                return -1;
            }
            cfg->userRate = (uint8_t)number;
            seen_rate = 1;
        } else if (strcmp(name, EXTRA_FIELD_NAME) == 0) {
            if (parse_u32(value, 255, &number) != 0) {
                fclose(file);
                return -1;
            }
            cfg->extra = (uint8_t)number;
            seen_extra = 1;
        } else if (strcmp(name, "hostPrime") == 0) {
            if (parse_u32(value, 1, &number) != 0) {
                fclose(file);
                return -1;
            }
            cfg->hostPrime = (uint8_t)number;
            seen_prime = 1;
        } else {
            fclose(file);
            return -1;
        }
    }
    fclose(file);
    return seen_node && seen_ip && seen_power && seen_freq_type &&
           seen_freq && seen_rate && seen_extra && seen_prime ? 0 : -1;
}

static const char *peer_reason_text(PeerReason reason)
{
    switch (reason) {
    case PEER_REASON_OK:
        return "OK";
    case PEER_REASON_PARAM_MISMATCH:
        return "PARAM_MISMATCH";
    case PEER_REASON_NO_MASTER:
        return "NO_MASTER";
    case PEER_REASON_MULTI_MASTER:
        return "MULTI_MASTER";
    default:
        return "UNKNOWN";
    }
}

static int ovs_add_flowf(const char *bridge, const char *fmt, ...)
{
    char flow[256];
    va_list ap;
    char *argv[] = {"ovs-ofctl", "add-flow", (char *)bridge, flow, NULL};
    int written;

    va_start(ap, fmt);
    written = vsnprintf(flow, sizeof(flow), fmt, ap);
    va_end(ap);
    if (written < 0 || written >= (int)sizeof(flow)) {
        return -1;
    }
    return run_cmd(argv);
}

static int ovs_reset_base_flows(const char *bridge, const char *ns_port, const char *up_port)
{
    char *del_all[] = {"ovs-ofctl", "del-flows", (char *)bridge, NULL};
    uint32_t management_ip;
    char management_ip_text[INET_ADDRSTRLEN];

    if (run_cmd(del_all) != 0) {
        return -1;
    }
    if (ovs_add_flowf(bridge, "priority=300,in_port=LOCAL,dl_type=0x88b5,actions=output:%s", up_port) != 0 ||
        ovs_add_flowf(bridge, "priority=300,in_port=%s,dl_type=0x88b5,actions=LOCAL", up_port) != 0 ||
        ovs_add_flowf(bridge, "priority=290,dl_type=0x88b5,actions=drop") != 0 ||
        ovs_add_flowf(bridge, "priority=260,udp,tp_dst=10001,actions=NORMAL") != 0 ||
        ovs_add_flowf(bridge, "priority=260,udp,tp_src=10001,actions=NORMAL") != 0 ||
        ovs_add_flowf(bridge, "priority=260,udp,tp_dst=8882,actions=NORMAL") != 0 ||
        ovs_add_flowf(bridge, "priority=260,in_port=%s,udp,tp_src=10009,actions=LOCAL", up_port) != 0) {
        return -1;
    }
    if (get_iface_ipv4(bridge, &management_ip) == 0 &&
        format_ipv4(management_ip, management_ip_text, sizeof(management_ip_text)) == 0) {
        if (ovs_add_flowf(bridge, "priority=270,in_port=%s,ip,nw_dst=%s,actions=LOCAL", ns_port, management_ip_text) != 0 ||
            ovs_add_flowf(bridge, "priority=270,in_port=LOCAL,ip,nw_src=%s,actions=output:%s", management_ip_text, up_port) != 0 ||
            ovs_add_flowf(bridge, "priority=270,in_port=%s,arp,arp_tpa=%s,actions=LOCAL", ns_port, management_ip_text) != 0 ||
            ovs_add_flowf(bridge, "priority=270,in_port=LOCAL,arp,arp_spa=%s,actions=output:%s", management_ip_text, up_port) != 0) {
            return -1;
        }
        if (ovs_add_flowf(bridge, "priority=280,in_port=%s,ip,nw_dst=%s,actions=LOCAL", up_port, management_ip_text) != 0 ||
            ovs_add_flowf(bridge, "priority=250,in_port=%s,udp,tp_dst=10001,actions=drop", up_port) != 0 ||
            ovs_add_flowf(bridge, "priority=250,in_port=%s,udp,tp_dst=8882,actions=drop", up_port) != 0) {
            return -1;
        }
    } else {
        log_msg("WARN", "failed to read management IP from %s", bridge);
    }
    return ovs_add_flowf(bridge, "priority=240,arp,actions=NORMAL") == 0 &&
           ovs_add_flowf(bridge, "priority=0,actions=drop") == 0 ? 0 : -1;
}

static int ovs_add_peer_whitelist(const char *bridge, const char *ns_port, const char *up_port,
                                  const RadioConfig *local, const RadioConfig *peer)
{
    char local_ip[INET_ADDRSTRLEN];
    char peer_ip[INET_ADDRSTRLEN];

    if (format_ipv4(local->businessIp, local_ip, sizeof(local_ip)) != 0 ||
        format_ipv4(peer->businessIp, peer_ip, sizeof(peer_ip)) != 0) {
        return -1;
    }
    return ovs_add_flowf(bridge, "priority=200,in_port=%s,ip,nw_src=%s,nw_dst=%s,actions=NORMAL", ns_port, local_ip, peer_ip) == 0 &&
           ovs_add_flowf(bridge, "priority=200,in_port=%s,ip,nw_src=%s,nw_dst=%s,actions=NORMAL", up_port, peer_ip, local_ip) == 0 &&
           ovs_add_flowf(bridge, "priority=200,in_port=%s,arp,arp_spa=%s,arp_tpa=%s,actions=NORMAL", ns_port, local_ip, peer_ip) == 0 &&
           ovs_add_flowf(bridge, "priority=200,in_port=%s,arp,arp_spa=%s,arp_tpa=%s,actions=NORMAL", up_port, peer_ip, local_ip) == 0 ? 0 : -1;
}

static int get_iface_info(int sock, const char *ifname, int *ifindex, uint8_t mac[ETH_ALEN])
{
    struct ifreq ifr;

    memset(&ifr, 0, sizeof(ifr));
    snprintf(ifr.ifr_name, sizeof(ifr.ifr_name), "%s", ifname);
    if (ioctl(sock, SIOCGIFINDEX, &ifr) < 0) {
        return -1;
    }
    *ifindex = ifr.ifr_ifindex;

    memset(&ifr, 0, sizeof(ifr));
    snprintf(ifr.ifr_name, sizeof(ifr.ifr_name), "%s", ifname);
    if (ioctl(sock, SIOCGIFHWADDR, &ifr) < 0) {
        return -1;
    }
    memcpy(mac, ifr.ifr_hwaddr.sa_data, ETH_ALEN);
    return 0;
}

static int open_radio_socket(const char *ifname, int *ifindex, uint8_t mac[ETH_ALEN])
{
    int sock = socket(AF_PACKET, SOCK_RAW, htons(RADIO_ETHERTYPE));
    struct sockaddr_ll addr;

    if (sock < 0) {
        return -1;
    }
    if (get_iface_info(sock, ifname, ifindex, mac) != 0) {
        close(sock);
        return -1;
    }
    memset(&addr, 0, sizeof(addr));
    addr.sll_family = AF_PACKET;
    addr.sll_protocol = htons(RADIO_ETHERTYPE);
    addr.sll_ifindex = *ifindex;
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(sock);
        return -1;
    }
    return sock;
}

static int send_probe(int sock, int ifindex, const uint8_t src_mac[ETH_ALEN],
                      const RadioConfig *cfg)
{
    uint8_t frame[FRAME_LEN];
    struct ethhdr *eth = (struct ethhdr *)frame;
    struct sockaddr_ll dst;

    memset(frame, 0, sizeof(frame));
    memset(eth->h_dest, 0xff, ETH_ALEN);
    memcpy(eth->h_source, src_mac, ETH_ALEN);
    eth->h_proto = htons(RADIO_ETHERTYPE);
    encode_config(frame + ETH_HLEN, cfg);

    memset(&dst, 0, sizeof(dst));
    dst.sll_family = AF_PACKET;
    dst.sll_ifindex = ifindex;
    dst.sll_halen = ETH_ALEN;
    memset(dst.sll_addr, 0xff, ETH_ALEN);
    return sendto(sock, frame, sizeof(frame), 0, (struct sockaddr *)&dst, sizeof(dst)) < 0 ? -1 : 0;
}

static Neighbor *find_neighbor(Neighbor neighbors[NEIGHBOR_MAX], uint8_t node_num)
{
    size_t i;
    for (i = 0; i < NEIGHBOR_MAX; i++) {
        if (neighbors[i].used && neighbors[i].cfg.nodeNum == node_num) {
            return &neighbors[i];
        }
    }
    return NULL;
}

static Neighbor *alloc_neighbor(Neighbor neighbors[NEIGHBOR_MAX])
{
    size_t i;
    for (i = 0; i < NEIGHBOR_MAX; i++) {
        if (!neighbors[i].used) {
            return &neighbors[i];
        }
    }
    return NULL;
}

static int update_neighbor(Neighbor neighbors[NEIGHBOR_MAX], const RadioConfig *cfg,
                           const uint8_t src_mac[ETH_ALEN], time_t now)
{
    Neighbor *neighbor = find_neighbor(neighbors, cfg->nodeNum);
    int changed = 0;

    if (neighbor == NULL) {
        neighbor = alloc_neighbor(neighbors);
        changed = 1;
    }
    if (neighbor == NULL) {
        return 0;
    }
    if (neighbor->used &&
        (!same_config(&neighbor->cfg, cfg) || memcmp(neighbor->src_mac, src_mac, ETH_ALEN) != 0)) {
        changed = 1;
    }
    neighbor->used = 1;
    neighbor->cfg = *cfg;
    memcpy(neighbor->src_mac, src_mac, ETH_ALEN);
    neighbor->last_seen = now;
    return changed;
}

static int receive_probe(int sock, const RadioConfig *local,
                         Neighbor neighbors[NEIGHBOR_MAX])
{
    uint8_t buf[2048];
    struct sockaddr_ll addr;
    socklen_t addr_len = sizeof(addr);
    ssize_t nread;
    const struct ethhdr *eth;
    RadioConfig remote;

    nread = recvfrom(sock, buf, sizeof(buf), 0, (struct sockaddr *)&addr, &addr_len);
    if (nread < 0) {
        if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
            return 0;
        }
        return -1;
    }
    if ((size_t)nread < FRAME_LEN || addr.sll_pkttype == PACKET_OUTGOING) {
        return 0;
    }
    eth = (const struct ethhdr *)buf;
    if (ntohs(eth->h_proto) != RADIO_ETHERTYPE) {
        return 0;
    }
    if (decode_config(buf + ETH_HLEN, (size_t)nread - ETH_HLEN, &remote) != 0) {
        return 0;
    }
    if (remote.nodeNum == local->nodeNum) {
        return 0;
    }
    return update_neighbor(neighbors, &remote, eth->h_source, time(NULL));
}

static int prune_neighbors(Neighbor neighbors[NEIGHBOR_MAX], time_t now)
{
    size_t i;
    int changed = 0;

    for (i = 0; i < NEIGHBOR_MAX; i++) {
        if (neighbors[i].used && now - neighbors[i].last_seen > NEIGHBOR_TIMEOUT_SEC) {
            memset(&neighbors[i], 0, sizeof(neighbors[i]));
            changed = 1;
        }
    }
    return changed;
}

static int count_matching_masters(const RadioConfig *local,
                                  const Neighbor neighbors[NEIGHBOR_MAX])
{
    size_t i;
    int master_count = local->hostPrime == 1 ? 1 : 0;

    for (i = 0; i < NEIGHBOR_MAX; i++) {
        if (neighbors[i].used &&
            same_match_params(local, &neighbors[i].cfg) &&
            neighbors[i].cfg.hostPrime == 1) {
            master_count++;
        }
    }
    return master_count;
}

static void update_peer_reachability(const RadioConfig *local,
                                     Neighbor neighbors[NEIGHBOR_MAX])
{
    size_t i;
    int master_count = count_matching_masters(local, neighbors);

    for (i = 0; i < NEIGHBOR_MAX; i++) {
        if (!neighbors[i].used) {
            continue;
        }
        if (!same_match_params(local, &neighbors[i].cfg)) {
            neighbors[i].reachable = 0;
            neighbors[i].reason = PEER_REASON_PARAM_MISMATCH;
        } else if (master_count == 0) {
            neighbors[i].reachable = 0;
            neighbors[i].reason = PEER_REASON_NO_MASTER;
        } else if (master_count > 1) {
            neighbors[i].reachable = 0;
            neighbors[i].reason = PEER_REASON_MULTI_MASTER;
        } else {
            neighbors[i].reachable = 1;
            neighbors[i].reason = PEER_REASON_OK;
        }
    }
}

static int ovs_apply_policy(const char *bridge, const char *ns_port, const char *up_port,
                            const RadioConfig *local, Neighbor neighbors[NEIGHBOR_MAX])
{
    size_t i;
    int allowed = 0;

    update_peer_reachability(local, neighbors);
    if (ovs_reset_base_flows(bridge, ns_port, up_port) != 0) {
        return -1;
    }
    for (i = 0; i < NEIGHBOR_MAX; i++) {
        if (neighbors[i].used && neighbors[i].reachable) {
            if (ovs_add_peer_whitelist(bridge, ns_port, up_port, local, &neighbors[i].cfg) != 0) {
                return -1;
            }
            allowed++;
        }
    }
    log_msg("INFO", "applied whitelist policy on %s, allowed_peers=%d", bridge, allowed);
    return 0;
}

static int write_peer_status(const RadioConfig *local,
                             Neighbor neighbors[NEIGHBOR_MAX])
{
    FILE *file;
    char local_ip[INET_ADDRSTRLEN];
    char local_freq[32];
    int first = 1;
    size_t i;
    time_t now = time(NULL);

    update_peer_reachability(local, neighbors);
    if (format_ipv4(local->businessIp, local_ip, sizeof(local_ip)) != 0) {
        return -1;
    }
    format_freq(local->freqScaled, local_freq, sizeof(local_freq));
    file = fopen(PEER_STATUS_PATH, "w");
    if (file == NULL) {
        return -1;
    }
    fprintf(file,
            "{\n"
            "  \"local\": {\n"
            "    \"nodeNum\": %u,\n"
            "    \"businessIp\": \"%s\",\n"
            "    \"radioPower\": %u,\n"
            "    \"freqType\": %u,\n"
            "    \"freq\": \"%s\",\n"
            "    \"userRate\": %u,\n"
            "    \"" EXTRA_FIELD_NAME "\": %u,\n"
            "    \"hostPrime\": %u\n"
            "  },\n"
            "  \"peers\": [\n",
            (unsigned int)local->nodeNum, local_ip,
            (unsigned int)local->radioPower,
            (unsigned int)local->freqType,
            local_freq,
            (unsigned int)local->userRate,
            (unsigned int)local->extra,
            (unsigned int)local->hostPrime);

    for (i = 0; i < NEIGHBOR_MAX; i++) {
        char peer_ip[INET_ADDRSTRLEN];
        char peer_freq[32];
        if (!neighbors[i].used) {
            continue;
        }
        if (format_ipv4(neighbors[i].cfg.businessIp, peer_ip, sizeof(peer_ip)) != 0) {
            snprintf(peer_ip, sizeof(peer_ip), "0.0.0.0");
        }
        format_freq(neighbors[i].cfg.freqScaled, peer_freq, sizeof(peer_freq));
        fprintf(file,
                "%s"
                "    {\n"
                "      \"nodeNum\": %u,\n"
                "      \"businessIp\": \"%s\",\n"
                "      \"radioPower\": %u,\n"
                "      \"freqType\": %u,\n"
                "      \"freq\": \"%s\",\n"
                "      \"userRate\": %u,\n"
                "      \"" EXTRA_FIELD_NAME "\": %u,\n"
                "      \"hostPrime\": %u,\n"
                "      \"ageSec\": %ld,\n"
                "      \"reachable\": %s,\n"
                "      \"reason\": \"%s\"\n"
                "    }",
                first ? "" : ",\n",
                (unsigned int)neighbors[i].cfg.nodeNum,
                peer_ip,
                (unsigned int)neighbors[i].cfg.radioPower,
                (unsigned int)neighbors[i].cfg.freqType,
                peer_freq,
                (unsigned int)neighbors[i].cfg.userRate,
                (unsigned int)neighbors[i].cfg.extra,
                (unsigned int)neighbors[i].cfg.hostPrime,
                (long)(now - neighbors[i].last_seen),
                neighbors[i].reachable ? "true" : "false",
                peer_reason_text(neighbors[i].reason));
        first = 0;
    }
    fprintf(file, "\n  ]\n}\n");
    return fclose(file) == 0 ? 0 : -1;
}

static int read_config_stat(const char *path, ConfigStat *out_stat)
{
    struct stat st;

    if (stat(path, &st) != 0) {
        memset(out_stat, 0, sizeof(*out_stat));
        return -1;
    }
    out_stat->mtime_sec = st.st_mtim.tv_sec;
    out_stat->mtime_nsec = st.st_mtim.tv_nsec;
    out_stat->size = st.st_size;
    out_stat->valid = 1;
    return 0;
}

static int same_config_stat(const ConfigStat *a, const ConfigStat *b)
{
    return a->valid == b->valid &&
           a->mtime_sec == b->mtime_sec &&
           a->mtime_nsec == b->mtime_nsec &&
           a->size == b->size;
}

static void log_config(const char *prefix, const RadioConfig *cfg)
{
    char ip[INET_ADDRSTRLEN];
    char freq[32];
    format_ipv4(cfg->businessIp, ip, sizeof(ip));
    format_freq(cfg->freqScaled, freq, sizeof(freq));
    log_msg("INFO", "%s nodeNum=%u businessIp=%s radioPower=%u freqType=%u freq=%s userRate=%u %s=%u hostPrime=%u",
            prefix,
            (unsigned int)cfg->nodeNum,
            ip,
            (unsigned int)cfg->radioPower,
            (unsigned int)cfg->freqType,
            freq,
            (unsigned int)cfg->userRate,
            EXTRA_FIELD_NAME,
            (unsigned int)cfg->extra,
            (unsigned int)cfg->hostPrime);
}

static int run_daemon(const char *iface, const char *bridge, const char *config_file,
                      const char *ns_port, const char *up_port)
{
    int sock;
    int ifindex;
    uint8_t mac[ETH_ALEN];
    RadioConfig local_cfg;
    Neighbor neighbors[NEIGHBOR_MAX];
    ConfigStat last_stat;
    long long last_tx_ms = 0;
    long long last_cfg_ms = 0;

    memset(neighbors, 0, sizeof(neighbors));
    if (load_config(config_file, &local_cfg) != 0) {
        log_msg("ERROR", "failed to load config %s", config_file);
        return 1;
    }
    read_config_stat(config_file, &last_stat);
    sock = open_radio_socket(iface, &ifindex, mac);
    if (sock < 0) {
        log_msg("ERROR", "failed to open AF_PACKET socket on %s: %s", iface, strerror(errno));
        return 1;
    }
    signal(SIGTERM, handle_signal);
    signal(SIGINT, handle_signal);

    log_config("loaded config", &local_cfg);
    ovs_apply_policy(bridge, ns_port, up_port, &local_cfg, neighbors);
    write_peer_status(&local_cfg, neighbors);
    log_msg("INFO", "%s started on iface=%s bridge=%s config=%s ns_port=%s up_port=%s", DAEMON_NAME, iface, bridge, config_file, ns_port, up_port);

    while (g_running) {
        struct pollfd pfd;
        int changed = 0;
        struct timespec ts;
        long long now_ms;

        clock_gettime(CLOCK_MONOTONIC, &ts);
        now_ms = (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;

        pfd.fd = sock;
        pfd.events = POLLIN;
        pfd.revents = 0;
        if (poll(&pfd, 1, 50) > 0 && (pfd.revents & POLLIN)) {
            int result = receive_probe(sock, &local_cfg, neighbors);
            if (result < 0) {
                log_msg("WARN", "failed to receive probe");
            } else if (result > 0) {
                changed = 1;
            }
        }
        if (now_ms - last_tx_ms >= BROADCAST_INTERVAL_MS) {
            if (send_probe(sock, ifindex, mac, &local_cfg) != 0) {
                log_msg("WARN", "failed to send probe");
            }
            last_tx_ms = now_ms;
        }
        if (now_ms - last_cfg_ms >= CONFIG_CHECK_INTERVAL_MS) {
            ConfigStat current_stat;
            if (read_config_stat(config_file, &current_stat) == 0 &&
                !same_config_stat(&last_stat, &current_stat)) {
                RadioConfig new_cfg;
                if (load_config(config_file, &new_cfg) == 0) {
                    local_cfg = new_cfg;
                    last_stat = current_stat;
                    log_config("reloaded config", &local_cfg);
                    changed = 1;
                }
            }
            if (prune_neighbors(neighbors, time(NULL))) {
                changed = 1;
            }
            write_peer_status(&local_cfg, neighbors);
            last_cfg_ms = now_ms;
        }
        if (changed) {
            ovs_apply_policy(bridge, ns_port, up_port, &local_cfg, neighbors);
            write_peer_status(&local_cfg, neighbors);
        }
    }
    close(sock);
    log_msg("INFO", "%s stopped", DAEMON_NAME);
    return 0;
}

int main(int argc, char **argv)
{
    const char *iface = "br0";
    const char *bridge = "br0";
    const char *config_file = CONFIG_FILE_PATH;
    const char *ns_port = "eth-ns";
    const char *up_port = "eth-up";
    int i;

    for (i = 1; i < argc; i++) {
        if ((strcmp(argv[i], "-i") == 0 || strcmp(argv[i], "--iface") == 0) && i + 1 < argc) {
            iface = argv[++i];
        } else if ((strcmp(argv[i], "-b") == 0 || strcmp(argv[i], "--bridge") == 0) && i + 1 < argc) {
            bridge = argv[++i];
        } else if (strcmp(argv[i], "--config-file") == 0 && i + 1 < argc) {
            config_file = argv[++i];
        } else if ((strcmp(argv[i], "-n") == 0 || strcmp(argv[i], "--ns-port") == 0) && i + 1 < argc) {
            ns_port = argv[++i];
        } else if ((strcmp(argv[i], "-u") == 0 || strcmp(argv[i], "--up-port") == 0) && i + 1 < argc) {
            up_port = argv[++i];
        } else {
            fprintf(stderr,
                    "usage: %s [-i iface] [-b bridge] [--config-file path] "
                    "[-n ns-port] [-u up-port]\n",
                    argv[0]);
            return 2;
        }
    }
    return run_daemon(iface, bridge, config_file, ns_port, up_port);
}
