FROM docker.1ms.run/library/debian:12-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends openssh-server openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/run/sshd \
    && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
COPY fake-docker /usr/local/bin/docker
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/docker /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
