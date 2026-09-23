#define _GNU_SOURCE

#include "config_store.h"

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

#define CKL_ETHERTYPE 0x88B5
#define CKL_FMT_VERSION 0x02
#define CKL_RADIO_TYPE 0x01
#define CKL_PAYLOAD_LEN 13
#define CKL_FRAME_LEN (ETH_HLEN + CKL_PAYLOAD_LEN)
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

static int same_subnet_params(const RadioConfig *a, const RadioConfig *b)
{
    return a->radioFreq == b->radioFreq &&
           a->radioPower == b->radioPower &&
           a->workFreqMode == b->workFreqMode &&
           a->radioRate == b->radioRate;
}

static int same_config(const RadioConfig *a, const RadioConfig *b)
{
    return a->nodeNo == b->nodeNo &&
           a->businessIp == b->businessIp &&
           a->radioFreq == b->radioFreq &&
           a->radioPower == b->radioPower &&
           a->workFreqMode == b->workFreqMode &&
           a->radioRate == b->radioRate &&
           a->hostPrime == b->hostPrime;
}

static void encode_config(uint8_t payload[CKL_PAYLOAD_LEN], const RadioConfig *cfg)
{
    payload[0] = CKL_FMT_VERSION;
    payload[1] = CKL_RADIO_TYPE;
    payload[2] = cfg->nodeNo;
    memcpy(&payload[3], &cfg->businessIp, sizeof(cfg->businessIp));
    config_u16_to_be(cfg->radioFreq, &payload[7]);
    payload[9] = cfg->radioPower;
    payload[10] = cfg->workFreqMode;
    payload[11] = cfg->radioRate;
    payload[12] = cfg->hostPrime;
}

static int decode_config(const uint8_t *payload, size_t len, RadioConfig *cfg)
{
    if (payload == NULL || cfg == NULL || len < CKL_PAYLOAD_LEN) {
        return -1;
    }
    if (payload[0] != CKL_FMT_VERSION || payload[1] != CKL_RADIO_TYPE) {
        return -1;
    }

    cfg->nodeNo = payload[2];
    memcpy(&cfg->businessIp, &payload[3], sizeof(cfg->businessIp));
    cfg->radioFreq = config_u16_from_be(&payload[7]);
    cfg->radioPower = payload[9];
    cfg->workFreqMode = payload[10];
    cfg->radioRate = payload[11];
    cfg->hostPrime = payload[12];

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

    do {
        if (waitpid(pid, &status, 0) < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        break;
    } while (1);

    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        return -1;
    }
    return 0;
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
        log_msg("ERROR", "OVS flow string too long");
        return -1;
    }

    if (run_cmd(argv) != 0) {
        log_msg("ERROR", "failed to add OVS flow on %s: %s", bridge, flow);
        return -1;
    }
    return 0;
}

static int ovs_reset_base_flows(const char *bridge, const char *ns_port, const char *up_port)
{
    char *del_all[] = {"ovs-ofctl", "del-flows", (char *)bridge, NULL};
    uint32_t management_ip;
    char management_ip_text[INET_ADDRSTRLEN];

    if (run_cmd(del_all) != 0) {
        log_msg("ERROR", "failed to clear OVS flows on %s", bridge);
        return -1;
    }
    if (ovs_add_flowf(bridge, "priority=300,in_port=LOCAL,dl_type=0x88b5,actions=output:%s", up_port) != 0) {
        return -1;
    }
    if (ovs_add_flowf(bridge, "priority=300,in_port=%s,dl_type=0x88b5,actions=LOCAL", up_port) != 0) {
        return -1;
    }
    if (ovs_add_flowf(bridge, "priority=290,dl_type=0x88b5,actions=drop") != 0) {
        return -1;
    }
    if (ovs_add_flowf(bridge, "priority=260,udp,tp_dst=10001,actions=NORMAL") != 0) {
        return -1;
    }
    if (ovs_add_flowf(bridge, "priority=260,udp,tp_src=10001,actions=NORMAL") != 0) {
        return -1;
    }
    if (ovs_add_flowf(bridge, "priority=260,udp,tp_dst=8882,actions=NORMAL") != 0) {
        return -1;
    }
    if (get_iface_ipv4(bridge, &management_ip) == 0 &&
        format_ipv4(management_ip, management_ip_text, sizeof(management_ip_text)) == 0) {
        if (ovs_add_flowf(bridge,
                          "priority=270,in_port=%s,ip,nw_dst=%s,actions=LOCAL",
                          ns_port,
                          management_ip_text) != 0) {
            return -1;
        }
        if (ovs_add_flowf(bridge,
                          "priority=270,in_port=LOCAL,ip,nw_src=%s,actions=output:%s",
                          management_ip_text,
                          up_port) != 0) {
            return -1;
        }
        if (ovs_add_flowf(bridge,
                          "priority=270,in_port=%s,arp,arp_tpa=%s,actions=LOCAL",
                          ns_port,
                          management_ip_text) != 0) {
            return -1;
        }
        if (ovs_add_flowf(bridge,
                          "priority=270,in_port=LOCAL,arp,arp_spa=%s,actions=output:%s",
                          management_ip_text,
                          up_port) != 0) {
            return -1;
        }
        if (ovs_add_flowf(bridge,
                          "priority=280,in_port=%s,ip,nw_dst=%s,actions=LOCAL",
                          up_port,
                          management_ip_text) != 0 ||
            ovs_add_flowf(bridge,
                          "priority=250,in_port=%s,udp,tp_dst=10001,actions=drop",
                          up_port) != 0 ||
            ovs_add_flowf(bridge,
                          "priority=250,in_port=%s,udp,tp_dst=8882,actions=drop",
                          up_port) != 0) {
            return -1;
        }
    } else {
        log_msg("WARN", "failed to read management IP from %s, local namespace-container IP traffic may be blocked",
                bridge);
    }
    if (ovs_add_flowf(bridge, "priority=240,arp,actions=NORMAL") != 0) {
        return -1;
    }
    if (ovs_add_flowf(bridge, "priority=0,actions=drop") != 0) {
        return -1;
    }
    return 0;
}

