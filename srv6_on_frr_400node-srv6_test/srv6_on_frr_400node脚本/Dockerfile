FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

# 安装基础工具
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    lsb-release \
    apt-transport-https \
    ca-certificates \
    iproute2 \
    iputils-ping \
    tcpdump \
    net-tools \
    vim \
    traceroute \
    iperf3 \
    && rm -rf /var/lib/apt/lists/*

# 安装FRR
RUN curl -s https://deb.frrouting.org/frr/keys.gpg | tee /usr/share/keyrings/frrouting.gpg > /dev/null && \
    echo "deb [signed-by=/usr/share/keyrings/frrouting.gpg] https://deb.frrouting.org/frr $(lsb_release -s -c) frr-stable" | \
    tee -a /etc/apt/sources.list.d/frr.list && \
    apt-get update && \
    apt-get install -y frr frr-pythontools && \
    rm -rf /var/lib/apt/lists/*

# 创建必要目录
RUN mkdir -p /etc/frr /var/log/frr /var/run/frr && \
    chown -R frr:frr /etc/frr /var/log/frr /var/run/frr && \
    chmod 755 /etc/frr /var/log/frr /var/run/frr

# 创建空的配置文件
RUN touch /etc/frr/vtysh.conf && \
    chown frr:frr /etc/frr/vtysh.conf && \
    chmod 644 /etc/frr/vtysh.conf

# 复制启动脚本
COPY start-frr-srv6.sh /usr/local/bin/start-frr-srv6.sh
RUN chmod +x /usr/local/bin/start-frr-srv6.sh

CMD ["/usr/local/bin/start-frr-srv6.sh"]