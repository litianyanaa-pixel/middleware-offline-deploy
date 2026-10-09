# -*- coding: utf-8 -*-
"""打包配置校验(单机/多机/K8s 集群/物料齐套)"""
import base64
import os
import re
import yaml

from .catalog import PLUGINS, PROXYSQL_META
from .compose import backupable_services
from .paths import BASE_DIR, WAREHOUSE
from .util import (LINUX_PATH_RE, MIRROR_RE, PASSWORD_RE, PROJECT_RE, USERNAME_RE,
                   WEBHOOK_RE, PackError, is_multihost, master_server_ip, tr, valid_ipv4)


# ---------------------------------------------------------------- 配置校验

# etcd 高级参数白名单(kk builtin etcd.env 模板逐键消费; int 键做数值校验, log_level 为枚举)

_ETCD_ENV_KEYS = {"heartbeat_interval": int, "election_timeout": int, "compaction_retention": int,
                  "snapshot_count": int, "quota_backend_bytes": int, "max_request_bytes": int,
                  "max_snapshots": int, "max_wals": int, "log_level": str}


_ETCD_ENV_ORDER = ("heartbeat_interval", "election_timeout", "compaction_retention", "snapshot_count",
                   "quota_backend_bytes", "max_request_bytes", "max_snapshots", "max_wals", "log_level")


