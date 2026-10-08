# -*- coding: utf-8 -*-
"""K8s 集群: inventory/config 生成, 离线物料收集与自动补齐"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

from .catalog import load_catalog
from .paths import BASE_DIR, CLUSTER_SH, DIST_DIR, WAREHOUSE, log
from .util import PackError, tr
from .validate import _ETCD_ENV_ORDER


# ---------------------------------------------------------------- K8s 集群

def _yq(v):
    """标量转 YAML 安全字面量(JSON 引号规则是 YAML 引号规则的子集)"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def gen_cluster_inventory(cfg):
    """kk Inventory: 集群节点 = 服务器池中分配了角色的机器"""
    nodes = cfg["cluster"]["nodes"]
    lines = [
        "# 由 packer.py 生成, 勿手改; 变更请在打包页面修改后重新打包",
        "apiVersion: kubekey.kubesphere.io/v1",
        "kind: Inventory",
        "metadata:",
        '  name: "%s-cluster"' % cfg["project"],
        "spec:",
        "  hosts:",
    ]
    for n in nodes:
        lines += [
            "    %s:" % n["name"],
            "      connector:",
            "        type: ssh",
            '        host: %s' % _yq(n["ip"]),
            "        port: %d" % n["ssh"],
            "        user: %s" % _yq(n["user"]),
            '        password: %s' % _yq(n["password"]),
            "      internal_ipv4: %s" % _yq(n["ip"]),
        ]
    cp = [n["name"] for n in nodes if n["role"] == "control-plane"]
    wk = [n["name"] for n in nodes if n["role"] == "worker"]
    rg = [n["name"] for n in nodes if n["role"] == "registry"]
    lines += ["  groups:", "    k8s_cluster:", "      groups:", "        - kube_control_plane",
              "        - kube_worker", "    kube_control_plane:", "      hosts:"]
    lines += ["        - %s" % n for n in cp] or ["        # (空)"]
    lines += ["    kube_worker:", "      hosts:"]
    lines += ["        - %s" % n for n in wk] or ["        # (空)"]
    # kk 要求 etcd 组显式非空(默认堆叠 etcd 部署在控制面节点上)
    lines += ["    etcd:", "      hosts:"]
    lines += ["        - %s" % n for n in cp] or ["        # (空)"]
    if rg:
        lines += ["    image_registry:", "      hosts:"] + ["        - %s" % n for n in rg]
    return "\n".join(lines) + "\n"


