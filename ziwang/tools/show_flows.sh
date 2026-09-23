#!/bin/bash
# show_flows.sh - 清晰展示四个容器 br0 的流表（串行、分区显示）

containers=("conA" "conB" "conC" "conD")

for c in "${containers[@]}"; do
    echo "╔══════════════════════════════════════════╗"
    echo "║  容器: $c  -  br0 流表                   ║"
    echo "╚══════════════════════════════════════════╝"
    docker exec "$c" ovs-ofctl dump-flows br0 2>&1
    echo -e "\n"
done