static int ovs_add_peer_whitelist(const char *bridge, const char *ns_port, const char *up_port,
                                  const RadioConfig *local, const RadioConfig *peer)
{
    char local_ip[INET_ADDRSTRLEN];
    char peer_ip[INET_ADDRSTRLEN];

    if (format_ipv4(local->businessIp, local_ip, sizeof(local_ip)) != 0 ||
        format_ipv4(peer->businessIp, peer_ip, sizeof(peer_ip)) != 0) {
        log_msg("ERROR", "failed to format whitelist IPs");
        return -1;
    }

    if (ovs_add_flowf(bridge,
                      "priority=200,in_port=%s,ip,nw_src=%s,nw_dst=%s,actions=NORMAL",
                      ns_port,
                      local_ip, peer_ip) != 0) {
        return -1;
    }
    if (ovs_add_flowf(bridge,
                      "priority=200,in_port=%s,ip,nw_src=%s,nw_dst=%s,actions=NORMAL",
                      up_port,
                      peer_ip, local_ip) != 0) {
        return -1;
    }
    if (ovs_add_flowf(bridge,
                      "priority=200,in_port=%s,arp,arp_spa=%s,arp_tpa=%s,actions=NORMAL",
                      ns_port,
                      local_ip, peer_ip) != 0) {
        return -1;
    }
    if (ovs_add_flowf(bridge,
                      "priority=200,in_port=%s,arp,arp_spa=%s,arp_tpa=%s,actions=NORMAL",
                      up_port,
                      peer_ip, local_ip) != 0) {
        return -1;
    }

    return 0;
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

static int open_ckl_socket(const char *ifname, int *ifindex, uint8_t mac[ETH_ALEN])
{
    int sock;
    struct sockaddr_ll addr;

    sock = socket(AF_PACKET, SOCK_RAW, htons(CKL_ETHERTYPE));
    if (sock < 0) {
        return -1;
    }

    if (get_iface_info(sock, ifname, ifindex, mac) != 0) {
        close(sock);
        return -1;
    }

    memset(&addr, 0, sizeof(addr));
    addr.sll_family = AF_PACKET;
    addr.sll_protocol = htons(CKL_ETHERTYPE);
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
    uint8_t frame[CKL_FRAME_LEN];
    struct ethhdr *eth = (struct ethhdr *)frame;
    struct sockaddr_ll dst;

    memset(frame, 0, sizeof(frame));
    memset(eth->h_dest, 0xff, ETH_ALEN);
    memcpy(eth->h_source, src_mac, ETH_ALEN);
    eth->h_proto = htons(CKL_ETHERTYPE);
    encode_config(frame + ETH_HLEN, cfg);

    memset(&dst, 0, sizeof(dst));
    dst.sll_family = AF_PACKET;
    dst.sll_ifindex = ifindex;
    dst.sll_halen = ETH_ALEN;
    memset(dst.sll_addr, 0xff, ETH_ALEN);

    if (sendto(sock, frame, sizeof(frame), 0,
               (struct sockaddr *)&dst, sizeof(dst)) < 0) {
        return -1;
    }
    return 0;
}

static Neighbor *find_neighbor(Neighbor neighbors[NEIGHBOR_MAX], uint8_t node_no)
{
    size_t i;

    for (i = 0; i < NEIGHBOR_MAX; i++) {
        if (neighbors[i].used && neighbors[i].cfg.nodeNo == node_no) {
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
    Neighbor *neighbor = find_neighbor(neighbors, cfg->nodeNo);
    int changed = 0;

    if (neighbor == NULL) {
        neighbor = alloc_neighbor(neighbors);
        changed = 1;
    }
    if (neighbor == NULL) {
        log_msg("WARN", "neighbor table full, ignoring node %u",
                (unsigned int)cfg->nodeNo);
        return 0;
    }

    if (neighbor->used &&
        (!same_config(&neighbor->cfg, cfg) ||
         memcmp(neighbor->src_mac, src_mac, ETH_ALEN) != 0)) {
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

    nread = recvfrom(sock, buf, sizeof(buf), 0,
                     (struct sockaddr *)&addr, &addr_len);
    if (nread < 0) {
        if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
            return 0;
        }
        return -1;
    }
    if ((size_t)nread < CKL_FRAME_LEN) {
        return 0;
    }
    if (addr.sll_pkttype == PACKET_OUTGOING) {
        return 0;
    }

    eth = (const struct ethhdr *)buf;
    if (ntohs(eth->h_proto) != CKL_ETHERTYPE) {
        return 0;
    }
    if (decode_config(buf + ETH_HLEN, (size_t)nread - ETH_HLEN, &remote) != 0) {
        return 0;
    }
    if (remote.nodeNo == local->nodeNo) {
        return 0;
    }

    return update_neighbor(neighbors, &remote, eth->h_source, time(NULL));
}

static int prune_neighbors(Neighbor neighbors[NEIGHBOR_MAX], time_t now)
{
    size_t i;
    int changed = 0;

    for (i = 0; i < NEIGHBOR_MAX; i++) {
        if (!neighbors[i].used) {
            continue;
        }
        if (now - neighbors[i].last_seen > NEIGHBOR_TIMEOUT_SEC) {
            log_msg("INFO", "neighbor node %u aged out",
                    (unsigned int)neighbors[i].cfg.nodeNo);
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
        if (!neighbors[i].used) {
            continue;
        }
        if (!same_subnet_params(local, &neighbors[i].cfg)) {
            continue;
        }
        if (neighbors[i].cfg.hostPrime == 1) {
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

        if (!same_subnet_params(local, &neighbors[i].cfg)) {
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
        if (!neighbors[i].used || !neighbors[i].reachable) {
            continue;
        }
        if (ovs_add_peer_whitelist(bridge, ns_port, up_port, local, &neighbors[i].cfg) != 0) {
            return -1;
        }
        allowed++;
    }

    log_msg("INFO", "applied whitelist policy on %s, allowed_peers=%d",
            bridge, allowed);
    return 0;
}

static int write_peer_status(const RadioConfig *local,
                             const Neighbor neighbors[NEIGHBOR_MAX])
{
    FILE *file;
    char local_ip[INET_ADDRSTRLEN];
    int first = 1;
    size_t i;
    time_t now = time(NULL);

    if (format_ipv4(local->businessIp, local_ip, sizeof(local_ip)) != 0) {
        return -1;
    }

    file = fopen(PEER_STATUS_PATH, "w");
    if (file == NULL) {
        log_msg("ERROR", "failed to write %s: %s", PEER_STATUS_PATH, strerror(errno));
        return -1;
    }

    fprintf(file,
            "{\n"
            "  \"local\": {\n"
            "    \"nodeNo\": %u,\n"
            "    \"businessIp\": \"%s\",\n"
            "    \"radioFreq\": %u,\n"
            "    \"radioPower\": %u,\n"
            "    \"workFreqMode\": %u,\n"
            "    \"radioRate\": %u,\n"
            "    \"hostPrime\": %u\n"
            "  },\n"
            "  \"peers\": [\n",
            (unsigned int)local->nodeNo,
            local_ip,
            (unsigned int)local->radioFreq,
            (unsigned int)local->radioPower,
            (unsigned int)local->workFreqMode,
            (unsigned int)local->radioRate,
            (unsigned int)local->hostPrime);

    for (i = 0; i < NEIGHBOR_MAX; i++) {
        char peer_ip[INET_ADDRSTRLEN];

        if (!neighbors[i].used) {
            continue;
        }
        if (format_ipv4(neighbors[i].cfg.businessIp, peer_ip, sizeof(peer_ip)) != 0) {
            snprintf(peer_ip, sizeof(peer_ip), "0.0.0.0");
        }

        fprintf(file,
                "%s"
                "    {\n"
                "      \"nodeNo\": %u,\n"
                "      \"businessIp\": \"%s\",\n"
                "      \"radioFreq\": %u,\n"
                "      \"radioPower\": %u,\n"
                "      \"workFreqMode\": %u,\n"
                "      \"radioRate\": %u,\n"
                "      \"hostPrime\": %u,\n"
                "      \"ageSec\": %ld,\n"
                "      \"reachable\": %s,\n"
                "      \"reason\": \"%s\"\n"
                "    }",
                first ? "" : ",\n",
                (unsigned int)neighbors[i].cfg.nodeNo,
                peer_ip,
                (unsigned int)neighbors[i].cfg.radioFreq,
                (unsigned int)neighbors[i].cfg.radioPower,
                (unsigned int)neighbors[i].cfg.workFreqMode,
                (unsigned int)neighbors[i].cfg.radioRate,
                (unsigned int)neighbors[i].cfg.hostPrime,
                (long)(now - neighbors[i].last_seen),
                neighbors[i].reachable ? "true" : "false",
                peer_reason_text(neighbors[i].reason));
        first = 0;
    }

    fprintf(file, "\n  ]\n}\n");

    if (fclose(file) != 0) {
        log_msg("ERROR", "failed to close %s: %s", PEER_STATUS_PATH, strerror(errno));
        return -1;
    }

    return 0;
}

static void log_config(const char *prefix, const RadioConfig *cfg)
{
    char business_ip[INET_ADDRSTRLEN];

    if (format_ipv4(cfg->businessIp, business_ip, sizeof(business_ip)) != 0) {
        snprintf(business_ip, sizeof(business_ip), "0.0.0.0");
    }

    log_msg("INFO",
            "%s nodeNo=%u businessIp=%s radioFreq=%u radioPower=%u workFreqMode=%u radioRate=%u hostPrime=%u",
            prefix,
            (unsigned int)cfg->nodeNo,
            business_ip,
            (unsigned int)cfg->radioFreq,
            (unsigned int)cfg->radioPower,
            (unsigned int)cfg->workFreqMode,
            (unsigned int)cfg->radioRate,
            (unsigned int)cfg->hostPrime);
}

static int load_config(RadioConfig *cfg)
{
    int result = config_read_all(cfg);

    if (result != CONFIG_OK) {
        log_msg("ERROR", "failed to read %s, error=%d", CONFIG_FILE_PATH, result);
        return -1;
    }
    return 0;
}

static int read_config_stat(ConfigStat *out)
{
    struct stat st;

    if (stat(CONFIG_FILE_PATH, &st) != 0) {
        return -1;
    }

    out->mtime_sec = st.st_mtim.tv_sec;
    out->mtime_nsec = st.st_mtim.tv_nsec;
    out->size = st.st_size;
    out->valid = 1;
    return 0;
}

static int config_stat_changed(ConfigStat *old_stat)
{
    ConfigStat current;

    if (read_config_stat(&current) != 0) {
        return old_stat->valid;
    }
    if (!old_stat->valid ||
        current.mtime_sec != old_stat->mtime_sec ||
        current.mtime_nsec != old_stat->mtime_nsec ||
        current.size != old_stat->size) {
        *old_stat = current;
        return 1;
    }
    return 0;
}

static void usage(const char *prog)
{
    fprintf(stderr,
            "Usage: %s [-i iface] [-b bridge]\n"
            "  -i iface   AF_PACKET interface, default br0\n"
            "  -b bridge  OVS bridge to control, default br0\n"
            "  -n port    software-side OVS port, default eth-ns\n"
            "  -u port    uplink OVS port, default eth-up\n"
            "Config file: %s\n",
            prog, CONFIG_FILE_PATH);
}

int main(int argc, char **argv)
{
    const char *iface = "br0";
    const char *bridge = "br0";
    const char *ns_port = "eth-ns";
    const char *up_port = "eth-up";
    int opt;
    int sock;
    int ifindex = 0;
    uint8_t iface_mac[ETH_ALEN];
    RadioConfig local_cfg;
    Neighbor neighbors[NEIGHBOR_MAX];
    ConfigStat cfg_stat = {0};
    int policy_applied = 0;
    long long next_tx_ms;
    long long next_cfg_check_ms;

    while ((opt = getopt(argc, argv, "i:b:n:u:h")) != -1) {
        switch (opt) {
        case 'i':
            iface = optarg;
            break;
        case 'b':
            bridge = optarg;
            break;
        case 'n':
            ns_port = optarg;
            break;
        case 'u':
            up_port = optarg;
            break;
        case 'h':
        default:
            usage(argv[0]);
            return opt == 'h' ? 0 : 2;
        }
    }

    signal(SIGTERM, handle_signal);
    signal(SIGINT, handle_signal);

    memset(neighbors, 0, sizeof(neighbors));

    if (load_config(&local_cfg) != 0) {
        return 1;
    }
    if (read_config_stat(&cfg_stat) != 0) {
        log_msg("ERROR", "failed to stat %s: %s", CONFIG_FILE_PATH, strerror(errno));
        return 1;
    }
    log_config("loaded config", &local_cfg);

    if (ovs_apply_policy(bridge, ns_port, up_port, &local_cfg, neighbors) != 0) {
        return 1;
    }
    policy_applied = 1;
    write_peer_status(&local_cfg, neighbors);

    sock = open_ckl_socket(iface, &ifindex, iface_mac);
    if (sock < 0) {
        log_msg("ERROR", "failed to open AF_PACKET socket on %s: %s",
                iface, strerror(errno));
        return 1;
    }

    {
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        next_tx_ms = ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
        next_cfg_check_ms = next_tx_ms + CONFIG_CHECK_INTERVAL_MS;
    }

    log_msg("INFO", "ckl_daemon started on iface=%s bridge=%s ns_port=%s up_port=%s",
            iface, bridge, ns_port, up_port);

    while (g_running) {
        struct timespec ts;
        struct pollfd pfd;
        long long now_ms;
        long long wait_tx;
        long long wait_cfg;
        int timeout_ms;
        int poll_result;
        int changed = 0;

        clock_gettime(CLOCK_MONOTONIC, &ts);
        now_ms = ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;

        wait_tx = next_tx_ms - now_ms;
        wait_cfg = next_cfg_check_ms - now_ms;
        if (wait_tx < 0) {
            wait_tx = 0;
        }
        if (wait_cfg < 0) {
            wait_cfg = 0;
        }
        timeout_ms = (int)(wait_tx < wait_cfg ? wait_tx : wait_cfg);

        pfd.fd = sock;
        pfd.events = POLLIN;
        pfd.revents = 0;
        poll_result = poll(&pfd, 1, timeout_ms);
        if (poll_result < 0) {
            if (errno == EINTR) {
                continue;
            }
            log_msg("ERROR", "poll failed: %s", strerror(errno));
            break;
        }
        if (poll_result > 0 && (pfd.revents & POLLIN)) {
            int rx = receive_probe(sock, &local_cfg, neighbors);
            if (rx < 0) {
                log_msg("ERROR", "receive failed: %s", strerror(errno));
            } else if (rx > 0) {
                changed = 1;
            }
        }

        clock_gettime(CLOCK_MONOTONIC, &ts);
        now_ms = ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;

        if (now_ms >= next_tx_ms) {
            if (send_probe(sock, ifindex, iface_mac, &local_cfg) != 0) {
                log_msg("ERROR", "send probe failed: %s", strerror(errno));
            }
            next_tx_ms = now_ms + BROADCAST_INTERVAL_MS;
        }

        if (now_ms >= next_cfg_check_ms) {
            if (config_stat_changed(&cfg_stat)) {
                RadioConfig new_cfg;
                if (load_config(&new_cfg) == 0) {
                    local_cfg = new_cfg;
                    log_config("reloaded config", &local_cfg);
                    changed = 1;
                }
            }
            if (prune_neighbors(neighbors, time(NULL))) {
                changed = 1;
            }
            update_peer_reachability(&local_cfg, neighbors);
            write_peer_status(&local_cfg, neighbors);
            next_cfg_check_ms = now_ms + CONFIG_CHECK_INTERVAL_MS;
        }

        if (changed || !policy_applied) {
            if (ovs_apply_policy(bridge, ns_port, up_port, &local_cfg, neighbors) == 0) {
                policy_applied = 1;
                write_peer_status(&local_cfg, neighbors);
            }
        }
    }

    close(sock);
    log_msg("INFO", "ckl_daemon stopped");
    return 0;
}