def gen_cluster_config(cfg, catalog):
    """kk Config: 按页面集群配置生成完整 spec(离线/在线/HA/目录/NTP/存储/镜像仓库/运行时参数)"""
    cl = cfg["cluster"]
    ver = catalog["cluster"]["versions"][cl["kube_version"]]
    cni_type = cl["cni_type"]
    # CNI 版本跟随所选类型(条目按 kk per-minor vars 携带各 CNI 配套版本)
    cni_versions = ver.get("cni_versions") or {}
    cni_version = cni_versions.get(cni_type) or ver.get("cni_plugin", {}).get("version", "")
    cni_key = cni_type + "_version"
    mode = cl["mode"]
    online = mode == "online"
    comp = cl.get("components") or {}
    kubelet = cl.get("kubelet") or {}
    ntp = cl.get("ntp") or {}
    storage = cl.get("storage") or {}
    imgreg = cl.get("image_registry") or {}
    ha_type = cl.get("ha_type") or "local"

    # zone: cn 时在线模式的镜像/二进制自动走国内源(hub.kubesphere.com.cn 等); 离线模式保持为空
    zone_cn = online and (cl.get("zone") or "cn") == "cn"
    lines = [
        "# 由 packer.py 生成, 勿手改; 变更请在打包页面修改后重新打包",
        "apiVersion: kubekey.kubesphere.io/v1",
        "kind: Config",
        "metadata:",
        '  name: "%s-cluster"' % cfg["project"],
        "spec:",
        "  zone: %s" % _yq("cn" if zone_cn else ""),
    ]
    # zone=cn 仅由 spec.zone 生效: 在线+国区交给 kk 内置 CN 映射
    # (hub.kubesphere.com.cn/{kubernetes,coredns,flannel-io}), 不显式覆盖 registry.imageRepository —
    # 裸域名会让 etcd/coredns 丢掉命名空间(hub 实测 400/404)
    lines += [
        "  kubernetes:",
    ]
    if not zone_cn:
        # sandbox 三元组必须写全: kk containerd 配置模板用这三个键拼 sandbox 镜像,
        # 缺 registry/repository 会渲染出非法值导致容器沙箱创建失败;
        # 在线+cn 不写, 由 kk 按 zone 渲染国内 sandbox(hub.kubesphere.com.cn/kubernetes/pause)
        lines += [
            "    sandbox_image:",
            '      registry: "registry.k8s.io"',
            '      repository: "pause"',
            "      tag: %s" % _yq(ver.get("sandbox_image_tag") or "3.9"),
        ]
    lines += [
        "    kube_version: %s" % _yq(cl["kube_version"]),
        "    kube_proxy:",
        "      mode: %s" % _yq(cl["proxy_mode"]),
        "    control_plane_endpoint:",
        "      type: %s" % _yq(ha_type),
    ]
    if ha_type in ("kube-vip", "haproxy") and cl.get("ha_vip"):
        lines += ["      host: %s" % _yq(cl["ha_vip"])]
    if ha_type == "kube-vip" and cl.get("ha_vip"):
        # kube-vip 静态 pod 的 vip_address 取 kube_vip.address(不能为空), 网卡按该地址所在网段自动发现
        lines += ["      kube_vip:", "        address: %s" % _yq(cl["ha_vip"])]

    # kubelet 参数(可选覆盖); root_dir 通过 extra_args 的 root-dir 生效
    # 注意: kk 会给每个 arg 自动加 "--" 前缀, 这里不能自带横杠(否则变成 ----root-dir)
    kubelet_lines = []
    kubelet_args = [a.lstrip("-") for a in (kubelet.get("extra_args") or [])]
    if kubelet.get("root_dir"):
        kubelet_args.insert(0, "root-dir=" + kubelet["root_dir"])
    if kubelet_args:
        kubelet_lines.append("      extra_args:")
        for a in kubelet_args:
            k, _, v = a.partition("=")
            kubelet_lines.append("        %s: %s" % (_yq(k.strip()), _yq(v.strip())))
    if kubelet.get("max_pods"):
        kubelet_lines.append("      max_pods: %d" % int(kubelet["max_pods"]))

    if kubelet.get("extra_config"):
        kubelet_lines.append("      extra_config:")
        for ln in kubelet["extra_config"].splitlines():
            kubelet_lines.append("      " + ln if ln.strip() else "")
        while kubelet_lines and kubelet_lines[-1] == "      ":
            kubelet_lines.pop()
    if kubelet_lines:
        lines += ["    kubelet:"] + kubelet_lines

    # kubeadm 配置备份(kk post_install 每次部署带时间戳备份 /etc/kubernetes/kubeadm-config.yaml;
    # 显式写出便于用户改路径, 空串=显式禁用 — kk 默认开启, 不写无法关闭)
    lines += [
        "    backup:",
        "      kubeadm_config_dir: %s" % _yq(cl.get("kubeadm_config_dir") if cl.get("kubeadm_config_dir") is not None else "/etc/kubekey/backup/kubernetes"),
    ]
    if cl.get("certs_renew") is False:
        lines += ["    certs:", "      renew: false"]

    # CRI: 运行时选择 + containerd 数据目录/静态构建(glibc 老系统)/版本覆盖 + docker data-root + registry mirrors
    # kk config 结构: cri.container_manager(containerd/docker) + cri.containerd_version(直属 cri!) +
    #                 cri.containerd.{static_binary, data_root}, cri.docker.data_root, cri.registry.mirrors
    # data_root 是 kk containerd 模板认的键(模板里 set $config "root" data_root), 不能写 config.root
    use_containerd = (cl.get("container_manager") or "containerd") == "containerd"
    cri_lines = ["    container_manager: %s" % _yq(cl.get("container_manager") or "containerd")]
    if use_containerd:
        cd_lines = []
        if comp.get("containerd_version_override"):
            cd_lines.append("    containerd_version: %s" % _yq(comp["containerd_version_override"]))
        cd_sub = []
        if comp.get("static_binary"):
            cd_sub.append("      static_binary: true")
        if comp.get("containerd_root"):
            cd_sub.append("      data_root: %s" % _yq(comp["containerd_root"]))
        if not online:
            # 纯离线: 部署脚本先预装 containerd 并导入镜像, config_policy=overwrite 让 kk 每次都重写
            # 配置并重启(镜像落在 data_root 磁盘存储, 重启不丢); 否则版本一致时 kk 跳过配置,
            # 节点会带着部署脚本的精简配置或默认配置进入 kubeadm(sandbox 镜像不对)
            cd_sub.append("      config_policy: overwrite")
        if cd_lines:
            cri_lines += cd_lines
        if cd_sub:
            cri_lines += ["    containerd:"] + cd_sub
    if comp.get("docker_root"):
        cri_lines += ["    docker:", "      data_root: %s" % _yq(comp["docker_root"])]
    if cl.get("registry_mirrors"):
        cri_lines += ["    registry:", "      mirrors: [%s]" % ", ".join(_yq(m) for m in cl["registry_mirrors"])]
    lines += ["  cri:"] + cri_lines

    # etcd: 堆叠内部署 + 数据目录/高级参数(必须挂在 env 下 — kk etcd 模板只读 .etcd.env.*,
    # 顶层 etcd.data_dir 是旧版 kk 的键, v4 会静默忽略导致数据仍落 /var/lib/etcd)
    etcd_lines = ['    deployment_type: "internal"']
    etcd_ver = ((ver.get("components") or {}).get("etcd") or {}).get("version")
    if etcd_ver:
        etcd_lines.append("    etcd_version: %s" % _yq(etcd_ver))
    etcd_env = comp.get("etcd_env") or {}
    if isinstance(etcd_env, list):
        # 容错: 未过 validate_config 的原始配置是 ["k=v", ...] 列表, 归一成 dict
        etcd_env = {str(kv).partition("=")[0].strip(): str(kv).partition("=")[2].strip()
                    for kv in etcd_env if str(kv).strip()}
    env_lines = []
    if comp.get("etcd_dir"):
        env_lines.append("      data_dir: %s" % _yq(comp["etcd_dir"]))
    for k in _ETCD_ENV_ORDER:
        if k in etcd_env:
            env_lines.append("      %s: %s" % (k, _yq(etcd_env[k])))
    if env_lines:
        etcd_lines.append("    env:")
        etcd_lines += env_lines
    lines += ["  etcd:"] + etcd_lines

    cni_lines = [
        "    type: %s" % _yq(cni_type),
        "    %s: %s" % (cni_key, _yq(cni_version)),
        "    pod_cidr: %s" % _yq(cl["pod_cidr"]),
        "    ipv4_mask_size: %d" % int(cl.get("ipv4_mask_size") or 24),
        "    service_cidr: %s" % _yq(cl["service_cidr"]),
    ]
    if cl.get("multi_cni") == "multus":
        # Multi-CNI: multus (kk cni.multi_cni + 镜像 tag 默认 v4.3.0, 见 roles/defaults 04-cni.yaml)
        cni_lines += ["    multi_cni: multus"]
        if cl.get("multi_cni_tag"):
            cni_lines += ["    multus:", "      image:", "        tag: %s" % _yq(cl["multi_cni_tag"])]
    if cni_type == "calico" and cl.get("calico_values"):
        # Calico 专属调优走 helm values 透传(kk calico 任务以 -f 追加): ipipMode/vxlanMode/mtu 等
        cni_lines.append("    calico:")
        cni_lines.append("      values: |")
        cni_lines += ["        " + ln for ln in cl["calico_values"].splitlines()]
    lines += ["  cni:"] + cni_lines

    # DNS 覆盖(coredns/nodelocaldns 镜像 tag 与启停; 不设置时跟随 kk per-minor 默认)
    dns_cfg = cl.get("dns") or {}
    dns_lines = []
    if dns_cfg.get("coredns_tag"):
        dns_lines += ["    coredns:", "      image:", "        tag: %s" % _yq(dns_cfg["coredns_tag"])]
    if dns_cfg.get("nodelocaldns_enabled") is False:
        dns_lines += ["    nodelocaldns:", "      enabled: false"]
    elif dns_cfg.get("nodelocaldns_tag"):
        dns_lines += ["    nodelocaldns:", "      image:", "        tag: %s" % _yq(dns_cfg["nodelocaldns_tag"])]
    if dns_lines:
        lines += ["  dns:"] + dns_lines

    # 私有镜像仓库部署(harbor/docker-registry; 部署在 registry 角色节点)
    if imgreg.get("type") in ("harbor", "docker-registry"):
        lines += [
            "  image_registry:",
            "    type: %s" % _yq(imgreg["type"]),
        ]
        if imgreg.get("vip"):
            lines.append("    ha_vip: %s" % _yq(imgreg["vip"]))

    # 存储配置(localpv / nfs storageclass): 必须显式写出 — kk 默认启用 localpv(enabled+default),
    # 不写会被静默当成已启用, 离线包没带 localpv chart/镜像时部署会折在存储类这一步
    lines += [
        "  storage_class:",
        "    local:",
        "      enabled: %s" % ("true" if storage.get("localpv_enabled") else "false"),
        "      default: %s" % ("true" if (storage.get("localpv_enabled") and not storage.get("nfs_enabled")) else "false"),
    ]
    if storage.get("localpv_enabled") and storage.get("localpv_path"):
        lines.append("      path: %s" % _yq(storage["localpv_path"]))
    if storage.get("nfs_enabled"):
        lines += [
            "    nfs:",
            "      enabled: true",
            "      default: %s" % _yq(bool(storage.get("nfs_default"))),
        ]
        if storage.get("nfs_server"):
            lines.append("      server: %s" % _yq(storage["nfs_server"]))
        if storage.get("nfs_path"):
            lines.append("      path: %s" % _yq(storage["nfs_path"]))

    lines += [
        "  native:",
        "    timezone: %s" % _yq(cl["timezone"]),
    ]
    if cl.get("set_hostname") is False:
        lines.append("    set_hostname: false")
    lines += [
        "    ntp:",
        "      enabled: %s" % _yq(bool(ntp.get("enabled"))),
        "      servers:",
    ]
    lines += [f"        - {_yq(s)}" for s in (ntp.get("servers") or [])] or ["        # (空)"]

    k8s_reg = cl.get("k8s_image_registry") or ""
    if online:
        lines += [
            "  # 在线安装: fetch=true, 组件与镜像联网下载(zone=cn 走国内源)",
            "  download:",
            "    fetch: true",
        ]
        if k8s_reg:
            lines += ["    images:", "      registry: %s" % _yq(k8s_reg)]
    else:
        lines += [
            "  download:",
            "    fetch: false",
            '    artifact_file: ""',
            '    artifact_md5: ""',
        ]
    lines += [
        "  cluster_require:",
        "    # 兜底: 未打补丁的官方 kk 也能过发行版 precheck; 打补丁构建无副作用",
        "    allow_unsupported_distribution_setup: true",
    ]
    return "\n".join([ln for ln in lines if ln is not None]) + "\n"


