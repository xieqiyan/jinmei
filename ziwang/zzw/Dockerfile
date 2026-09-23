ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive

RUN sed -i \
    -e 's/archive.ubuntu.com/mirrors.aliyun.com/g' \
    -e 's/security.ubuntu.com/mirrors.aliyun.com/g' \
    /etc/apt/sources.list

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    openvswitch-switch \
    iproute2 \
    net-tools \
    iputils-ping \
    tcpdump \
    iperf3 \
    python3 \
    curl \
    vim \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/run/openvswitch && \
    ovsdb-tool create /etc/openvswitch/conf.db /usr/share/openvswitch/vswitch.ovsschema

WORKDIR /opt/zzw
COPY subnet_daemon ./subnet_daemon
RUN make -C subnet_daemon clean && make -C subnet_daemon install && make -C subnet_daemon clean

COPY radio_protocol /usr/local/lib/radio_protocol
COPY radio_param_service.py /usr/local/bin/radio_param_service.py
RUN chmod +x /usr/local/bin/radio_param_service.py

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/bin/bash"]