def validate_config(cfg, catalog):
    """校验+规范化配置, 返回 (norm_cfg, warnings)"""
    warns = []

    project = str(cfg.get("project", "")).strip().lower()
    if not PROJECT_RE.match(project):
        raise PackError("项目名不合法: 只能小写字母开头, 小写字母/数字/中划线, 2-41 位|Invalid project name: must start with a lowercase letter, 2-41 chars of lowercase letters/digits/hyphens")
    arch = cfg.get("arch")
    if arch not in ("amd64", "arm64"):
        raise PackError("必须选择目标架构 (amd64 / arm64)|Target architecture must be selected (amd64 / arm64)")
    lang = cfg.get("lang") or "zh"
    if lang not in ("zh", "en"):
        lang = "zh"
    cfg["lang"] = lang

    services = cfg.get("services") or []
    cluster_raw = cfg.get("cluster") or {}
    cluster_on = bool(cluster_raw.get("enabled"))
    if not services and not cluster_on:
        raise PackError("至少选择一个中间件或启用 K8s 集群|Select at least one middleware or enable the K8s cluster")
    known = list(catalog["services"].keys())
    for s in services:
        if s not in known:
            raise PackError("未知中间件: %s|Unknown middleware: %s" % (s, s))
        meta = catalog["services"][s]
        if arch not in meta["supported_arch"]:
            raise PackError("%s 不支持 %s 架构 (%s)|%s does not support %s architecture (%s)"
                            % (tr(meta["label"], "zh"), arch, meta.get("note", "").partition("|")[0],
                               tr(meta["label"], "en"), arch, meta.get("note", "").partition("|")[2]))

    # ---- 端口 ----
    ports = cfg.get("ports") or {}
    norm_ports = {}
    seen = {}
    for s in services:
        for p in catalog["services"][s]["ports"]:
            key = p["key"]
            raw = ports.get(key, p["default"])
            try:
                v = int(raw)
            except (TypeError, ValueError):
                raise PackError("端口 %s 不合法: %s|Invalid port for %s: %s" % (tr(p["label"], "zh"), raw, tr(p["label"], "en"), raw))
            if not (1 <= v <= 65535):
                raise PackError("端口 %s 超出范围 1-65535: %s|Port %s out of range 1-65535: %s" % (tr(p["label"], "zh"), v, tr(p["label"], "en"), v))
            # 多机部署的服务跑在不同服务器上, 同端口合法(如多机主从同端口), 不参与查重
            mh_on = str((cfg.get("topology") or {}).get(s, "single")) in ("master-slave", "sentinel", "cluster")                     and ((cfg.get("multihost") or {}).get(s) or {}).get("enabled")
            if v in seen and not mh_on:
                raise PackError("端口冲突: %s 和 %s 都用了 %d|Port conflict: %s and %s both use %d"
                                % (tr(seen[v], "zh"), tr(p["label"], "zh"), v, tr(seen[v], "en"), tr(p["label"], "en"), v))
            if not mh_on:
                seen[v] = p["label"]
            norm_ports[key] = v
    cfg["ports"] = norm_ports

    # ---- 部署形态(可选): mysql8 主从 / redis 哨兵 ----
    topo_raw = cfg.get("topology") or {}
    topology = {}
    if "mysql8" in services:
        m = str(topo_raw.get("mysql8", "single"))
        if m not in ("single", "master-slave"):
            raise PackError("MySQL 部署形态不合法: %s (single / master-slave)|Invalid MySQL topology: %s (single / master-slave)" % (m, m))
        topology["mysql8"] = m
    if "mysql57" in services:
        m = str(topo_raw.get("mysql57", "single"))
        if m not in ("single", "master-slave"):
            raise PackError("MySQL 5.7 部署形态不合法: %s (single / master-slave)|Invalid MySQL 5.7 topology: %s (single / master-slave)" % (m, m))
        topology["mysql57"] = m
    if "redis" in services:
        r = str(topo_raw.get("redis", "single"))
        if r not in ("single", "sentinel"):
            raise PackError("Redis 部署形态不合法: %s (single / sentinel)|Invalid Redis topology: %s (single / sentinel)" % (r, r))
        topology["redis"] = r
    if "kafka" in services:
        k = str(topo_raw.get("kafka", "single"))
        if k not in ("single", "cluster"):
            raise PackError("Kafka 部署形态不合法: %s (single / cluster)|Invalid Kafka topology: %s (single / cluster)" % (k, k))
        topology["kafka"] = k
    cfg["topology"] = topology

    # ---- 多机部署: 服务器池 + 角色分配(可选) ----
    raw_servers = cfg.get("servers") or []
    servers = []
    seen_ips = set()
    for i, sv in enumerate(raw_servers):
        ip = str((sv or {}).get("ip", "")).strip()
        if not ip:
            continue
        if not valid_ipv4(ip):
            raise PackError("服务器 IP 不合法: %s (第 %d 台)|Invalid server IP: %s (#%d)" % (ip, i + 1, ip, i + 1))
        if ip in seen_ips:
            raise PackError("服务器 IP 重复: %s|Duplicate server IP: %s" % (ip, ip))
        seen_ips.add(ip)
        ssh_raw = sv.get("ssh", 22)
        try:
            ssh = int(ssh_raw)
        except (TypeError, ValueError):
            raise PackError("SSH 端口不合法: %s (服务器 %s)|Invalid SSH port: %s (server %s)" % (ssh_raw, ip, ssh_raw, ip))
        if not (1 <= ssh <= 65535):
            raise PackError("SSH 端口超出范围: %d (服务器 %s)|SSH port out of range: %d (server %s)" % (ssh, ip, ssh, ip))
        name = re.sub(r"[^A-Za-z0-9_-]", "", str(sv.get("name") or ("node%d" % (i + 1)))) or ("node%d" % (i + 1))
        password = str(sv.get("password") or sv.get("pass") or "")
        if "|" in password or "\n" in password:
            raise PackError("SSH 密码不能包含 | 或换行符 (服务器 %s)|SSH password must not contain | or newline (server %s)" % (ip, ip))
        servers.append({"name": name, "user": str(sv.get("user") or "root").strip() or "root",
                        "ip": ip, "ssh": ssh, "password": password})
    cfg["servers"] = servers

    # ---- K8s 集群(可选): 角色分配自服务器池, 物料按 catalog["cluster"] ----
    cluster = {"enabled": False}
    if cluster_on:
        if arch != "amd64":
            raise PackError("K8s 集群暂仅支持 amd64, arm64(鲲鹏/飞腾)后续开放|K8s cluster is amd64-only for now; arm64 support coming later")
        # 集群节点 SSH 密码留空 = 按免密连接, 提醒运维预分发密钥
        srv_raw = cfg.get("servers") or []
        role_map = cluster_raw.get("roles") or {}
        empty_pw = [str((srv_raw[int(i)] or {}).get("name") or ("node%s" % (int(i) + 1)))
                    for i in role_map if int(i) < len(srv_raw) and not ((srv_raw[int(i)] or {}).get("password") or (srv_raw[int(i)] or {}).get("pass") or "").strip()]
        if empty_pw:
            warns.append("集群节点未填 SSH 密码: %s — 将按免密 SSH 连接, 部署前需在节点间预分发密钥; 否则请回填密码重新打包"
                         "|Cluster nodes without SSH password: %s — passwordless SSH assumed; distribute keys first or fill passwords and repack"
                         % (", ".join(empty_pw), ", ".join(empty_pw)))
        ccat = catalog.get("cluster") or {}
        if not ccat:
            raise PackError("versions.json 缺少 cluster 物料目录|cluster catalog missing in versions.json")
        kube_ver = str(cluster_raw.get("kube_version") or "")
        if kube_ver not in (ccat.get("versions") or {}):
            raise PackError("K8s 版本不在物料目录: %s (可选 %s)|K8s version not in catalog: %s"
                            % (kube_ver, sorted((ccat.get("versions") or {})), kube_ver))
        mode = str(cluster_raw.get("mode") or "artifact")
        if mode not in ("artifact", "cache", "online"):
            raise PackError("集群离线模式不合法: %s (artifact / cache / online)|Invalid cluster mode: %s" % (mode, mode))
        if mode == "online":
            pass   # 联网安装: 无需 artifact/cache 物料, zone 由配置生成
        # 数据目录(可选覆盖 kk 默认)
        comps_raw = cluster_raw.get("components") or {}
        def _abspath(v, what):
            v = str(v or "").strip()
            if v and not v.startswith("/"):
                raise PackError("%s 需为绝对路径: %s|%s must be an absolute path: %s" % (what, v, what, v))
            return v
        components = {
            "containerd_root": _abspath(comps_raw.get("containerd_root"), "containerd 数据目录"),
            "docker_root": _abspath(comps_raw.get("docker_root"), "docker 数据目录"),
            "etcd_dir": _abspath(comps_raw.get("etcd_dir"), "etcd 数据目录"),
            "static_binary": bool(comps_raw.get("static_binary")),
            "containerd_version_override": str(comps_raw.get("containerd_version_override") or "").strip(),
        }
        if components["containerd_version_override"]:
            # 统一补 v 前缀: 仓库目录/包内 cache 布局均按 vX.Y.Z 命名, 1.7.27 会生成不存在的目录
            if not components["containerd_version_override"].startswith("v"):
                components["containerd_version_override"] = "v" + components["containerd_version_override"]
        if components["containerd_version_override"] and not re.match(r"^v\d+\.\d+", components["containerd_version_override"]):
            raise PackError("containerd 版本覆盖不合法: %s|Invalid containerd version override: %s"
                            % (components["containerd_version_override"], components["containerd_version_override"]))
        # etcd 高级参数(etcd.env.*; 键为 kk etcd 模板认的白名单, 值类型受限)
        etcd_env = {}
        for kv in (comps_raw.get("etcd_env") or []):
            k, _, v = str(kv).partition("=")
            k, v = k.strip(), v.strip()
            if not k or not v:
                raise PackError("etcd 高级参数需为 key=value: %s|etcd advanced param must be key=value: %s" % (kv, kv))
            if k not in _ETCD_ENV_KEYS:
                raise PackError("etcd 高级参数不支持: %s (可选: %s)|Unsupported etcd param: %s (allowed: %s)"
                                % (k, ", ".join(sorted(_ETCD_ENV_KEYS)), k, ", ".join(sorted(_ETCD_ENV_KEYS))))
            if _ETCD_ENV_KEYS[k] is int:
                try:
                    v = int(v)
                except ValueError:
                    raise PackError("etcd 参数 %s 需为整数: %s|etcd param %s must be an integer: %s" % (k, v, k, v))
            etcd_env[k] = v
        components["etcd_env"] = etcd_env
        if mode == "online" and str(cluster_raw.get("zone") or "cn") == "cn" and components["static_binary"]:
            # 在线+国内源: qingstor 镜像只同步非 static 版 containerd(static-*.tar.gz 404),
            # 静态构建仅离线包/国际源(用户直连 GitHub)可信, 这里自动回退并提示
            components["static_binary"] = False
            warns.append("在线+国内源: 国内镜像未同步 static 版 containerd, 已自动改用非 static 构建"
                         "(老系统兼容性由 containerd 官方发布包保证)|"
                         "Online+cn: domestic mirror lacks static containerd builds, falling back to non-static")
        # kubelet 参数
        kubelet_raw = cluster_raw.get("kubelet") or {}
        kubelet = {}
        if kubelet_raw.get("max_pods"):
            try:
                kubelet["max_pods"] = int(kubelet_raw["max_pods"])
                if not (1 <= kubelet["max_pods"] <= 1000):
                    raise ValueError
            except (TypeError, ValueError):
                raise PackError("max-pods 不合法: %s|Invalid max-pods: %s" % (kubelet_raw["max_pods"], kubelet_raw["max_pods"]))
        extra_args = []
        for kv in (kubelet_raw.get("extra_args") or []):
            kv = str(kv).strip()
            if kv:
                if "=" not in kv:
                    raise PackError("kubelet extra_args 需为 key=value 形式: %s|kubelet extra_args must be key=value: %s" % (kv, kv))
                extra_args.append(kv)
        kubelet["extra_args"] = extra_args
        kubelet["extra_config"] = str(kubelet_raw.get("extra_config") or "")
        # kubelet 数据目录(root-dir 启动参数): 数据盘场景常用
        root_dir = str(kubelet_raw.get("root_dir") or "").strip()
        if root_dir and not root_dir.startswith("/"):
            raise PackError("kubelet 数据目录需为绝对路径: %s|kubelet root-dir must be an absolute path: %s" % (root_dir, root_dir))
        kubelet["root_dir"] = root_dir
        # 镜像加速(docker.io)
        mirrors = [str(m).strip().rstrip("/") for m in (cluster_raw.get("registry_mirrors") or []) if str(m).strip()]
        for m in mirrors:
            if not m.startswith(("http://", "https://")):
                raise PackError("镜像加速地址需以 http(s):// 开头: %s|Registry mirror must start with http(s)://: %s" % (m, m))
        # K8s 组件镜像源前缀(在线/镜像下载走该仓库)
        k8s_image_registry = str(cluster_raw.get("k8s_image_registry") or "").strip().rstrip("/")
        if k8s_image_registry and not re.match(r"^[a-z0-9][a-z0-9._/-]*(:[0-9]+)?$", k8s_image_registry, re.I):
            raise PackError("K8s 镜像源前缀不合法: %s (如 hub.kubesphere.com.cn 或 host:port/namespace)|Invalid K8s image registry prefix: %s"
                            % (k8s_image_registry, k8s_image_registry))
        # NTP
        ntp_raw = cluster_raw.get("ntp") or {}
        ntp = {"enabled": bool(ntp_raw.get("enabled")),
               "servers": [str(s).strip() for s in (ntp_raw.get("servers") or []) if str(s).strip()]}
        if ntp["enabled"] and not ntp["servers"]:
            raise PackError("启用 NTP 时至少填写一台 NTP 服务器|NTP enabled requires at least one server")
        # 存储
        storage_raw = cluster_raw.get("storage") or {}
        storage = {
            "localpv_enabled": bool(storage_raw.get("localpv_enabled")),
            "localpv_path": _abspath(storage_raw.get("localpv_path"), "localpv 路径") or "/var/openebs/local",
            "nfs_enabled": bool(storage_raw.get("nfs_enabled")),
            "nfs_default": bool(storage_raw.get("nfs_default")),
            "nfs_server": str(storage_raw.get("nfs_server") or "").strip(),
            "nfs_path": str(storage_raw.get("nfs_path") or "").strip() or "/share/kubernetes",
        }
        if storage["nfs_enabled"] and not storage["nfs_server"]:
            raise PackError("启用 NFS 存储类必须填写 NFS 服务器地址|NFS storage class requires an NFS server address")
        # 私有镜像仓库部署
        imgreg_raw = cluster_raw.get("image_registry") or {}
        imgreg = {"type": str(imgreg_raw.get("type") or "").strip(),
                  "vip": str(imgreg_raw.get("vip") or "").strip()}
        if imgreg["type"] and imgreg["type"] not in ("harbor", "docker-registry"):
            raise PackError("镜像仓库类型不合法: %s (harbor / docker-registry)|Invalid registry type: %s" % (imgreg["type"], imgreg["type"]))
        if imgreg["type"]:
            has_registry_node = any(n["role"] == "registry" for n in nodes)
            if not has_registry_node:
                raise PackError("部署私有镜像仓库需要给至少一台节点分配 registry 角色|Deploying a private registry requires a node with the registry role")
            if imgreg["vip"] and not valid_ipv4(imgreg["vip"]):
                raise PackError("镜像仓库 VIP 不合法: %s|Invalid registry VIP: %s" % (imgreg["vip"], imgreg["vip"]))
        # 是否由 kk 设置节点 hostname(容器等特殊环境需关闭; 默认开)
        set_hostname = cluster_raw.get("set_hostname")
        set_hostname = True if set_hostname is None else bool(set_hostname)
        # 证书自动续期 crontab(部署后写入; 空=不启用)
        certs_cron = str(cluster_raw.get("certs_renew_cron") or "").strip()
        # CRI 运行时(kk cri.container_manager): docker 仅在线安装 — 纯离线物料与预装流程只含 containerd
        container_manager = str(cluster_raw.get("container_manager") or "containerd").strip()
        if container_manager not in ("containerd", "docker"):
            raise PackError("运行时不合法: %s (containerd / docker)|Invalid container runtime: %s (containerd / docker)"
                            % (container_manager, container_manager))
        if container_manager == "docker" and mode != "online":
            raise PackError("docker 运行时仅支持在线安装(纯离线物料与预装流程只含 containerd)|"
                            "docker runtime is online-only (the offline bundle preinstalls containerd)")
        # CNI 高级: 每节点 Pod 子网掩码(kk cni.ipv4_mask_size, 默认 24) + Multi-CNI
        try:
            ipv4_mask_size = int(cluster_raw.get("ipv4_mask_size") or 24)
        except (TypeError, ValueError):
            raise PackError("Pod 子网掩码需为整数: %s|IPv4 mask size must be an integer: %s"
                            % (cluster_raw.get("ipv4_mask_size"), cluster_raw.get("ipv4_mask_size")))
        if not (16 <= ipv4_mask_size <= 28):
            raise PackError("Pod 子网掩码超出合理范围 16-28: %d|IPv4 mask size out of range 16-28: %d"
                            % (ipv4_mask_size, ipv4_mask_size))
        multi_cni = str(cluster_raw.get("multi_cni") or "none").strip()
        if multi_cni not in ("none", "multus"):
            raise PackError("Multi-CNI 不合法: %s (none / multus)|Invalid multi-CNI: %s (none / multus)" % (multi_cni, multi_cni))
        multi_cni_tag = str(cluster_raw.get("multi_cni_tag") or "").strip()
        if multi_cni_tag and multi_cni != "multus":
            multi_cni_tag = ""   # 未启用 multus 时 tag 无意义, 不入库
        # DNS 覆盖(镜像 tag; nodelocaldns_enabled 三态: None=不写, False=显式关闭)
        dns_raw = cluster_raw.get("dns") or {}
        dns = {"coredns_tag": str(dns_raw.get("coredns_tag") or "").strip(),
               "nodelocaldns_tag": str(dns_raw.get("nodelocaldns_tag") or "").strip(),
               "nodelocaldns_enabled": (bool(dns_raw["nodelocaldns_enabled"])
                                        if dns_raw.get("nodelocaldns_enabled") is not None else None)}
        # kubeadm 配置备份目录(kk post_install 带时间戳备份 /etc/kubernetes/kubeadm-config.yaml; 空=禁用备份)
        kubeadm_config_dir = str(cluster_raw.get("kubeadm_config_dir") or "").strip()
        if kubeadm_config_dir and not kubeadm_config_dir.startswith("/"):
            raise PackError("kubeadm 备份目录需为绝对路径(留空禁用备份): %s|kubeadm backup dir must be an absolute path (empty disables backup): %s"
                            % (kubeadm_config_dir, kubeadm_config_dir))
        # 安装时证书续期(kk kubernetes.certs.renew, 默认开; 三态: None=不写跟随 kk 默认)
        certs_renew = cluster_raw.get("certs_renew")
        certs_renew = None if certs_renew is None else bool(certs_renew)
        cni_type = str(cluster_raw.get("cni_type") or (ccat["versions"][kube_ver]["cni_plugin"]["type"]))
        if cni_type not in ("calico", "cilium", "flannel", "kubeovn"):
            raise PackError("CNI 类型不合法: %s|Invalid CNI type: %s" % (cni_type, cni_type))
        # CNI×K8s 兼容矩阵前置校验(免得到服务器部署时才被 kk precheck 拦下)
        minor = ".".join(kube_ver.split(".")[:2])   # v1.34.11 -> v1.34
        matrix = ccat.get("cni_matrix") or {}
        if cni_type in matrix and minor:
            ok_minors = set()
            for _ver, minors in matrix[cni_type].items():
                ok_minors.update(minors)
            if ok_minors and minor not in ok_minors:
                raise PackError(
                    "CNI %s 不支持 Kubernetes %s (可选: %s)|CNI %s does not support Kubernetes %s (supported: %s)"
                    % (cni_type, kube_ver, sorted(ok_minors), cni_type, kube_ver, sorted(ok_minors)))
        # Calico 自定义 values(kk cni.calico.values 透传 helm -f; 仅 CNI=calico 生效)
        calico_values = str(cluster_raw.get("calico_values") or "").strip("\n")
        if calico_values:
            if cni_type != "calico":
                warns.append("Calico values 覆盖仅在 CNI=calico 时生效, 当前 CNI=%s 已忽略|Calico values override only applies when CNI is calico (current: %s)"
                             % (cni_type, cni_type))
                calico_values = ""
            else:
                try:
                    parsed = yaml.safe_load(calico_values)
                    if not isinstance(parsed, dict):
                        raise ValueError("not a mapping")
                except Exception as e:
                    raise PackError("Calico values 不是合法 YAML mapping: %s|Calico values is not a valid YAML mapping: %s" % (e, e))
        proxy_mode = str(cluster_raw.get("proxy_mode") or "iptables")
        if proxy_mode not in ("iptables", "nftables"):
            raise PackError("kube-proxy 模式不合法: %s (iptables / nftables)|Invalid kube-proxy mode: %s" % (proxy_mode, proxy_mode))
        for cidr_key, v in (("pod_cidr", cluster_raw.get("pod_cidr") or "10.233.64.0/18"),
                            ("service_cidr", cluster_raw.get("service_cidr") or "10.233.0.0/18")):
            if "/" not in v or v.count("/") > 1:
                raise PackError("集群 %s 需为 CIDR 形式: %s|Cluster %s must be CIDR format: %s" % (cidr_key, v, cidr_key, v))
        roles_raw = cluster_raw.get("roles") or {}
        nodes, seen_names = [], set()
        for idx, role in roles_raw.items():
            if role in ("", None, "none"):
                continue
            if role not in ("control-plane", "worker", "registry"):
                raise PackError("集群角色不合法: %s (control-plane / worker / registry)|Invalid cluster role: %s" % (role, role))
            try:
                sv = servers[int(idx)]
            except (ValueError, IndexError):
                raise PackError("集群角色分配指向不存在的服务器: %s|Cluster role points to a missing server: %s" % (idx, idx))
            if sv["name"] in seen_names:
                raise PackError("集群节点名重复: %s|Duplicate cluster node name: %s" % (sv["name"], sv["name"]))
            seen_names.add(sv["name"])
            nodes.append({"name": sv["name"], "ip": sv["ip"], "user": sv["user"],
                          "ssh": sv["ssh"], "password": sv["password"], "role": role})
        if not any(n["role"] == "control-plane" for n in nodes):
            raise PackError("K8s 集群至少需要一台控制面节点(在角色分配中选择)|The K8s cluster needs at least one control-plane node (assign it in role mapping)")
        os_distros = [d for d in (cluster_raw.get("os_distros") or []) if d]
        for d in os_distros:
            if d not in (ccat.get("distros") or {}):
                raise PackError("OS 依赖包发行版不在支持矩阵: %s|Distro not in cluster matrix: %s" % (d, d))
        # 控制面高可用: 多控制面不允许 local(无 VIP, 其余节点无法加入同一端点)
        ha_type = str(cluster_raw.get("ha_type") or "local")
        if ha_type not in ("local", "kube-vip", "haproxy"):
            raise PackError("控制面 HA 类型不合法: %s (local / kube-vip / haproxy)|Invalid control-plane HA type: %s" % (ha_type, ha_type))
        ha_vip = str(cluster_raw.get("ha_vip") or "").strip()
        cp_count = sum(1 for n in nodes if n["role"] == "control-plane")
        if ha_type == "local" and cp_count > 1:
            warns.append("多控制面 + local 端点仅单点可用, 生产请选 kube-vip/haproxy 并填 VIP"
                         "|Multiple control-plane nodes with a local endpoint expose a single VIP; prefer kube-vip/haproxy + VIP")
        if ha_type in ("kube-vip", "haproxy"):
            if not ha_vip:
                raise PackError("HA 类型为 %s 时必须填写 VIP|HA type %s requires a VIP" % (ha_type, ha_type))
            if not valid_ipv4(ha_vip):
                raise PackError("VIP 不是合法 IPv4: %s|Invalid VIP: %s" % (ha_vip, ha_vip))
        # 集群升级包(可选): 目标版本的 kube 三件套须已备料
        upgrade_to = str(cluster_raw.get("upgrade_to") or "").strip()
        if upgrade_to:
            kube_dir = BASE_DIR / ("warehouse/cluster/kube/%s/%s" % (upgrade_to, arch))
            missing_up = [b for b in ("kubeadm", "kubelet", "kubectl") if not (kube_dir / b).is_file()]
            if missing_up:
                raise PackError(
                    "升级包缺目标版本二进制(%s): 应位于 %s|Upgrade bundle missing %s under %s"
                    % ("/".join(missing_up), kube_dir, "/".join(missing_up), kube_dir))
        cluster = {"enabled": True, "kube_version": kube_ver, "mode": mode, "cni_type": cni_type,
                   "proxy_mode": proxy_mode, "zone": str(cluster_raw.get("zone") or "cn"),
                   "components": components, "kubelet": kubelet, "registry_mirrors": mirrors,
                   "k8s_image_registry": k8s_image_registry, "set_hostname": set_hostname,
                   "ntp": ntp, "storage": storage, "image_registry": imgreg,
                   "certs_renew_cron": certs_cron,
                   "container_manager": container_manager, "ipv4_mask_size": ipv4_mask_size,
                   "multi_cni": multi_cni, "multi_cni_tag": multi_cni_tag,
                   "calico_values": calico_values,
                   "dns": dns, "kubeadm_config_dir": kubeadm_config_dir, "certs_renew": certs_renew,
                   "ha_type": ha_type, "ha_vip": ha_vip, "upgrade_to": upgrade_to,
                   "pod_cidr": cluster_raw.get("pod_cidr") or "10.233.64.0/18",
                   "service_cidr": cluster_raw.get("service_cidr") or "10.233.0.0/18",
                   "timezone": cluster_raw.get("timezone") or "Asia/Shanghai",
                   "os_distros": os_distros, "nodes": nodes}
        vminor = int(kube_ver.split(".")[1]) if kube_ver.startswith("v") else 0
        if vminor >= 35:
            warns.append("K8s %s 要求 cgroup v2, 麒麟/龙蜥等默认 cgroup v1 的系统需先启用并重启节点|K8s %s requires cgroup v2; enable it on cgroup-v1 distros (kylin/anolis...) before deploying" % (kube_ver, kube_ver))
        if proxy_mode == "nftables":
            warns.append("kube-proxy nftables 模式要求节点内核 >= 5.13, 旧内核(麒麟 4.19/阿里云 5.10)请用 iptables|nftables proxy mode needs kernel >= 5.13; use iptables on old kernels")
    cfg["cluster"] = cluster

    # ---- Java 运行时(可选基础中间件): 版本多选 + 默认版本 + 部署目标 ----
    # targets 元素: "local"(运行 deploy.sh 的主部署机) 或服务器池索引; 版本集合全局统一
    jraw = cfg.get("java") or {}
    java = {"enabled": bool(jraw.get("enabled")), "versions": [], "default": "", "targets": []}
    if java["enabled"]:
        all_majors = [str(m) for m in ((catalog.get("java") or {}).get("majors") or ["8", "17", "21"])]
        vs = sorted({str(v) for v in (jraw.get("versions") or []) if str(v) in all_majors},
                    key=all_majors.index)
        if not vs:
            warns.append("已启用 Java 运行时但未勾选版本, 已按默认 %s 处理|Java runtime enabled but no version "
                         "selected; defaulting to %s" % (all_majors[-1], all_majors[-1]))
            vs = [all_majors[-1]]
        java["versions"] = vs
        dft = str(jraw.get("default") or "") or vs[-1]
        if dft not in vs:
            warns.append("Java 默认版本 %s 不在勾选列表, 已改用 %s|Java default version %s is not selected; "
                         "switched to %s" % (dft, vs[-1], dft, vs[-1]))
            dft = vs[-1]
        java["default"] = dft
        for t in (jraw.get("targets") or []):
            if t == "local":
                java["targets"].append("local")
                continue
            try:
                idx = int(t)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(servers) and idx not in java["targets"]:
                java["targets"].append(idx)
        if not java["targets"]:
            java["targets"] = ["local"]
            warns.append("Java 部署目标为空, 默认仅安装到主部署机|Empty Java targets; installing on the "
                         "primary host only")
    cfg["java"] = java

    mh_raw = cfg.get("multihost") or {}
    mh = {}

    def _pick_idx(v, what):
        try:
            idx = int(v)
        except (TypeError, ValueError):
            raise PackError("%s 节点选择不合法|Invalid node assignment for %s" % (what, what))
        if not (0 <= idx < len(servers)):
            raise PackError("%s 指向了不存在的服务器(请先完善服务器池)|%s points to a missing server (fill the server pool first)" % (what, what))
        return idx

    if "kafka" in services and topology.get("kafka") == "cluster" and (mh_raw.get("kafka") or {}).get("enabled"):
        k = mh_raw.get("kafka") or {}
        try:
            count = int(k.get("count", 3))
        except (TypeError, ValueError):
            count = 0
        if count not in (3, 5):
            raise PackError("Kafka 多机节点数只能是 3 或 5|Kafka multi-host node count must be 3 or 5")
        brokers = [_pick_idx(x, "Kafka broker%d" % (i + 1)) for i, x in enumerate(k.get("brokers") or [])]
        if len(brokers) != count or len(set(brokers)) != count:
            raise PackError("Kafka 多机需为 %d 个 broker 分配 %d 台不同服务器|Kafka multi-host needs %d distinct servers for %d brokers" % (count, count, count, count))
        mh["kafka"] = {"enabled": True, "count": count, "brokers": brokers}

    for ms in ("mysql8", "mysql57"):
        if ms in services and topology.get(ms) == "master-slave" and (mh_raw.get(ms) or {}).get("enabled"):
            m = mh_raw.get(ms) or {}
            master = _pick_idx(m.get("master"), "%s 主库" % ms)
            replicas = [_pick_idx(x, "%s 从库" % ms) for x in (m.get("replicas") or [])]
            if not (1 <= len(replicas) <= 2) or master in replicas or len(set(replicas)) != len(replicas):
                raise PackError("%s 多机主从需 1-2 台不同的从库服务器且不与主库重复|%s multi-host needs 1-2 distinct replica servers != master" % (ms, ms))
            mh[ms] = {"enabled": True, "master": master, "replicas": replicas}

    if "redis" in services and topology.get("redis") == "sentinel" and (mh_raw.get("redis") or {}).get("enabled"):
        r = mh_raw.get("redis") or {}
        master = _pick_idx(r.get("master"), "Redis 主库")
        replicas = [_pick_idx(x, "Redis 从库") for x in (r.get("replicas") or [])]
        if len(replicas) != 2 or master in replicas or len(set(replicas)) != 2:
            raise PackError("Redis 多机哨兵需 1 主 + 2 从共 3 台不同服务器|Redis multi-host sentinel needs 1 master + 2 replicas on 3 distinct servers")
        mh["redis"] = {"enabled": True, "master": master, "replicas": replicas}
    cfg["multihost"] = mh

    # 单机主从从库数量(1-2); 多机时以分配的服务器数为准
    reps_raw = cfg.get("replicas") or {}
    reps = {}
    for ms in ("mysql8", "mysql57"):
        if ms not in services or topology.get(ms) != "master-slave":
            continue
        if ms in mh:
            reps[ms] = len(mh[ms]["replicas"])
            continue
        try:
            n = int(reps_raw.get(ms, 1))
        except (TypeError, ValueError):
            n = 1
        if n not in (1, 2):
            raise PackError("%s 从库数量只能是 1 或 2|%s replica count must be 1 or 2" % (ms, ms))
        reps[ms] = n
    cfg["replicas"] = reps

    # 读写分离(ProxySQL): 每个主从形态的 MySQL 独立一个实例(独立开关/入口与管理端口/配置,
    # 两套集群混在同一对 hostgroup 里读会互串); 旧版布尔 features.proxysql 兼容为对所有主从 MySQL 启用
    features_raw = cfg.get("features") or {}
    px_raw = features_raw.get("proxysql")
    if isinstance(px_raw, dict):
        px_on = {s: bool(px_raw.get(s)) for s in ("mysql8", "mysql57")}
    else:
        # 按实例顶层键(proxysql_mysql8/...)或旧版布尔(proxysql=true 视为全启用)
        px_on = {s: bool(features_raw.get("proxysql_" + s)) or bool(px_raw)
                 for s in ("mysql8", "mysql57")}
    features = {"proxysql": False}
    for s in ("mysql8", "mysql57"):
        features["proxysql_" + s] = px_on[s] and s in reps
    features["proxysql"] = features["proxysql_mysql8"] or features["proxysql_mysql57"]
    if features["proxysql"] and "proxysql" in services:
        raise PackError("读写分离为可选组件, 无需在中间件中单独选择|Read/write splitting is an optional component, not a middleware")
    cfg["features"] = features

    # 集群形态附加端口键(并入全局查重)
    topo_port_defs = []
    if topology.get("mysql8") == "master-slave":
        topo_port_defs.append(("mysql8_replica", "MySQL 从库端口|MySQL replica port", 13308))
        if reps.get("mysql8", 1) == 2 and "mysql8" not in mh:
            topo_port_defs.append(("mysql8_replica2", "MySQL 从库2端口|MySQL replica2 port", 13310))
    if topology.get("mysql57") == "master-slave":
        topo_port_defs.append(("mysql57_replica", "MySQL 5.7 从库端口|MySQL 5.7 replica port", 13309))
        if reps.get("mysql57", 1) == 2 and "mysql57" not in mh:
            topo_port_defs.append(("mysql57_replica2", "MySQL 5.7 从库2端口|MySQL 5.7 replica2 port", 13311))
    if topology.get("redis") == "sentinel":
        topo_port_defs.append(("redis_replica", "Redis 从库端口|Redis replica port", 16380))
        if "redis" in mh:
            topo_port_defs.append(("redis_sentinel", "Redis 哨兵端口(每节点)|Redis sentinel port (per node)", 26379))
    if topology.get("kafka") == "cluster":
        if "kafka" in mh:
            # 多机: 每节点同一组宿主端口(内网互联/控制器/SASL 对外), 各台机器独立不冲突
            topo_port_defs.append(("kafka_mh_inter", "Kafka 节点互联端口|Kafka inter-broker port", 9092))
            topo_port_defs.append(("kafka_mh_ctrl", "Kafka 控制器端口|Kafka controller port", 9093))
            topo_port_defs.append(("kafka_mh_sasl", "Kafka SASL 对外端口|Kafka SASL port", 9094))
        else:
            topo_port_defs.append(("kafka_c1", "Kafka broker1 端口(SASL)|Kafka broker1 port (SASL)", 19092))
            topo_port_defs.append(("kafka_c2", "Kafka broker2 端口(SASL)|Kafka broker2 port (SASL)", 29092))
            topo_port_defs.append(("kafka_c3", "Kafka broker3 端口(SASL)|Kafka broker3 port (SASL)", 39092))
    # 旧版单实例端口键迁移到 mysql8 实例(仅当新键未填), 老配置重新打包端口不变
    ports = dict(ports)
    if "proxysql_mysql8" not in ports and "proxysql" in ports:
        ports["proxysql_mysql8"] = ports["proxysql"]
    if "proxysql_mysql8_admin" not in ports and "proxysql_admin" in ports:
        ports["proxysql_mysql8_admin"] = ports["proxysql_admin"]
    for _px in ("mysql8", "mysql57"):
        if not features.get("proxysql_" + _px):
            continue
        nm = "MySQL 8.0" if _px == "mysql8" else "MySQL 5.7"
        d_main, d_admin = {"mysql8": (16033, 16032), "mysql57": (16035, 16034)}[_px]
        topo_port_defs.append(("proxysql_" + _px,
                               "读写分离入口端口(%s 主从)|Read/write split entry port (%s)" % (nm, nm), d_main))
        topo_port_defs.append(("proxysql_" + _px + "_admin",
                               "ProxySQL 管理端口(%s 主从)|ProxySQL admin port (%s)" % (nm, nm), d_admin))
    if "kafka" in mh:
        # 多机时主部署机不运行 Kafka 容器, 单机端口键让位给节点端口组
        norm_ports.pop("kafka", None)
        norm_ports.pop("kafka_host", None)
    # 多机形态下这些端口只存在于远端节点(主部署机不监听), 放开全局同端口冲突:
    # 异机部署允许主库与从库同端口(每台机器端口空间独立); kafka 多机节点组端口本就每台一致。
    # 单机主从/单机哨兵从库与主库同机, 仍走全局查重
    mh_node_keys = set()
    for ms in ("mysql8", "mysql57"):
        if ms in mh:
            mh_node_keys.add(ms + "_replica")
    if "redis" in mh:
        mh_node_keys |= {"redis_replica", "redis_sentinel"}
    seen_topo = set(norm_ports.values())
    for key, label, default in topo_port_defs:
        # 从用户原始 ports 取值(cfg["ports"] 已被目录归一化覆盖, 拓扑附加键不在 catalog 里会被丢掉)
        raw = ports.get(key, default)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise PackError("端口 %s 不合法: %s|Invalid port for %s: %s" % (tr(label, "zh"), raw, tr(label, "en"), raw))
        if not (1 <= v <= 65535):
            raise PackError("端口 %s 超出范围 1-65535: %d|Port %s out of range 1-65535: %d" % (tr(label, "zh"), v, tr(label, "en"), v))
        if key not in mh_node_keys and not key.startswith("kafka_mh_"):
            if v in seen_topo:
                raise PackError("端口冲突: %s 与其他端口都用了 %d|Port conflict: %s conflicts on %d" % (tr(label, "zh"), v, tr(label, "en"), v))
            seen_topo.add(v)
        norm_ports[key] = v

    # ---- 密码/账号 ----
    secrets_cfg = cfg.get("secrets") or {}
    need_keys = [x["key"] for x in catalog["secrets"]
                 if set(x["services"]) & set(services)]
    for item in catalog["secrets"]:
        key = item["key"]
        if key not in need_keys:
            continue
        val = str(secrets_cfg.get(key, item["default"])).strip()
        typ = item.get("type")
        if typ == "nacos_token":
            if not val.startswith("SecretKey"):
                raise PackError("Nacos Auth Token 必须以 SecretKey 开头|Nacos Auth Token must start with SecretKey")
            b64part = val[len("SecretKey"):]
            try:
                raw = base64.b64decode(b64part, validate=True)
            except Exception:
                raise PackError("Nacos Auth Token 的 SecretKey 后面必须是合法 Base64|Nacos Auth Token must be valid Base64 after SecretKey")
            if len(raw) < 32:
                raise PackError("Nacos Auth Token 解码后长度必须 >= 32 字节|Decoded Nacos Auth Token must be >= 32 bytes")
        elif item.get("type") == "webhook":
            # 告警 Webhook 地址: 可留空(仅界面展示), 非空时须是合法 URL 字符
            if val and not WEBHOOK_RE.match(val):
                raise PackError("%s 格式不合法(应为 http/https 开头的 URL): %s|%s is invalid (must be a http/https URL): %s"
                                % (tr(item["label"], "zh"), key, tr(item["label"], "en"), key))
        elif item.get("secret", True):
            if not PASSWORD_RE.match(val):
                raise PackError("%s 含不合法字符(禁止 空格 和 $ ` \" ' \\ ; | 字符): %s|%s contains illegal characters (space and $ ` \" ' \\ ; | are forbidden): %s"
                                % (tr(item["label"], "zh"), key, tr(item["label"], "en"), key))
            if len(val) < item.get("min_len", 6):
                raise PackError("%s 长度至少 %d 位|%s must be at least %d characters"
                                % (tr(item["label"], "zh"), item.get("min_len", 6), tr(item["label"], "en"), item.get("min_len", 6)))
        else:
            if not USERNAME_RE.match(val):
                raise PackError("%s 含不合法字符: %s|%s contains illegal characters: %s"
                                % (tr(item["label"], "zh"), key, tr(item["label"], "en"), key))
        secrets_cfg[key] = val
    # 只保留本次所选服务的密钥: 未选组件的账号密码不写入 .env / manifest.sh
    cfg["secrets"] = {k: secrets_cfg[k] for k in need_keys}

    # ---- 目录/镜像源 ----
    deploy_dir = str(cfg.get("deploy_dir") or catalog["defaults"]["deploy_dir"]).strip()
    data_root = str(cfg.get("docker_data_root") or catalog["defaults"]["docker_data_root"]).strip()
    if not LINUX_PATH_RE.match(deploy_dir) or deploy_dir == "/":
        raise PackError("部署目录必须是 Linux 绝对路径, 如 /data/middleware|Deploy dir must be a Linux absolute path, e.g. /data/middleware")
    if not LINUX_PATH_RE.match(data_root) or data_root == "/":
        raise PackError("Docker 数据目录必须是 Linux 绝对路径, 如 /data/docker|Docker data-root must be a Linux absolute path, e.g. /data/docker")
    cfg["deploy_dir"] = deploy_dir.rstrip("/")
    cfg["docker_data_root"] = data_root.rstrip("/")

    mirrors = cfg.get("registry_mirrors") or []
    norm_mirrors = []
    for m in mirrors:
        m = str(m).strip()
        if not m:
            continue
        if not MIRROR_RE.match(m):
            raise PackError("镜像加速器地址不合法: %s|Invalid registry mirror URL: %s" % (m, m))
        norm_mirrors.append(m)
    cfg["registry_mirrors"] = norm_mirrors

    # ---- 额外端口映射(可选, 每个中间件都能追加自定义 host:container) ----
    extra = cfg.get("extra_ports") or {}
    norm_extra = {}
    used_hosts = set(norm_ports.values())
    for svc, rows in extra.items():
        if svc not in services or not rows:
            continue
        norm_rows = []
        seen_ctn = set()
        for r in rows:
            try:
                h, c = int(r.get("host")), int(r.get("container"))
            except (TypeError, ValueError):
                raise PackError("%s 自定义端口必须是数字: %s|%s custom ports must be numbers: %s" % (svc, r, svc, r))
            if not (1 <= h <= 65535 and 1 <= c <= 65535):
                raise PackError("%s 自定义端口超出范围 1-65535: %d:%d|%s custom ports out of range 1-65535: %d:%d" % (svc, h, c, svc, h, c))
            if h in used_hosts:
                raise PackError("%s 自定义宿主机端口 %d 与其他端口冲突|%s custom host port %d conflicts with another port" % (svc, h, svc, h))
            used_hosts.add(h)
            if c in seen_ctn:
                raise PackError("%s 同一容器端口重复映射: %d|%s duplicate container port mapping: %d" % (svc, c, svc, c))
            seen_ctn.add(c)
            norm_rows.append({"host": h, "container": c})
        norm_extra[svc] = norm_rows
    cfg["extra_ports"] = norm_extra

    # ---- 数据库备份策略(按数据库类型独立计划) ----
    b = cfg.get("backup") or {}
    engines_in = b.get("engines") if isinstance(b.get("engines"), dict) else {}
    # 兼容旧版单一计划配置: 顶层 days/hour/keep -> 三个引擎继承同一份计划
    if not engines_in and b.get("days") is not None:
        engines_in = {k: {"enabled": bool(b.get("enabled")), "days": b.get("days"),
                          "hour": b.get("hour"), "keep": b.get("keep")}
                      for k in ("mysql", "pg", "mongo")}
    eng_of = {"mysql57": "mysql", "mysql8": "mysql", "postgres": "pg", "mongodb": "mongo"}
    have_engines = {eng_of[s] for s in backupable_services({"services": services})}
    norm_eng = {}
    for key, label in (("mysql", "MySQL"), ("pg", "PostgreSQL"), ("mongo", "MongoDB")):
        e = engines_in.get(key) or {}
        enabled = bool(e.get("enabled"))
        if enabled and key not in have_engines:
            enabled = False
            warns.append("未部署 %s, 其备份计划已忽略|%s not selected, its backup plan is ignored" % (label, label))
        if enabled:
            try:
                days = sorted({int(d) for d in (e.get("days") or [])})
            except (TypeError, ValueError):
                raise PackError("%s 备份星期配置不合法|Invalid %s backup weekdays" % (label, label))
            if not days or [d for d in days if not (1 <= d <= 7)]:
                raise PackError("%s 请至少选择一个有效备份日 (1=周一 ... 7=周日)|%s: select at least one valid backup day (1=Mon ... 7=Sun)" % (label, label))
            try:
                hour, keep = int(e.get("hour", 3)), int(e.get("keep", 7))
            except (TypeError, ValueError):
                raise PackError("%s 备份时间/保留份数不合法|Invalid %s backup hour/retention" % (label, label))
            if not 0 <= hour <= 23:
                raise PackError("%s 备份小时必须 0-23|%s backup hour must be 0-23" % (label, label))
            if not 1 <= keep <= 999:
                raise PackError("%s 备份保留份数必须 1-999|%s backup retention must be 1-999" % (label, label))
            norm_eng[key] = {"enabled": True, "days": days, "hour": hour, "keep": keep}
        else:
            norm_eng[key] = {"enabled": False, "days": [], "hour": 3, "keep": 7}
    bdir = str(b.get("dir") or "/data/backup/db").strip()
    if not LINUX_PATH_RE.match(bdir) or bdir == "/":
        raise PackError("备份目录必须是 Linux 绝对路径, 如 /data/backup/db|Backup dir must be a Linux absolute path, e.g. /data/backup/db")
    cfg["backup"] = {"dir": bdir.rstrip("/"), "engines": norm_eng}

    # ---- Nginx 反向代理向导(勾选 NGINX 才生效) ----
    proxies = cfg.get("proxies") or []
    if proxies and "nginx" not in services:
        raise PackError("配置了反向代理站点, 但中间件未勾选 NGINX|Reverse-proxy sites configured but NGINX is not selected")
    norm_px = []
    if proxies:
        # 站点监听端口必须是 nginx 容器内实际映射到的端口(80/443/9000 + 自定义映射的容器端口)
        ctn_ports = {80, 443, 9000}
        for r in (cfg.get("extra_ports") or {}).get("nginx", []):
            ctn_ports.add(int(r["container"]))
        if len(proxies) > 20:
            raise PackError("反代站点数量过多(最多 20 个)|Too many proxy sites (max 20)")
        seen_lsn = set()
        for i, p in enumerate(proxies):
            p = dict(p or {})
            no = i + 1
            mode = p.get("mode") or "static"
            if mode not in ("static", "proxy"):
                raise PackError("反代站点 %d 模式不合法: %s|Proxy site %d invalid mode: %s" % (no, mode, no, mode))
            sn = str(p.get("server_name") or "").strip()
            if not sn or not re.match(r"^[A-Za-z0-9._\-*]+( +[A-Za-z0-9._\-*]+)*$", sn):
                raise PackError("反代站点 %d 域名不合法(多个用空格分隔, 通配用 *): %r|Proxy site %d invalid server_name (space-separated, wildcard *): %r" % (no, sn, no, sn))
            key = (int(p.get("listen") or 80), sn.lower())
            try:
                listen = int(p.get("listen") or 80)
            except (TypeError, ValueError):
                raise PackError("反代站点 %d 监听端口不合法" % no)
            if listen not in ctn_ports:
                raise PackError("反代站点 %d 监听端口 %d 未映射进 nginx 容器, 请先到端口映射为 NGINX 添加自定义映射(宿主机:%d → 容器:%d)"
                                % (no, listen, listen, listen))
            if key in seen_lsn:
                raise PackError("反代站点 %d 与其他站点重复: 端口 %d + 域名 %s" % (no, listen, sn))
            seen_lsn.add(key)
            body = str(p.get("body_size") or "500m").strip()
            if not re.match(r"^[0-9]+[kKmMgG]?$", body):
                raise PackError("反代站点 %d 上传大小限制不合法(如 100m): %s|Proxy site %d invalid body size (e.g. 100m): %s" % (no, body, no, body))
            ws = bool(p.get("ws"))
            ws_path = str(p.get("ws_path") or "/ws/").strip() or "/ws/"
            if ws and not re.match(r"^/[A-Za-z0-9_./-]*$", ws_path):
                raise PackError("反代站点 %d WebSocket 路径不合法: %s|Proxy site %d invalid WebSocket path: %s" % (no, ws_path, no, ws_path))
            trusted = []
            for cidr in (p.get("trusted_proxies") or []):
                cidr = str(cidr).strip()
                if not cidr:
                    continue
                if not re.match(r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$|^[A-Fa-f0-9:]+/\d{1,3}$", cidr):
                    raise PackError("反代站点 %d 可信代理网段不合法(应为 CIDR 如 10.0.0.0/8): %s|Proxy site %d invalid trusted proxy CIDR (e.g. 10.0.0.0/8): %s" % (no, cidr, no, cidr))
                trusted.append(cidr)

            # HTTPS 证书(随包分发, 部署即配好 SSL)
            ssl_on = bool(p.get("ssl"))
            cert_pem = str(p.get("cert_pem") or "")
            key_pem = str(p.get("key_pem") or "")
            redirect = bool(p.get("redirect", True))
            if ssl_on:
                if "-----BEGIN CERTIFICATE-----" not in cert_pem:
                    raise PackError("反代站点 %d 启用了 HTTPS 但证书文件无效, 请重新选择 fullchain.pem/.crt|Proxy site %d HTTPS enabled but certificate invalid, re-select fullchain.pem/.crt" % (no, no))
                if "ENCRYPTED" in key_pem:
                    raise PackError("反代站点 %d 私钥带密码保护, 暂不支持; 请先导出无密码私钥再上传|Proxy site %d private key is passphrase-protected (unsupported); export an unencrypted key first" % (no, no))
                if not re.search(r"-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----", key_pem):
                    raise PackError("反代站点 %d 启用了 HTTPS 但私钥文件无效, 请重新选择 .key 私钥|Proxy site %d HTTPS enabled but private key invalid, re-select the .key file" % (no, no))
                if len(cert_pem) > 200_000 or len(key_pem) > 200_000:
                    raise PackError("反代站点 %d 证书/私钥文件过大(>200KB), 请核对是否选错文件|Proxy site %d cert/key file too large (>200KB), check the file" % (no, no))
            else:
                cert_pem = key_pem = ""
                redirect = False

            api_prefix, root, spa, strip = "", "", False, False
            if mode == "static":
                root = str(p.get("root") or "").strip()
                if root and not LINUX_PATH_RE.match(root):
                    raise PackError("反代站点 %d 前端目录必须是容器内绝对路径: %s|Proxy site %d web root must be an absolute path inside the container: %s" % (no, root, no, root))
                spa = bool(p.get("spa"))
                api_prefix = str(p.get("api_prefix") or "").strip()
                if api_prefix and not re.match(r"^/[A-Za-z0-9_./-]*$", api_prefix):
                    raise PackError("反代站点 %d 接口前缀不合法: %s|Proxy site %d invalid API prefix: %s" % (no, api_prefix, no, api_prefix))
                if api_prefix and not api_prefix.endswith("/"):
                    api_prefix += "/"
            else:
                api_prefix = "/"

            target_host, target_port = "", 0
            if api_prefix:
                target = str(p.get("api_target") or "").strip().rstrip("/")
                m = re.match(r"^http://([A-Za-z0-9_.-]+)(?::([0-9]{1,5}))?$", target)
                if not m:
                    raise PackError("反代站点 %d 后端地址不合法(仅支持 http://主机[:端口], 主机可为 compose 服务名): %s|Proxy site %d invalid backend (only http://host[:port], host may be a compose service): %s" % (no, target, no, target))
                target_host = m.group(1)
                target_port = int(m.group(2) or 80)
                if not (1 <= target_port <= 65535):
                    raise PackError("反代站点 %d 后端端口超出范围: %d|Proxy site %d backend port out of range: %d" % (no, target_port, no, target_port))
                if target_host in ("127.0.0.1", "localhost", "::1"):
                    target_host = "host.docker.internal"
                    p["_gw"] = True
                    warns.append("反代站点 %d 后端填的是宿主机地址, 容器内已自动改用 host.docker.internal(host-gateway)|"
                                 "Proxy site %d backend points to the host; rewritten to host.docker.internal (host-gateway)" % (no, no))
                if target_host == "host.docker.internal":
                    p["_gw"] = True
                strip = bool(p.get("strip_prefix", api_prefix != "/"))
            norm_px.append({
                "mode": mode, "server_name": sn, "listen": listen,
                "root": root, "spa": spa, "api_prefix": api_prefix,
                "target_host": target_host, "target_port": target_port,
                "strip": strip, "ws": ws, "ws_path": ws_path,
                "body_size": body, "trusted_proxies": trusted,
                "ssl": ssl_on, "redirect": redirect,
                "cert_pem": cert_pem, "key_pem": key_pem,
                "_gw": bool(p.get("_gw")),   # 后端指向宿主机: nginx 容器需要 host-gateway
            })
    cfg["proxies"] = norm_px

    # ---- 数据库(nacos / xxljob) ----
    db = cfg.get("db") or {}
    local_mysqls = [s for s in ("mysql57", "mysql8") if s in services]
    for app, schema_default in (("nacos", "nacos"), ("xxljob", "xxl_job")):
        conf = db.get(app) or {}
        if app not in services:
            db[app] = None
            continue
        mode = conf.get("mode")
        if mode not in ("local", "external"):
            raise PackError("%s 必须选择数据库来源(local/external)|%s must choose a database source (local/external)" % (app, app))
        schema = str(conf.get("schema") or schema_default).strip()
        if not re.match(r"^[A-Za-z0-9_]+$", schema):
            raise PackError("%s 数据库名不合法: %s|%s invalid schema name: %s" % (app, schema, app, schema))
        conf["schema"] = schema
        if mode == "local":
            if not local_mysqls:
                raise PackError("%s 选择使用本次部署的 MySQL, 但本次未部署 MySQL|%s uses the bundled MySQL but MySQL is not selected" % (app, app))
            svc = conf.get("local_svc")
            if svc not in local_mysqls:
                svc = "mysql8" if "mysql8" in local_mysqls else local_mysqls[0]
            conf["local_svc"] = svc
            conf["host"] = svc            # compose 服务名
            conf["host_import"] = ""
            conf["port"] = 3306
            conf["user"] = "root"
            conf["password"] = secrets_cfg["MYSQL_ROOT_PASSWORD"]
            if is_multihost(cfg, svc):
                # 多机 MySQL: 主库在远端节点上, 主部署机的 nacos/xxljob 直连主节点 IP:宿主端口(写操作直达主库)
                conf["host"] = master_server_ip(cfg, svc)
                conf["host_import"] = conf["host"]
                conf["port"] = cfg["ports"][svc]
                conf["multihost"] = True
        else:
            host = str(conf.get("host", "")).strip()
            if not host:
                raise PackError("%s 外部数据库地址不能为空|%s external database host is required" % (app, app))
            try:
                port = int(conf.get("port", 3306))
            except (TypeError, ValueError):
                raise PackError("%s 外部数据库端口不合法|%s external database port invalid" % (app, app))
            if not (1 <= port <= 65535):
                raise PackError("%s 外部数据库端口超出范围|%s external database port out of range" % (app, app))
            user = str(conf.get("user", "")).strip()
            password = str(conf.get("password", ""))
            if not USERNAME_RE.match(user):
                raise PackError("%s 外部数据库账号不合法|%s external database user invalid" % (app, app))
            if not password:
                raise PackError("%s 外部数据库密码不能为空|%s external database password is required" % (app, app))
            if not PASSWORD_RE.match(password):
                raise PackError("%s 外部数据库密码含不合法字符(禁止 空格 和 $ ` \" ' \\ ; |)|%s external database password contains illegal characters (space and $ ` \" ' \\ ; | forbidden)" % (app, app))
            conf["port"] = port
            conf["user"] = user
            conf["password"] = password
            # 127.0.0.1/localhost 指的是宿主机: compose 内换成 host-gateway 别名
            if host in ("127.0.0.1", "localhost", "::1"):
                conf["host"] = "host.docker.internal"
                conf["host_import"] = "127.0.0.1"
                conf["extra_hosts"] = True
                warns.append("%s 外部数据库填的是本机地址, 容器内已自动改用 host.docker.internal(host-gateway); "
                             "请确认该数据库监听的是 0.0.0.0 而非仅 127.0.0.1|"
                             "%s external database uses a loopback address; rewritten to host.docker.internal (host-gateway). "
                             "Make sure it listens on 0.0.0.0, not just 127.0.0.1" % (app, app))
            else:
                conf["host"] = host
                conf["host_import"] = host
                conf["extra_hosts"] = False
        db[app] = conf
    cfg["db"] = db

    # ---- 仓库文件存在性 ----
    missing = check_warehouse(cfg, catalog)
    if missing:
        raise PackError("离线包物料缺失, 请补齐后再打包:\n  - %s|Offline bundle files missing, add them to the warehouse first:\n  - %s"
                        % ("\n  - ".join(missing), "\n  - ".join(missing)))
    return cfg, warns


def check_warehouse(cfg, catalog):
    """校验本次打包需要的物料文件是否存在, 返回缺失列表"""
    arch = cfg["arch"]
    missing = []
    docker_tgz = BASE_DIR / catalog["docker"]["packages"][arch]
    compose_bin = BASE_DIR / catalog["compose"]["packages"][arch]
    if not docker_tgz.is_file():
        missing.append(str(catalog["docker"]["packages"][arch]))
    if not compose_bin.is_file():
        missing.append(str(catalog["compose"]["packages"][arch]))

    need_imgs = {}
    kafka_cluster = "kafka" in cfg["services"] and (cfg.get("topology") or {}).get("kafka") == "cluster"
    for s in cfg["services"]:
        if s == "kafka" and kafka_cluster:
            continue   # 集群形态用 bitnami 镜像, 单节点镜像 tar 不需要
        meta = catalog["services"][s]
        need_imgs[meta["images"][arch]] = True
    if kafka_cluster:
        kc = getattr(PLUGINS.get("kafka"), "CLUSTER", None)
        if kc and kc["images"][arch]:
            need_imgs[kc["images"][arch]] = True
    if (cfg.get("features") or {}).get("proxysql"):
        need_imgs[PROXYSQL_META["images"][arch]] = True
    # 外部库导表需要 mysql 客户端镜像
    needs_client = False
    for a in ("nacos", "xxljob"):
        conf = (cfg.get("db") or {}).get(a) or {}
        if conf.get("mode") == "external":
            needs_client = True
    has_local_mysql = any(s in cfg["services"] for s in ("mysql57", "mysql8"))
    if needs_client and not has_local_mysql:
        img = catalog["services"]["mysql8"]["images"][arch]
        need_imgs[img] = True
    for rel in need_imgs:
        if not (BASE_DIR / rel).is_file():
            missing.append(rel)
    return missing