_PREPARE = None


def _prepare_module():
    """惰性加载 prepare_cluster 模块(离线镜像清单/收集函数的单一来源, 打包与备料两处共用)"""
    global _PREPARE
    if _PREPARE is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("kk_prepare", BASE_DIR / "prepare_cluster.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PREPARE = mod
    return _PREPARE


def _tar_valid(path):
    """tar.gz/tgz 完整性快速校验(损坏返回 False)"""
    try:
        import tarfile
        with tarfile.open(path) as tf:
            tf.getmembers()
        return True
    except Exception:
        return False


def cluster_materials(cfg, catalog):
    """集群物料: 返回 (missing, [(bundle内相对路径, 源Path)])。online 模式仅需 kk 与部署脚本。"""
    cl = cfg["cluster"]
    ccat = catalog["cluster"]
    arch = cfg["arch"]
    missing, items = [], []

    kk_rel = ccat["kk"]["binaries"].get(arch)
    kk_path = BASE_DIR / kk_rel if kk_rel else None
    if kk_path and kk_path.is_file():
        items.append(("cluster/kk", kk_path))
    else:
        missing.append("kk 二进制(%s, 须为打过信创补丁的构建, 放置后重新打包即可): %s (维护机一条命令生成: python prepare_cluster.py --kk-build; 打包/部署全程不需要编译)" % (arch, kk_rel))

    if cl["mode"] == "artifact":
        fname = ccat["artifact"]["filename_pattern"].format(kube_version=cl["kube_version"], arch=arch)
        art = BASE_DIR / ccat["artifact"]["warehouse_dir"] / fname
        if art.is_file():
            items.append(("cluster/" + fname, art))
        else:
            missing.append("kk artifact 产物: %s" % art)
    elif cl["mode"] == "online":
        pass   # 在线安装: 组件与镜像部署时联网下载(zone=cn 走国内源), 无本地物料
    else:
        ver = ccat["versions"][cl["kube_version"]]
        comps = ver.get("components") or {}
        cni_type = cl["cni_type"]
        cni_version = (ver.get("cni_versions") or {}).get(cni_type) or ver.get("cni_plugin", {}).get("version", "")
        # containerd 版本覆盖: 老内核节点(如 CentOS7 3.10 不受 containerd 2.x 支持)降级 1.7.x,
        # 仓库目录与包内 cache 布局同步切换到覆盖版本
        ct_ver_override = ((cl.get("components") or {}).get("containerd_version_override") or "").strip()
        cache_map = {"kube": "kube", "etcd": "etcd", "cni_plugins": "cni/plugins",
                     "helm": "helm", "crictl": "crictl", "containerd": "containerd", "runc": "runc"}
        for key, cache_sub in cache_map.items():
            comp = comps.get(key) or {}
            wdir_rel = comp.get("warehouse_dir")
            if not wdir_rel:
                continue
            layout = comp.get("cache_layout") or ("%s/{arch}" % cache_sub)
            if key == "containerd" and ct_ver_override:
                wdir_rel = re.sub(r"(containerd/)v[^/]+(/)", r"\g<1>%s\g<2>" % ct_ver_override, wdir_rel)
                layout = re.sub(r"(containerd/)v[^/]+(/)", r"\g<1>%s\g<2>" % ct_ver_override, layout)
            wdir = BASE_DIR / wdir_rel.format(arch=arch)
            sub = layout.format(arch=arch)
            # 排除断点续传/临时文件(xxx.p0 / xxx.part2 / xxx.tmp), 避免残缺物料混进包里;
            # tar.gz/tgz 额外做完整性校验(防中断残留的半截文件带病进包)
            real = []
            for f in wdir.glob("**/*"):
                if not f.is_file() or re.search(r"\.(p\d+|part\d*|tmp)$", f.name):
                    continue
                if f.suffix in (".tgz", ".gz") and not _tar_valid(f):
                    continue
                real.append(f)
            if not wdir.is_dir() or not real:
                missing.append("二进制缓存目录为空: %s" % wdir)
            else:
                items.append(("cluster/cache/%s" % sub, wdir))
                if key == "kube":
                    for b in ("kubeadm", "kubelet", "kubectl"):
                        if not (wdir / b).is_file():
                            missing.append("缺 %s: 应位于 %s/%s" % (b, wdir, b))
        # 纯离线镜像: 打包时收集(docker pull+save, 原生 tag), 部署前由 deploy-cluster.sh
        # 导入节点本地 containerd; 缺失时先记 missing, 打包流程会尝试自动收集
        img_src = BASE_DIR / "warehouse" / "cluster" / "images" / \
            _prepare_module().cluster_images_tar_name(cl["kube_version"], cl["cni_type"], arch)
        if img_src.is_file():
            items.append(("cluster/images/" + img_src.name, img_src))
        else:
            missing.append("k8s 离线镜像包: %s (需 Docker Desktop 自动收集)" % img_src)

        # CNI chart(+calicoctl): kk 部署 CNI 时 helm install binary_dir 下的固定路径 chart,
        # bundle 内放在 cluster/cache/cni/<type>/ 随缓存整体铺到 binary_dir 即命中
        chart_dir = BASE_DIR / "warehouse" / "cluster" / "charts" / cni_type
        chart_map = {"flannel": ["flannel-%s.tgz" % cni_version],
                     "calico": ["tigera-operator-%s.tgz" % cni_version],
                     "cilium": ["cilium-%s.tgz" % cni_version],
                     "kubeovn": ["kube-ovn-%s.tgz" % cni_version]}
        expect_charts = chart_map.get(cni_type) or []
        if expect_charts:
            miss_charts = [f for f in expect_charts if not (chart_dir / f).is_file()]
            if miss_charts:
                missing.append("CNI chart 物料: %s (应位于 %s/)" % (", ".join(miss_charts), chart_dir))
            else:
                items.append(("cluster/cache/cni/%s" % cni_type, chart_dir))
            if cni_type == "calico" and not miss_charts:
                ctl = chart_dir / cni_version / arch / ("calicoctl-linux-%s" % arch)
                if not ctl.is_file():
                    missing.append("calicoctl 二进制: %s" % ctl)

        # 存储 chart(localpv/nfs provisioner)
        storage = cl.get("storage") or {}
        if storage.get("localpv_enabled"):
            lp = ((ver.get("components") or {}).get("localpv") or {}).get("version") or "4.4.0"
            lp_chart = BASE_DIR / "warehouse" / "cluster" / "charts" / "localpv" / ("localpv-provisioner-%s.tgz" % lp)
            if lp_chart.is_file():
                items.append(("cluster/cache/storageclass/local", lp_chart.parent))
            else:
                missing.append("localpv chart: %s" % lp_chart)
        if storage.get("nfs_enabled"):
            nf = ((ver.get("components") or {}).get("nfs") or {}).get("version") or "v4.0.18"
            nf_chart = BASE_DIR / "warehouse" / "cluster" / "charts" / "nfs" / ("nfs-subdir-external-provisioner-%s.tgz" % nf)
            if nf_chart.is_file():
                items.append(("cluster/cache/storageclass/nfs", nf_chart.parent))
            else:
                missing.append("nfs provisioner chart: %s" % nf_chart)

        # 升级目标版本的离线镜像包: 升级只会替换 kube 三件套, 新版本控制面/CoreDNS 镜像
        # 需随包携带并由 upgrade-cluster.sh 导入, 否则 air-gapped 升级会联网拉镜像失败
        up_to = ((cl.get("upgrade_to") or "")).strip()
        if up_to:
            pm = _prepare_module()
            up_src = BASE_DIR / "warehouse" / "cluster" / "images" / \
                pm.cluster_images_tar_name(up_to, cni_type, arch)
            if up_src.is_file():
                items.append(("cluster/upgrade/images-%s.tar" % up_to, up_src))
            else:
                missing.append("离线镜像包(升级 %s): %s (打包时自动收集)" % (up_to, up_src))
            # kk upgrade --all 的二进制阶段按目标版本 manifest 取全套组件(kube/crictl/helm/
            # etcd/cni/containerd/runc), 而基础包缓存是部署版本的组件(如 helm v3.12.1):
            # 目标版本换了 minor 组件版本就对不上, 必须整套随包由 upgrade-cluster.sh 铺进
            # kk 缓存(真机 v1.28.15->v1.29.15 首测先后踩坑 crictl v1.29.0 与 helm v3.13.3 缺失)
            up_comps = ((ccat["versions"].get(up_to) or {}).get("components") or {})
            for key, comp in up_comps.items():
                if key == "kube":
                    continue   # kube 三件套由 pack 侧 cluster/upgrade/<版本>/ 专门携带
                wdir_rel = comp.get("warehouse_dir")
                if not wdir_rel:
                    continue
                layout = comp.get("cache_layout") or ""
                cw = BASE_DIR / wdir_rel.format(arch=arch)
                if cw.is_dir() and any(f.is_file() for f in cw.glob("**/*")):
                    items.append(("cluster/upgrade/cache/%s" % layout.format(arch=arch), cw))
                else:
                    missing.append("升级组件 %s(%s): %s (python prepare_cluster.py --download 可补)" % (key, up_to, cw))
    if not CLUSTER_SH.is_file():
        missing.append("集群部署脚本模板缺失: %s" % CLUSTER_SH)

    for d in cl["os_distros"]:
        d_dir = BASE_DIR / ccat["os_packages_dir"].format(distro=d, arch=arch)
        if d_dir.is_dir() and any(d_dir.iterdir()):
            items.append(("cluster/os/%s" % d, d_dir))
        else:
            missing.append("OS 依赖包目录为空: %s (不需要可取消勾选该发行版)" % d_dir)
    return missing, items


def gen_upgrade_sh(cfg):
    """一键集群升级脚本: 导入目标版本离线镜像 → 铺三件套到 kk 缓存 → kk upgrade cluster"""
    cl = cfg["cluster"]
    up_to = cl["upgrade_to"]
    catalog = load_catalog()
    sandbox = "registry.k8s.io/pause:%s" % (((catalog["cluster"]["versions"].get(up_to) or {})
                                             .get("sandbox_image_tag") or ""))
    return r"""#!/usr/bin/env bash
# 一键集群升级脚本 (由 packer.py 生成; 目标版本: {ver})
# 前提: 集群已通过 ./deploy.sh 部署完成; 纯离线升级(不在线拉取任何镜像)
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ctr/kubectl 等装在 /usr/local/bin: cron/CI 等非登录 shell 的 PATH 不含它
export PATH="/usr/local/bin:/usr/local/sbin:$PATH"
[ -f manifest.sh ] || { echo "[FAIL] 缺少 manifest.sh"; exit 1; }
source ./manifest.sh
[ "${CLUSTER_ENABLED:-0}" = "1" ] || { echo "[FAIL] 本包未启用集群"; exit 1; }

KK_HOME="$PWD/kubekey"; KK_CACHE="$KK_HOME/kubekey"

# SSH 包装(与 deploy-cluster.sh 同源): 凭据取 cluster/inventory.yaml
SSH_PORT="$(sed -n 's/^        port: \([0-9][0-9]*\).*/\1/p' cluster/inventory.yaml | head -1)"; SSH_PORT="${SSH_PORT:-22}"
SSH_USER="$(sed -n 's/^        user: \(.*\)$/\1/p' cluster/inventory.yaml | head -1 | tr -d '"')"; SSH_USER="${SSH_USER:-root}"
PW="$(sed -n 's/^        password: \(.*\)$/\1/p' cluster/inventory.yaml | head -1 | tr -d '"')"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=8 -p $SSH_PORT"
# scp 的端口参数是大写 -P(-p 是保留时间戳), 不能与 ssh 共用一份 OPTS
SCP_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=8 -P $SSH_PORT"
if [ -n "$PW" ] && command -v sshpass >/dev/null 2>&1; then
  RUN_SSH() { SSHPASS="$PW" sshpass -e ssh $SSH_OPTS "$@"; }
  RUN_SCP() { SSHPASS="$PW" sshpass -e scp $SCP_OPTS "$@"; }
else
  RUN_SSH() { ssh $SSH_OPTS -o BatchMode=yes "$@"; }
  RUN_SCP() { scp $SCP_OPTS "$@"; }
fi

# 节点侧导入脚本: 写临时文件后 bash -s 下发(与 deploy-cluster.sh 的 IMP_SH 同一套路)。
# 不能用 _script 变量 + bash -c 嵌套引号: sed 行的 \\" 转义经两层 shell 解析必炸(unexpected EOF)
IMP_SH="$(mktemp /tmp/.kk-up-import.XXXXXX.sh)"
cat > "$IMP_SH" <<UP_EOF
for t in /tmp/.kk-up/*.tar; do
  [ -f "\$t" ] || continue
  ctr -n k8s.io images import --all-platforms "\$t" >/dev/null 2>&1 || echo "[WARN] \$(basename \$t) 导入部分报错(多为已存在)"
done
if [ -n "{sandbox}" ] && [ -s /etc/containerd/config.toml ]; then
  # 新版本 pause 镜像可能变化, 改写 sandbox 后重启 containerd(磁盘存储不丢镜像)
  sed -i "s#sandbox_image = \\"registry.k8s.io/pause:[^\\"]*\\"#sandbox_image = \\"{sandbox}\\"#" /etc/containerd/config.toml
  systemctl restart containerd
  sleep 2
fi
rm -rf /tmp/.kk-up
UP_EOF
import_one() {
  # $1 = LOCAL 或 user@ip
  if [ "$1" = "LOCAL" ]; then bash "$IMP_SH"; else RUN_SSH "$1" 'bash -s' < "$IMP_SH"; fi
}

echo "[INFO] 导入目标版本离线镜像 ({ver})"
LOCAL_IPS="$(hostname -I 2>/dev/null || true)"
for entry in ${CLUSTER_NODES[@]:-}; do
  IP="$(echo "$entry" | cut -d'|' -f2)"
  if echo "$LOCAL_IPS" | grep -qw "$IP"; then
    mkdir -p /tmp/.kk-up && cp -a cluster/upgrade/images-*.tar /tmp/.kk-up/ 2>/dev/null || true
    import_one LOCAL
  else
    RUN_SSH "${SSH_USER}@${IP}" "mkdir -p /tmp/.kk-up" || { echo "[FAIL] 节点 $IP 不可达"; exit 1; }
    RUN_SCP -q cluster/upgrade/images-*.tar "${SSH_USER}@${IP}:/tmp/.kk-up/" || { echo "[FAIL] 节点 $IP 镜像分发失败"; exit 1; }
    import_one "${SSH_USER}@${IP}"
  fi
done
rm -f "$IMP_SH"

# 0) 铺目标版本全套组件缓存到 kk 缓存(upgrade --all 的二进制阶段按目标版本 manifest
#    取 crictl/helm/etcd/cni/containerd/runc 全套; 只带 kube 会连环缺 crictl/helm)
if [ -d cluster/upgrade/cache ]; then
  cp -a cluster/upgrade/cache/. "$KK_CACHE/"
  echo "[INFO] 目标版本组件缓存已就位 ($KK_CACHE)"
fi
# 1) 铺目标版本 kube 三件套到 kk 缓存
mkdir -p "$KK_CACHE/kube/{ver}/{arch}"
cp -a cluster/upgrade/{ver}/{arch}/. "$KK_CACHE/kube/{ver}/{arch}/"
chmod +x "$KK_CACHE/kube/{ver}/{arch}/"*
echo "[INFO] 目标版本 {ver} 二进制已就位 ($KK_CACHE/kube/{ver}/{arch})"
# 2) 执行升级 (kubeadm 只允许单 minor 递增, kk 会自动拆步; --all 连带组件)
echo "[INFO] 开始升级: $CLUSTER_KUBE_VERSION -> {ver} (耗时较长, 请勿中断)"
./cluster/kk upgrade cluster -i cluster/inventory.yaml -c cluster/config.yaml \
  --workdir "$KK_HOME" \
  --with-kubernetes {ver} --all
echo "[INFO] 升级完成, 验证:"
KUBECONFIG=/etc/kubernetes/admin.conf kubectl get nodes -o wide
""".replace("{ver}", up_to).replace("{arch}", cfg["arch"]).replace("{sandbox}", sandbox)


def _auto_download_cluster_materials(cfg, catalog, cl_missing, prog):
    """打包时自动补齐缺失的集群二进制物料(调 prepare_cluster.py 的下载器)。
    返回 {"downloaded_mb": float, "warnings": [...]}; 无法自动生成的物料(kk/artifact)提示人工途径。"""
    arch = cfg["arch"]
    cl = cfg["cluster"]
    res = {"downloaded_mb": 0.0, "warnings": []}
    # 判定缺失项是否全部为可自动下载的缓存组件; kk/artifact 缺失不走自动下载
    needs_kk = any("kk 二进制" in m for m in cl_missing)
    needs_artifact = any("artifact 产物" in m for m in cl_missing)
    needs_cache = any("二进制缓存目录" in m or "缺 kubeadm" in m or "缺 kubelet" in m or "缺 kubectl" in m for m in cl_missing)
    needs_images = any("离线镜像包" in m for m in cl_missing)
    needs_charts = any("chart" in m or "calicoctl" in m for m in cl_missing)
    if needs_kk:
        res["warnings"].append("缺 kk 二进制: 运行 python prepare_cluster.py --kk-build 生成构建脚本"
                               "|Missing kk binary: run prepare_cluster.py --kk-build")
    if needs_artifact:
        res["warnings"].append("缺 artifact 产物: 打包时自动构建(需 Docker Desktop), 或运行 python prepare_cluster.py --artifact-export 后执行生成的 .bat"
                               "|Missing artifact: auto-built at pack time (Docker Desktop), or run prepare_cluster.py --artifact-export then the generated .bat")
    if not needs_cache and not needs_images and not needs_charts:
        return res

    pm_path = BASE_DIR / "prepare_cluster.py"
    if not pm_path.is_file():
        res["warnings"].append("缺 prepare_cluster.py, 无法自动下载")
        return res
    try:
        pm = _prepare_module()
    except Exception as e:
        res["warnings"].append("备料脚本加载失败: %s" % e)
        return res

    up_to = (cl.get("upgrade_to") or "").strip()
    if up_to:
        # 打包时缺升级物料同样自动下载; 升级三件套已在仓库则无需再下
        kube_dir = BASE_DIR / ("warehouse/cluster/kube/%s/%s" % (up_to, arch))
        if all((kube_dir / b).is_file() for b in ("kubeadm", "kubelet", "kubectl")):
            up_to = ""
    prog.stage("下载缺失集群物料")
    prog._downloads.clear()

    def cb(event, name, done, total):
        if event == "start":
            if total:
                info_msg = "需下载 %.0f MB" % (total / 1048576)
                prog.file(info_msg, total)
        elif event == "dl":
            prog._downloads[name] = [done, total]
            prog.file("%s (%.1f/%.1f MB)" % (name, done / 1048576, total / 1048576), total)
        elif event == "done":
            prog._downloads.pop(name, None)

    try:
        if needs_cache:
            downloaded, failed = pm.ensure_materials(catalog["cluster"], cl["kube_version"], arch, up_to, progress_cb=cb)
            res["downloaded_mb"] += downloaded / 1048576
            if failed:
                res["warnings"].append("自动下载失败: %s (网络恢复后重跑或 python prepare_cluster.py --download)" % ", ".join(failed))
        if needs_images:
            # 不接 cb: ensure_images 的事件单位是镜像个数, 与进度条的字节模型不兼容;
            # docker pull 本身无字节回调, 阶段提示即可
            prog.stage("收集离线镜像包(docker, 需数分钟)")
            cl = cfg["cluster"]
            versions = [cl["kube_version"]]
            if (cl.get("upgrade_to") or "").strip():
                versions.append(cl["upgrade_to"].strip())   # 升级目标版本的镜像同样随包携带
            for v in versions:
                img_dl, img_failed = pm.ensure_images(ccat, v, arch,
                                                      pm.k8s_image_specs(ccat, v, cl["cni_type"],
                                                                         ha_type=cl.get("ha_type") or "local",
                                                                         storage=cl.get("storage") or {}, arch=arch,
                                                                         dns=cl.get("dns") or {},
                                                                         multi_cni=cl.get("multi_cni") or "none",
                                                                         multi_cni_tag=cl.get("multi_cni_tag") or ""),
                                                      cl["cni_type"])
                res["downloaded_mb"] += img_dl   # ensure_images 返回值已是 MB
                if img_failed:
                    res["warnings"].append("离线镜像收集失败(%s): %s (需 Docker Desktop 运行中)" % (v, ", ".join(img_failed)))
        if needs_charts:
            prog.stage("下载 CNI/存储 chart")
            cl = cfg["cluster"]
            ch_dl, ch_failed = pm.ensure_charts(ccat, cl["kube_version"], cl["cni_type"], arch,
                                                storage=cl.get("storage") or {})
            res["downloaded_mb"] += ch_dl
            if ch_failed:
                res["warnings"].append("chart 下载失败: %s (检查网络或手工放置到 warehouse/cluster/charts/)"
                                       % ", ".join(ch_failed))
    except Exception as e:
        res["warnings"].append("自动下载异常: %s" % e)
    prog._downloads.clear()
    return res


def _auto_build_artifact(cfg, catalog, prog):
    """打包时自动构建 kk artifact 产物(打包机需 Docker Desktop + 外网)。
    镜像清单由 kk 按 kube_version+CNI 自动聚合; 返回 (ok, 失败日志尾部)。"""
    import shutil
    import subprocess
    arch = cfg["arch"]
    ver = cfg["cluster"]["kube_version"]
    ccat = catalog["cluster"]
    art_dir = (BASE_DIR / ccat["artifact"]["warehouse_dir"]).resolve()
    out_name = ccat["artifact"]["filename_pattern"].format(kube_version=ver, arch=arch)
    if (art_dir / out_name).is_file():
        return True, ""
    pm_path = BASE_DIR / "prepare_cluster.py"
    if not pm_path.is_file():
        return False, "缺 prepare_cluster.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("kk_prepare", pm_path)
    pm = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(pm)
    except Exception as e:
        return False, "备料脚本加载失败: %s" % e
    try:
        pm.cmd_artifact_export(ccat, ver)   # 生成导出配置(复用备料脚本, 不重复实现)
    except Exception as e:
        return False, "导出配置生成失败: %s" % e
    cfg_name = "artifact-config-%s-%s.yaml" % (ver, arch)
    kk_ver = (ccat.get("kk") or {}).get("version", "v4.0.7-xc1")
    kk_in = "/work/warehouse/cluster/kk/%s/%s/kk" % (kk_ver, arch)
    # kk 本地连接器固定走 `sudo -SE`; 容器无网络源装不了 sudo, 用垫片透传执行,
    # 并备好 /etc/sudoers 与 visudo(artifact_export 的 native/root 角色要改 sudoers)
    shim = ("printf '#!/bin/bash\\n[ \"$1\" = \"-SE\" ] && shift\\nexec \"$@\"\\n' > /usr/local/bin/sudo "
            "&& chmod +x /usr/local/bin/sudo "
            "&& touch /etc/sudoers "
            "&& printf '#!/bin/bash\\nexit 0\\n' > /usr/sbin/visudo && chmod +x /usr/sbin/visudo "
            "&& chmod +x %s" % kk_in)
    inner = ("sed -i 's|http://archive.ubuntu.com|http://mirrors.huaweicloud.com|g; "
             "s|http://security.ubuntu.com|http://mirrors.huaweicloud.com|g' /etc/apt/sources.list "
             "&& apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq ca-certificates >/dev/null 2>&1 "
             "&& %s && %s artifact export -c /work/warehouse/cluster/artifact/%s --workdir /work/kkstage"
             % (shim, kk_in, cfg_name))
    cmd = ["docker", "run", "--rm", "--name", "kk-artifact-build",
           "-v", BASE_DIR.resolve().as_posix() + ":/work", "-w", "/work",
           "ubuntu:22.04", "bash", "-c", inner]
    prog.stage("构建 kk artifact 产物(拉取全量镜像, 约 5~20 分钟; 需 Docker Desktop)")
    prog._downloads["artifact 构建"] = [0, 1]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
    except Exception as e:
        prog._downloads.clear()
        return False, "Docker 启动失败: %s" % e
    tail = []
    for line in proc.stdout:
        line = line.strip()
        if line:
            tail.append(line)
            if len(tail) > 200:
                tail.pop(0)
        prog._downloads["artifact 构建"] = [min(len(tail) + 1, 60), 60]
        if prog._cancelled:
            proc.kill()
            # docker CLI 被杀后容器仍会运行, 强制清理, 避免孤儿容器占用挂载
            subprocess.run(["docker", "rm", "-f", "kk-artifact-build"],
                           capture_output=True, timeout=30)
            prog._downloads.clear()
            shutil.rmtree(BASE_DIR / "kkstage", ignore_errors=True)
            raise PackError("已取消|Cancelled")
    proc.wait()
    prog._downloads.clear()
    # kk 固定把产物写到 {workdir}/artifact/kubekey-artifact.tgz; 搬到仓库目录并改名
    stage_tgz = BASE_DIR / "kkstage" / "artifact" / "kubekey-artifact.tgz"
    ok = proc.returncode == 0 and stage_tgz.is_file()
    if ok:
        art_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage_tgz), str(art_dir / out_name))
    shutil.rmtree(BASE_DIR / "kkstage", ignore_errors=True)
    if ok:
        return True, ""
    return False, " | ".join(tail[-3:]) if tail else "kk artifact export 退出码 %s, 未找到导出产物" % proc.returncode
