#!/bin/bash
set -e

ovsdb-server --remote=punix:/var/run/openvswitch/db.sock \
             --private-key=db:Open_vSwitch,SSL,private_key \
             --certificate=db:Open_vSwitch,SSL,certificate \
             --bootstrap-ca-cert=db:Open_vSwitch,SSL,ca_cert \
             --pidfile --detach
ovs-vswitchd --pidfile --detach

sleep 1

BRIDGE="${BRIDGE:-br0}"
mkdir -p /tmp/radio_test
ovs-vsctl --may-exist add-br "$BRIDGE"
ip link set "$BRIDGE" up
ovs-ofctl add-flow "$BRIDGE" actions=NORMAL 2>/dev/null || true

exec "$@"
