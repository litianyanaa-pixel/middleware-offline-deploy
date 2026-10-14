#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集群全生命周期回归测试(本地可跑, 不依赖 Docker/网络/真机)

补 test_offline_images.py 之外的生命周期分支:
  - gen_cluster_config: 自定义 etcd/kubelet 数据目录直写(卸载兜底的前提)
  - gen_manifest_sh:    CLUSTER_UPGRADE_TO / CLUSTER_CERTS_RENEW_CRON 导出
  - uninstall-cluster.sh: --keep-cri 分支(--set 三连) / --with-data / safe_dir 防误删 / 未知参数拒绝
  - gen_upgrade_sh:     升级脚本关键内容(kk upgrade --with-kubernetes / 镜像导入 / 铺缓存 / sandbox 改写)
  - deploy-cluster.sh:  artifact 分支(md5 校验) / 幂等指引 / deb+rpm 双分支
  - 三个 shell 脚本 bash -n 语法门禁
用法: python tests/test_cluster_lifecycle.py  (或 pytest tests/test_cluster_lifecycle.py)
"""
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))
import packer  # noqa: E402

CATALOG = packer.load_catalog()
CCAT = CATALOG["cluster"]
CHECKS = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ FAIL ") + name)


def _base_cluster(**over):
    cl = {"enabled": True, "kube_version": "v1.28.15", "mode": "cache", "zone": "",
          "cni_type": "flannel", "proxy_mode": "iptables", "ha_type": "local",
          "pod_cidr": "10.233.64.0/18", "service_cidr": "10.233.0.0/18",
          "timezone": "Asia/Shanghai", "os_distros": [],
          "roles": {"0": "control-plane"},
          "components": {}, "storage": {}, "kubelet": {}, "ntp": {}}
    cl.update(over)
    return {"project": "t-online", "arch": "amd64", "services": [], "cluster": cl,
            "servers": [{"name": "m1", "user": "root", "ip": "10.0.0.1", "ssh": 22}],
            "ports": {}}


def _vcfg(**over):
    """validate 后的配置 — 模拟真实链路(WebUI 输入 -> validate_config -> gen_*):
    gen_cluster_config 等生成器消费的是 validate 规整后的值(etcd_env 强转 int/
    dns 补默认键/calico_values 归位), 直接喂原始 dict 会与生产行为不符(假阴性教训)"""
    out, _ = packer.validate_config(_base_cluster(**over), CATALOG)
    return out


def test_online_cn_static_fallback():
    """online+zone=cn 必须自动回退非 static containerd(qingstor 镜像无 static-*, 404 实测),
    offline/cache 与国际源保留 static"""
    cfg = _base_cluster(mode="online", zone="cn", components={"static_binary": True})
    out, _ = packer.validate_config(cfg, CATALOG)
    check("online+cn 回退非 static", out["cluster"]["components"]["static_binary"] is False)
    cfg2 = _base_cluster(mode="cache", components={"static_binary": True})
    out2, _ = packer.validate_config(cfg2, CATALOG)
    check("cache 模式保留 static", out2["cluster"]["components"]["static_binary"] is True)
    cfg3 = _base_cluster(mode="online", zone="intl", components={"static_binary": True})
    out3, _ = packer.validate_config(cfg3, CATALOG)
    check("online 国际源保留 static", out3["cluster"]["components"]["static_binary"] is True)

    y = packer.gen_cluster_config(_base_cluster(mode="online", zone="cn"), CATALOG)
    check("online+cn 不写裸域名 imageRepository", "imageRepository" not in y)
    check("online+cn 不覆盖 sandbox(交 kk 内置 CN 映射)", "sandbox_image:" not in y)
    y2 = packer.gen_cluster_config(_base_cluster(mode="cache"), CATALOG)
    check("offline 模式保留 sandbox 覆盖", 'registry: "registry.k8s.io"' in y2)


def test_config_custom_dirs():
    """自定义 etcd/kubelet 目录必须直写进 config.yaml, uninstall 兜底依赖这两个值"""
    cfg = _base_cluster(components={"etcd_dir": "/data1/etcd", "containerd_root": "/data1/containerd"},
                        kubelet={"root_dir": "/data1/kubelet"})
    y = packer.gen_cluster_config(cfg, CATALOG)
    check("etcd 自定义目录写入 env.data_dir", '  etcd:' in y and '    env:' in y and '      data_dir: "/data1/etcd"' in y)
    check("kubelet 自定义目录写入 root-dir", '"root-dir": "/data1/kubelet"' in y)
    check("containerd data_root 直写", 'data_root: "/data1/containerd"' in y)
    # 目录为空时不产生空值行(kk 会用默认路径)
    y2 = packer.gen_cluster_config(_base_cluster(), CATALOG)
    check("未配置时不残留 data_dir/root-dir", "data_dir:" not in y2 and "root-dir:" not in y2)


def test_config_etcd_env_and_advance():
    """etcd 数据目录/高级参数必须挂在 etcd.env 下(v4 只读 .etcd.env.*, 顶层 data_dir 是旧版键会被忽略)"""
    cfg = _base_cluster(components={"etcd_dir": "/data1/etcd",
                                    "etcd_env": ["heartbeat_interval=250", "election_timeout=5000",
                                                 "compaction_retention=8", "snapshot_count=10000",
                                                 "quota_backend_bytes=8589934592",
                                                 "max_request_bytes=1572864", "max_snapshots=5",
                                                 "max_wals=5", "log_level=info"]})
    out, warns = packer.validate_config(cfg, CATALOG)
    y = packer.gen_cluster_config(out, CATALOG)
    check("env 块包含 data_dir", "    env:\n      data_dir: \"/data1/etcd\"" in y)
    for k, v in (("heartbeat_interval", 250), ("election_timeout", 5000), ("compaction_retention", 8),
                 ("snapshot_count", 10000), ("quota_backend_bytes", 8589934592),
                 ("max_request_bytes", 1572864), ("max_snapshots", 5), ("max_wals", 5)):
        check("env.%s=%s" % (k, v), ("      %s: %d" % (k, v)) in y)
    check("env.log_level", '      log_level: "info"' in y)
    check("顶层无 etcd.data_dir 残留", "\n    data_dir:" not in y)
    # SSH 免密提示属正常告警(测试节点未填密码); 除它之外必须零告警
    check("validate 除 SSH 免密提示外无告警", not [w for w in warns if "SSH" not in w])


def test_registry_tar_path_resolution():
    """回归: ensure_registry_image 曾把 arch 误传给 kube_ver 参数位(查不到版本 -> 静默零下载),
    registry tar 永远不会被自动收集。此处锁死路径解析与失败上报两条链路"""
    prep = packer._prepare_module()
    # 路径解析: 版本来自对应 kube 版本 components.docker_registry_image(2.8.3)
    out, ver = prep.registry_image_tar_path(CCAT, "v1.28.15", "amd64")
    check("registry tar 路径带版本号", out is not None and ver == "2.8.3"
          and "docker-registry-image/2.8.3/amd64" in out.as_posix())
    out_none, _ = prep.registry_image_tar_path(CCAT, "amd64", "amd64")   # 旧 bug 的传参形态
    check("kube_ver 传错(arch)时返回 None 而非静默成功", out_none is None)
    # 失败上报: docker 拉取全失败时必须返回 failed, 不允许 0.0/[] 假成功。
    # 三个 patch: registry tar 路径指向临时空目录(消除本机 warehouse 已有 tar 的
    # 环境依赖) + _docker 全失败 + 引擎探测恒就绪(否则会真去拉起 Docker Desktop)
    import tempfile
    orig_docker = prep._docker
    orig_path = prep.registry_image_tar_path
    orig_engine = prep.ensure_docker_running
    tmpd = tempfile.mkdtemp(prefix="kk-reg-test-")
    prep._docker = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "simulated pull failure")
    prep.registry_image_tar_path = lambda ccat, kv="", arch="amd64": (
        Path(tmpd) / "docker-registry-2.8.3-linux-amd64.tgz", "2.8.3")
    prep.ensure_docker_running = lambda *a, **k: True
    try:
        dl, failed = prep.ensure_registry_image(CCAT, "v1.28.15", "amd64")
        check("registry 拉取失败上报 failed", dl == 0.0 and failed)
    finally:
        prep._docker = orig_docker
        prep.registry_image_tar_path = orig_path
        prep.ensure_docker_running = orig_engine


def test_prepare_download_events():
    """锁死备料下载层进度协议: plan/skip 事件、ensure_charts 逐项缓存判定(曾全有全无)、
    fetch_simple 字节 tick、ensure_service_images 三态(缓存跳过/拉取成功/save 失败清理残缺 tar)"""
    import tarfile
    import types
    prep = packer._prepare_module()
    tmp = Path(tempfile.mkdtemp(prefix="kk-dl-test-"))
    (orig_base, orig_fetch, orig_clen, orig_docker, orig_engine,
     orig_shutil, orig_curl) = (prep.BASE_DIR, prep.fetch_simple, prep.content_length,
                                prep._docker, prep.ensure_docker_running, prep.shutil, prep.has_curl)
    try:
        prep.BASE_DIR = tmp
        prep.content_length = lambda u: 4096
        prep.ensure_docker_running = lambda *a, **k: True
        prep.has_curl = lambda: False
        prep.shutil = types.SimpleNamespace(which=lambda n: "/usr/bin/docker")
        # ---- fetch_simple: urllib 分支逐 MiB 字节 tick ----
        big = tmp / "src.bin"
        big.write_bytes(b"z" * 2621440)   # 2.5MiB -> 3 个 tick
        ticks = []
        dst = tmp / "dst.bin"
        ok = prep.fetch_simple(big.as_uri(), dst, tick=ticks.append)
        check("fetch_simple 下载成功", ok and dst.stat().st_size == 2621440)
        check("tick 字节递增且终值=文件大小", ticks == sorted(ticks) and len(ticks) >= 3
              and ticks[-1] == 2621440)
        check("原子落盘: 成功后无 .tmp 残留", not (tmp / "dst.bin.tmp").exists())

        def _boom():
            raise RuntimeError("cancelled")

        try:
            prep.fetch_simple(big.as_uri(), tmp / "dst2.bin", abort_check=_boom)
            check("fetch_simple 取消异常向上传播", False)
        except RuntimeError:
            check("fetch_simple 取消异常向上传播", True)
        check("取消后无半截正式/临时文件", not (tmp / "dst2.bin").exists()
              and not (tmp / "dst2.bin.tmp").exists())
        # ---- ensure_materials: abort_check 异常穿透 ----
        try:
            prep.ensure_materials({"versions": {"v1.28.15": {}}}, "v1.28.15", "amd64", "",
                                  abort_check=_boom)
            check("ensure_materials 取消异常向上传播", False)
        except RuntimeError:
            check("ensure_materials 取消异常向上传播", True)
        # ---- ensure_charts: 混合缓存逐项 skip(修复前只在全缓存时发 skip) ----
        ccat = {"versions": {"v1.28.15": {"cni_versions": {"flannel": "v0.24.0"}}}}
        fl_path = tmp / "warehouse/cluster/charts/flannel/flannel-v0.24.0.tgz"
        fl_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fl_path, "w:gz") as tf:
            tf.addfile(tarfile.TarInfo("x"))
        events = []

        def fake_fetch(url, out, timeout=1800, tick=None, abort_check=None):
            out.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(out, "w:gz") as tf:
                tf.addfile(tarfile.TarInfo("y"))
            if tick:
                tick(4096)
            return True

        prep.fetch_simple = fake_fetch
        dl_mb, failed = prep.ensure_charts(ccat, "v1.28.15", "flannel", "amd64",
                                           storage={"localpv_enabled": True},
                                           progress_cb=lambda *e: events.append(e))
        check("charts 命中项逐项发 skip", ("skip", "flannel chart", 1, 1) in events)
        check("charts plan 事件(命中1/共2)", ("plan", "charts", 1, 2) in events)
        check("charts 缺失项排队发 pending", ("pending", "localpv chart", 1, 1) in events)
        check("charts 缺失项下载成功且 dl/downloaded 齐备",
              dl_mb > 0 and not failed and any(e[0] == "dl" for e in events)
              and ("downloaded", "localpv chart", 1, 1) in events)
        # ---- ensure_service_images: 缓存跳过 + 拉取成功(候选源+retag) ----
        nginx = tmp / "images/nginx.tar"
        redis = tmp / "images/redis.tar"
        nginx.parent.mkdir(parents=True, exist_ok=True)
        nginx.write_bytes(b"n" * 1_500_000)   # 已缓存(>1MB)
        items = [("docker.io/library/nginx:1.25", nginx), ("docker.io/library/redis:7", redis)]
        pulls = []

        def docker_ok(*a, **k):
            if a[0] == "pull":
                pulls.append(a[1])
            elif a[0] == "save":
                Path(a[2]).write_bytes(b"r" * 2_000_000)
            return subprocess.CompletedProcess(a, 0, "", "")

        prep._docker = docker_ok
        events2 = []
        saved_mb, failed2 = prep.ensure_service_images(items, progress_cb=lambda *e: events2.append(e))
        check("service 缓存项跳过", ("skip", "docker.io/library/nginx:1.25", 1, 1) in events2)
        check("service plan(命中1/共2)", ("plan", "services", 1, 2) in events2)
        check("service 拉取成功: tar 落盘且无 failed",
              saved_mb > 0 and failed2 == [] and redis.stat().st_size > 1_000_000)
        check("service 拉取走镜像源候选(首个命中, 不再兜底官方源)",
              len(pulls) == 1 and pulls[0] != "docker.io/library/redis:7")
        check("service 缺失项排队发 pending", ("pending", "docker.io/library/redis:7", 1, 1) in events2)
        check("service dli/downloaded/dlc 事件齐备", any(e[0] == "dli" for e in events2)
              and ("downloaded", "docker.io/library/redis:7", 1, 1) in events2
              and ("dlc", "收集中间件镜像", 1, 1) in events2)
        # ---- ensure_service_images: save 失败 -> 上报 failed + 清理残缺 tar ----
        redis.unlink()

        def docker_save_fail(*a, **k):
            if a[0] == "save":
                Path(a[2]).write_bytes(b"broken")   # 模拟残缺产物
                return subprocess.CompletedProcess(a, 1, "", "simulated save failure")
            return subprocess.CompletedProcess(a, 0, "", "")

        prep._docker = docker_save_fail
        events3 = []
        saved3, failed3 = prep.ensure_service_images(items, progress_cb=lambda *e: events3.append(e))
        check("save 失败上报 failed", saved3 == 0.0 and failed3 == ["docker.io/library/redis:7"])
        check("save 失败清理残缺 tar(防下轮 size>1MB 误判有效)", not redis.exists())
        check("save 失败也弹出在途不确定行", ("done", "docker.io/library/redis:7", 1, 1) in events3)
    finally:
        (prep.BASE_DIR, prep.fetch_simple, prep.content_length, prep._docker,
         prep.ensure_docker_running, prep.shutil, prep.has_curl) = (
            orig_base, orig_fetch, orig_clen, orig_docker, orig_engine, orig_shutil, orig_curl)


def test_auto_download_wiring():
    """回归: _auto_download_cluster_materials 曾引用未定义的 ccat(NameError 被外层 except
    吞成一条 warning), 缺镜像/registry tar/chart 时自动补料入口整体静默不可达。
    锁死接线: 缺什么就必须真的调起对应收集函数, 不允许出现被吞异常的告警形态;
    同时锁死进度事件映射(plan/skip -> 已缓存 N/M, dl/dlc/dli -> 三种单位的子进度行)"""
    import types
    import packerlib.cluster as cl_mod
    from packerlib.progress import PackProgress
    real_prep = cl_mod._PREPARE if cl_mod._PREPARE is not None else packer._prepare_module()
    calls = []
    abort_ok = []   # 各收集器是否收到 abort_check(取消钩子接线)
    prog = PackProgress()
    snaps = []   # 收集过程中的中间快照(断言子进度行单位/生命周期)
    fake = types.ModuleType("kk_prepare_fake")
    for attr in dir(real_prep):          # 浅拷贝真实模块公开属性(纯函数保持真实实现)
        if not attr.startswith("_"):
            try:
                setattr(fake, attr, getattr(real_prep, attr))
            except Exception:
                pass

    def fake_materials(ccat, kv, arch, up_to="", progress_cb=None, only_missing=True, extra_comps=(),
                       abort_check=None):
        calls.append("materials")
        abort_ok.append(abort_check is not None)
        if progress_cb:
            progress_cb("plan", "materials", 2, 5)
            progress_cb("skip", "etcd/v3.5.6", 1, 1)
            progress_cb("skip", "helm/v3.12.3", 1, 1)
            progress_cb("pending", "cni-plugins", 1, 1)
            progress_cb("src", "cni-plugins 源 1/1: mirrors.huaweicloud.com", 1, 1)
            progress_cb("start", "download", 0, 1000)
            progress_cb("dl", "cni-plugins", 400, 1000)
            snaps.append(prog.snapshot())
            progress_cb("downloaded", "cni-plugins", 1, 1)
        return 0.0, []

    def fake_images(ccat, v, arch, specs, cni, progress_cb=None, abort_check=None):
        calls.append(("images", v, len(specs)))
        abort_ok.append(abort_check is not None)
        if progress_cb:
            progress_cb("plan", "images", 0, 1)
            progress_cb("pending", "k8s-v1.28.15-images.tar", 1, 1)
            progress_cb("startc", "images", 0, 2)
            progress_cb("dli", "registry.k8s.io/pause:3.9", 0, 1)
            snaps.append(prog.snapshot())
            progress_cb("done", "registry.k8s.io/pause:3.9", 1, 1)
            progress_cb("dlc", "拉取镜像", 1, 2)
            snaps.append(prog.snapshot())
            progress_cb("downloaded", "k8s-v1.28.15-images.tar", 1, 1)
        return 0.0, []

    def fake_registry(*a, **k):
        calls.append("registry")
        abort_ok.append(k.get("abort_check") is not None)
        cb = k.get("progress_cb")
        if cb:
            cb("plan", "registry", 0, 1)
            cb("pending", "docker-registry-2.8.3.tgz", 1, 1)
            cb("dli", "docker-registry-2.8.3.tgz", 0, 1)
            cb("downloaded", "docker-registry-2.8.3.tgz", 1, 1)
        return 0.0, []

    def fake_charts(*a, **k):
        calls.append("charts")
        abort_ok.append(k.get("abort_check") is not None)
        cb = k.get("progress_cb")
        if cb:
            cb("skip", "flannel chart", 1, 1)
            cb("plan", "charts", 1, 2)
        return 0.0, []

    fake.ensure_materials = fake_materials
    fake.ensure_images = fake_images
    fake.ensure_registry_image = fake_registry
    fake.ensure_charts = fake_charts

    cfg = _base_cluster()
    cl_missing = ["缺 k8s 离线镜像包(k8s-1.28.15-flannel-amd64-images.tar)",
                  "缺私有仓库 registry 镜像 tar",
                  "缺 CNI chart 物料(tigera-operator)",
                  "缺 kubeadm 二进制缓存"]
    orig = cl_mod._PREPARE
    cl_mod._PREPARE = fake
    try:
        res = cl_mod._auto_download_cluster_materials(cfg, CATALOG, cl_missing, prog)
    finally:
        cl_mod._PREPARE = orig
    check("缺缓存目录调起 ensure_materials", "materials" in calls)
    img_calls = [c for c in calls if isinstance(c, tuple)]
    check("缺镜像包调起 ensure_images", len(img_calls) == 1)
    check("ensure_images 收到 kube 版本与非空镜像清单", img_calls and img_calls[0][1] == "v1.28.15" and img_calls[0][2] > 0)
    check("缺 registry tar 调起 ensure_registry_image", "registry" in calls)
    check("缺 chart 调起 ensure_charts", "charts" in calls)
    check("无被吞异常告警(修复前必现 '自动下载异常: name ccat is not defined')",
          not any("自动下载异常" in w for w in res["warnings"]))
    check("kk/artifact 缺失提示仍在", any("kk 二进制" in w for w in res["warnings"]) is False
          and all("kk" not in w for w in res["warnings"]))
    check("取消钩子接线(abort_check 传入全部 4 个收集器)", len(abort_ok) == 4 and all(abort_ok))
    # ---- 进度事件映射 ----
    snap = prog.snapshot()
    check("plan 总数跨收集阶段累加(5+1+1+2)", snap["cached"]["total"] == 9)
    check("skip 名单去重登记(etcd/helm/flannel chart)",
          snap["cached"]["n"] == 3 and set(snap["cached"]["names"]) == {"etcd/v3.5.6", "helm/v3.12.3", "flannel chart"})
    d0 = {d["name"]: d for d in snaps[0]["downloads"]}
    check("dl 事件 -> 字节单位子进度行", d0.get("cni-plugins", {}).get("unit") == "bytes"
          and d0["cni-plugins"]["done"] == 400 and d0["cni-plugins"]["total"] == 1000)
    check("排队物料在下载行出现后离队", snaps[0]["pending"] == [])
    d1 = {d["name"]: d for d in snaps[1]["downloads"]}
    check("dli 事件 -> 不确定进度单位(docker pull 无字节回调)",
          d1.get("registry.k8s.io/pause:3.9", {}).get("unit") == "indeterminate")
    check("镜像包下载期间保持待下载标记(物料级待完成)",
          snaps[1]["pending"] == ["k8s-v1.28.15-images.tar"])
    d2 = {d["name"]: d for d in snaps[2]["downloads"]}
    check("dlc 事件 -> 个数单位子进度行", d2.get("拉取镜像", {}).get("unit") == "count"
          and d2["拉取镜像"]["done"] == 1 and d2["拉取镜像"]["total"] == 2)
    check("done 事件弹出在途子进度行", "registry.k8s.io/pause:3.9" not in d2)
    check("收集结束子进度行清空(downloads_reset)", snap["downloads"] == [])
    check("缓存命中统计保留到收集结束之后(显示至打包完成)",
          snap["cached"]["n"] == 3 and snap["cached"]["total"] == 9)
    check("已下载登记(cni + k8s 镜像包 + registry tar)",
          snap["downloaded"]["n"] == 3
          and set(snap["downloaded"]["names"]) == {"cni-plugins", "k8s-v1.28.15-images.tar",
                                                   "docker-registry-2.8.3.tgz"})
    check("收集结束待下载清空(残留即失败项, 由 warnings 上报)", snap["pending"] == [])


def test_auto_download_cancel_passthrough():
    """「打包已取消」必须穿透自动补料直达打包线程, 不得被 except Exception 吞成 warning"""
    import types
    import packerlib.cluster as cl_mod
    from packerlib.progress import PackProgress
    from packerlib.util import PackError
    fake = types.ModuleType("kk_prepare_fake")

    def boom(*a, **k):
        raise PackError("打包已取消")

    fake.ensure_materials = boom
    fake.ensure_images = boom
    fake.ensure_registry_image = boom
    fake.ensure_charts = boom
    orig = cl_mod._PREPARE
    cl_mod._PREPARE = fake
    raised = False
    try:
        cl_mod._auto_download_cluster_materials(_base_cluster(), CATALOG,
                                                ["缺 kubeadm 二进制缓存"], PackProgress())
    except PackError:
        raised = True
    finally:
        cl_mod._PREPARE = orig
    check("PackError(打包已取消)穿透不被吞成 warning", raised)


def test_config_ha_vip_and_backup():
    """kube-vip 必须写 kube_vip.address(网卡自动发现依据); kubeadm 备份目录/证书续期显式落 config"""
    y = packer.gen_cluster_config(_base_cluster(ha_type="kube-vip", ha_vip="10.0.0.100"), CATALOG)
    check("kube-vip 写 host", '      host: "10.0.0.100"' in y)
    check("kube-vip 写 kube_vip.address", "      kube_vip:\n        address: \"10.0.0.100\"" in y)
    y2 = packer.gen_cluster_config(_base_cluster(ha_type="haproxy", ha_vip="127.0.0.2"), CATALOG)
    check("haproxy 写 host 不写 kube_vip", 'host: "127.0.0.2"' in y2 and "kube_vip:" not in y2)
    y3 = packer.gen_cluster_config(_base_cluster(), CATALOG)
    check("默认 backup 目录(/etc/kubekey/backup/kubernetes)", 'kubeadm_config_dir: "/etc/kubekey/backup/kubernetes"' in y3)
    y4 = packer.gen_cluster_config(_base_cluster(kubeadm_config_dir="/data1/kk-backup"), CATALOG)
    check("自定义备份目录", 'kubeadm_config_dir: "/data1/kk-backup"' in y4)
    y5 = packer.gen_cluster_config(_base_cluster(kubeadm_config_dir="", certs_renew=False), CATALOG)
    check('空串禁用备份 + certs.renew: false', 'kubeadm_config_dir: ""' in y5 and "renew: false" in y5)


def test_config_cri_cni_dns():
    """container_manager 选择 / CNI 高级(掩码+multus+calico values) / DNS 覆盖必须落到对应键"""
    y = packer.gen_cluster_config(_vcfg(container_manager="docker"), CATALOG)
    check("docker 运行时写 container_manager", 'container_manager: "docker"' in y)
    check("docker 不写 containerd 子块", "    containerd:" not in y)
    check("离线 docker 写 daemon_policy: override(kk 重写 daemon.json 带加速器)", "      daemon_policy: override" in y)
    y_d2 = packer.gen_cluster_config(_vcfg(container_manager="docker", components={"docker_root": "/data1/docker"}), CATALOG)
    check("docker 数据目录直写 data_root", 'data_root: "/data1/docker"' in y_d2)
    y2 = packer.gen_cluster_config(_vcfg(cni_type="calico", ipv4_mask_size=25, multi_cni="multus", multi_cni_tag="v4.3.0",
                                         calico_values="installation:\n  calicoNetwork:\n    mtu: 1440",
                                         dns={"coredns_tag": "v1.12.1", "nodelocaldns_enabled": True,
                                              "nodelocaldns_tag": "1.26.4"}), CATALOG)
    check("ipv4_mask_size 直写", "    ipv4_mask_size: 25" in y2)
    check("multi_cni: multus + multus tag", "    multi_cni: multus" in y2 and 'tag: "v4.3.0"' in y2)
    check("calico values 块透传(helm -f)", "    calico:\n      values: |\n        installation:" in y2)
    check("coredns/nodelocaldns tag 覆盖", 'tag: "v1.12.1"' in y2 and 'tag: "1.26.4"' in y2)
    y3 = packer.gen_cluster_config(_vcfg(dns={"nodelocaldns_enabled": False}), CATALOG)
    check("nodelocaldns 显式关闭", "    nodelocaldns:\n      enabled: false" in y3)
    check("关闭时不写 nodelocaldns tag", "k8s-dns-node-cache" not in y3)
    # 离线镜像清单跟随 DNS 覆盖/multus(导入镜像必须与 config 生成的 tag 一致);
    # dns 需带 enabled 键(validate 后的形态, 生产链路 k8s_image_specs 只见规整值)
    prep = packer._prepare_module()
    specs = prep.k8s_image_specs(CCAT, "v1.28.15", "flannel",
                                 dns={"coredns_tag": "v1.12.1", "nodelocaldns_enabled": True,
                                      "nodelocaldns_tag": "1.26.4"},
                                 multi_cni="multus", multi_cni_tag="v4.4.0")
    check("镜像清单用覆盖后的 coredns tag", "registry.k8s.io/coredns/coredns:v1.12.1" in specs)
    check("镜像清单用覆盖后的 nodelocaldns tag", any("k8s-dns-node-cache:1.26.4" in s for s in specs))
    check("multus 镜像入清单", "ghcr.io/k8snetworkplumbingwg/multus-cni:v4.4.0" in specs)
    specs_off = prep.k8s_image_specs(CCAT, "v1.28.15", "flannel", dns={"nodelocaldns_enabled": False})
    check("nodelocaldns 关闭不出镜像", not any("k8s-dns-node-cache" in s for s in specs_off))


def test_config_validation_matrix():
    """新键非法值必须前置拦截(免得到部署时才被 kk 拦下)"""
    def expect_err(name, over):
        try:
            packer.validate_config(_base_cluster(**over), CATALOG)
            check(name, False)
        except packer.PackError:
            check(name, True)
    # 离线 docker 已支持(静态二进制随 kk 缓存携带); artifact 模式告警建议 cache
    out_d, w_d = packer.validate_config(_base_cluster(container_manager="docker"), CATALOG)
    check("cache+docker 放行", out_d["cluster"]["container_manager"] == "docker")
    out_da, w_da = packer.validate_config(_base_cluster(mode="artifact", container_manager="docker"), CATALOG)
    check("artifact+docker 告警建议 cache", any("artifact" in w for w in w_da))
    expect_err("运行时非法值", {"container_manager": "podman"})
    expect_err("etcd 未知键", {"components": {"etcd_env": ["foo=1"]}})
    expect_err("etcd 非整数", {"components": {"etcd_env": ["heartbeat_interval=abc"]}})
    expect_err("mask 越界", {"ipv4_mask_size": 40})
    expect_err("multi_cni 非法", {"multi_cni": "spiderpool"})
    # YAML 校验仅对当前 CNI 的 values 生效(flannel 下 calico_values 提前短路忽略)
    expect_err("calico values 非 YAML", {"cni_type": "calico", "calico_values": "{{{{"})
    expect_err("备份目录相对路径", {"kubeadm_config_dir": "rel/path"})
    # 在线模式 docker 放行
    out, _ = packer.validate_config(_base_cluster(mode="online", container_manager="docker"), CATALOG)
    check("online+docker 放行", out["cluster"]["container_manager"] == "docker")
    # calico values 仅 calico 生效(其他 CNI 静默忽略+告警); SSH 免密提示为常态告警需排除
    out2, w2 = packer.validate_config(_base_cluster(cni_type="flannel", calico_values="a: 1"), CATALOG)
    check("非 calico 忽略 values 并告警", out2["cluster"]["calico_values"] == ""
          and len([w for w in w2 if "SSH" not in w]) == 1)


def test_registry_auth_and_data_dir():
    """私有仓库账号密码写 image_registry.auth(kk 下发到各节点 CRI 配置);
    数据目录写 docker_registry.storage.filesystem.rootdir; 成对/路径校验"""
    cfg = _base_cluster(roles={"0": "control-plane,registry"},
                        image_registry={"type": "docker-registry", "username": "reguser",
                                        "password": "RegPass123", "data_dir": "/data1/registry"})
    out, _ = packer.validate_config(cfg, CATALOG)
    ir = out["cluster"]["image_registry"]
    check("账号密码透传", ir["username"] == "reguser" and ir["password"] == "RegPass123")
    check("数据目录透传", ir["data_dir"] == "/data1/registry")
    y = packer.gen_cluster_config(out, CATALOG)
    check("auth 块写入 config", '    auth:\n      username: "reguser"\n      password: "RegPass123"' in y)
    check("rootdir 写入 config", '        filesystem:\n          rootdir: "/data1/registry"' in y)
    # 都留空: 不写 auth 块(kk 用默认 admin/Harbor12345)
    y2 = packer.gen_cluster_config(_base_cluster(roles={"0": "control-plane,registry"}, image_registry={"type": "docker-registry"}), CATALOG)
    check("留空不写 auth 块", "    auth:" not in y2 and "rootdir" not in y2)

    def expect_err(name, over):
        try:
            packer.validate_config(_base_cluster(**over), CATALOG)
            check(name, False)
        except packer.PackError:
            check(name, True)
    expect_err("只填账号拒绝", {"roles": {"0": "control-plane,registry"}, "image_registry": {"type": "docker-registry", "username": "u"}})
    expect_err("只填密码拒绝", {"roles": {"0": "control-plane,registry"}, "image_registry": {"type": "docker-registry", "password": "p"}})
    expect_err("数据目录相对路径拒绝", {"roles": {"0": "control-plane,registry"}, "image_registry": {"type": "docker-registry", "data_dir": "rel/dir"}})
    # harbor + 数据目录: 告警忽略(不写入 config)
    out3, w3 = packer.validate_config(_base_cluster(roles={"0": "control-plane,registry"}, image_registry={"type": "harbor", "data_dir": "/data1/registry"}), CATALOG)
    y3 = packer.gen_cluster_config(out3, CATALOG)
    check("harbor 数据目录告警且不落 config", any("数据目录" in w for w in w3) and "rootdir" not in y3)


def test_manifest_lifecycle_vars():
    """升级目标版本与证书续期 cron 必须导出到 manifest.sh(deploy/upgrade/uninstall 三脚本都读)"""
    cfg = _base_cluster(upgrade_to="v1.29.15", certs_renew_cron="0 0 1 * *",
                        nodes=[{"name": "m", "ip": "1.2.3.4", "role": "control-plane"},
                               {"name": "w", "ip": "5.6.7.8", "role": "worker"}])
    cfg["deploy_dir"] = "/data/middleware"
    cfg["docker_data_root"] = "/data/docker"
    cfg["ports"] = {}
    cfg["secrets"] = {}
    cfg["db"] = {}
    cfg["servers"] = []
    cfg["registry_mirrors"] = []
    m = packer.gen_manifest_sh(cfg, CATALOG, [], [])
    check("manifest 导出 CLUSTER_UPGRADE_TO", "CLUSTER_UPGRADE_TO='v1.29.15'" in m)
    check("manifest 导出 CLUSTER_CERTS_RENEW_CRON", "CLUSTER_CERTS_RENEW_CRON='0 0 1 * *'" in m)
    # 未配置时导出空串(脚本端 ${VAR:-} 判空, 不能缺变量)
    cfg2 = _base_cluster(nodes=[{"name": "m", "ip": "1.2.3.4", "role": "control-plane"}])
    cfg2.update({"deploy_dir": "/data/m", "docker_data_root": "/data/docker",
                 "ports": {}, "secrets": {}, "db": {}, "servers": [],
                 "registry_mirrors": []})
    m2 = packer.gen_manifest_sh(cfg2, CATALOG, [], [])
    check("未配置时导出空 CLUSTER_UPGRADE_TO", "CLUSTER_UPGRADE_TO=''" in m2)
    check("未配置时导出空 CLUSTER_CERTS_RENEW_CRON", "CLUSTER_CERTS_RENEW_CRON=''" in m2)
    # docker CRI / 私有仓库数据目录变量(deploy-cluster.sh 导入与数据目录绑定分支读)
    check("manifest 导出 CLUSTER_CRI", "CLUSTER_CRI='containerd'" in m2)
    check("manifest 导出 CLUSTER_DOCKER_DATA_ROOT", "CLUSTER_DOCKER_DATA_ROOT=''" in m2)
    check("manifest 导出 CLUSTER_REGISTRY_DATA_DIR", "CLUSTER_REGISTRY_DATA_DIR=''" in m2)


def test_uninstall_flag_matrix():
    u = (BASE / "server" / "uninstall-cluster.sh").read_text(encoding="utf-8")
    check("--keep-cri 打开 dns/etcd/registry 三删开关",
          "--set delete.dns=true --set delete.etcd=true --set delete.image_registry=true" in u)
    check("--with-data 透传 kk", 'KK_ARGS+=(--with-data)' in u)
    check("safe_dir 防误删(拒绝相对路径/根/..)", "safe_dir()" in u and '*..*' in u)
    check("未知参数拒绝", "未知参数" in u)
    check("自定义 kubelet 目录走 CLUSTER_KUBELET_ROOT_DIR", "CLUSTER_KUBELET_ROOT_DIR" in u)
    check("自定义 etcd 目录走 CLUSTER_ETCD_DATA_DIR", "CLUSTER_ETCD_DATA_DIR" in u)
    check("worker 不清 etcd 自定义目录", '$_role' in u and '"$2"' in u)


def test_upgrade_sh_content():
    up_to = "v1.29.15"
    cfg = _base_cluster(upgrade_to=up_to)
    up = packer.gen_upgrade_sh(cfg)
    check("升级走 kk upgrade cluster --with-kubernetes", "kk upgrade cluster" in up and "--with-kubernetes %s" % up_to in up)
    check("导入目标版本离线镜像(cluster/upgrade)", "cluster/upgrade/images-*.tar" in up)
    check("目标版本三件套铺到 kk 缓存", "kube/%s/amd64" % up_to in up)
    tag = ((CCAT["versions"].get(up_to) or {}).get("sandbox_image_tag") or "")
    check("sandbox 镜像随目标版本改写", tag and ("pause:%s" % tag) in up)
    check("升级后 kubectl get nodes 验证", "kubectl get nodes" in up)
    check("升级脚本带 --all(连带组件)", "--all" in up)
    # 回归: _script 变量 + bash -c 嵌套引号在 sed \\" 转义上两层都炸(真机升级首测发现),
    # 必须走临时文件 bash -s 下发(与 deploy-cluster.sh 同套路); 单引号块能骗过整文件 bash -n,
    # 所以要把内嵌脚本体抠出来单独做语法门禁
    check("升级脚本无 bash -c 嵌套引号块", "_script=" not in up)
    check("远端导入走 bash -s 临时文件", "RUN_SSH \"$1\" 'bash -s' < \"$IMP_SH\"" in up)
    check("crictl 铺缓存连目录层(不带 /.)", 'cp -a cluster/upgrade/cache/. "$KK_CACHE/"' in up)
    bash = _find_working_bash()
    if bash and 'IMP_SH="$(mktemp' in up:
        # 回归: 节点侧脚本体经 heredoc 反转义后才合法, 必须在同一 shell 层落盘后再做语法门禁
        # (真机升级首测炸点: _script 变量 + bash -c 的 sed \\" 转义, 整文件 bash -n 拦不住)
        import os
        seg0 = up.find('IMP_SH="$(mktemp')
        seg1 = up.find('\nUP_EOF', seg0) + len('\nUP_EOF')
        r = subprocess.run([bash, "-c", up[seg0:seg1] + '\ncat "$IMP_SH"'],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            fd, tp = tempfile.mkstemp(suffix=".sh")
            try:
                with os.fdopen(fd, "w", newline="\n") as f:
                    f.write(r.stdout)
                r2 = subprocess.run([bash, "-n", tp], capture_output=True, text=True)
                check("导入脚本体(heredoc 展开后) bash -n 通过", r2.returncode == 0)
            finally:
                os.unlink(tp)
        else:
            check("导入脚本 heredoc 落盘失败", False)


def test_deploy_sh_branches():
    d = (BASE / "server" / "deploy-cluster.sh").read_text(encoding="utf-8")
    check("artifact 分支校验 md5 后传给 kk", "download.artifact_file" in d and "artifact.md5" in d)
    check("artifact md5 不一致即终止", "artifact md5 不一致" in d)
    check("deb 分支(dpkg)", "dpkg -i" in d)
    check("rpm 分支(localinstall)", "localinstall" in d)
    check("幂等指引: 扩容", "kk add nodes" in d)
    check("幂等指引: 升级", "kk upgrade cluster" in d)
    check("幂等指引: 卸载", "./uninstall-cluster.sh" in d)
    check("cache sha256 校验门禁", "cache.sha256" in d)
    check("kubelet 残留 unit 守卫(失败重部署卡死)", "清理残留 kubelet unit" in d)
    # docker CRI 离线分支
    check("CLUSTER_CRI 分支判断", 'CRI="${CLUSTER_CRI:-containerd}"' in d)
    check("docker CRI 预装静态二进制", "预装 docker (来自离线包静态二进制)" in d)
    check("docker CRI 镜像走 docker load", "docker load -i" in d)
    check("docker daemon.json 数据目录一致", "data-root" in d)
    # 私有仓库数据目录绑定(修复 kk compose 不挂数据卷)
    check("私有仓库数据目录绑定步骤", "私有仓库数据目录绑定" in d)
    check("绑定走 docker-compose.yml 补卷+重启", "docker-compose.yml" in d and "systemctl restart docker-registry" in d)


def _find_working_bash():
    """Windows 下 System32\\bash.exe(WSL) 会遮蔽 Git Bash: 逐候选做 bash -n 冒烟, 首个可用者胜"""
    import os
    import shutil
    import tempfile
    cands = []
    git = shutil.which("git")
    if git:
        cands.append(str(Path(git).resolve().parents[1] / "bin" / "bash.exe"))
    cands.append(r"C:\Program Files\Git\bin\bash.exe")
    w = shutil.which("bash")
    if w:
        cands.append(w)
    for c in cands:
        if not Path(c).is_file():
            continue
        fd, tp = tempfile.mkstemp(suffix=".sh")
        try:
            with os.fdopen(fd, "w", newline="\n") as f:
                f.write("exit 0\n")
            if subprocess.run([c, "-n", tp], capture_output=True).returncode == 0:
                return c
        except OSError:
            continue
        finally:
            try:
                os.unlink(tp)
            except OSError:
                pass
    return None


def test_scripts_bash_syntax():
    bash = _find_working_bash()
    if not bash:
        print("  (跳过: 本机无可用 bash)")
        return
    up = packer.gen_upgrade_sh(_base_cluster(upgrade_to="v1.29.15"))
    with tempfile.TemporaryDirectory() as td:
        gen = Path(td) / "upgrade-cluster.sh"
        gen.write_text(up, encoding="utf-8", newline="\n")
        for f in (BASE / "server" / "deploy-cluster.sh", BASE / "server" / "uninstall-cluster.sh", gen):
            r = subprocess.run([bash, "-n", str(f)], capture_output=True, text=True)
            check("bash -n 语法通过: %s" % f.name, r.returncode == 0)


def main():
    for fn in (test_online_cn_static_fallback, test_config_custom_dirs, test_config_etcd_env_and_advance,
               test_config_ha_vip_and_backup, test_config_cri_cni_dns, test_config_validation_matrix,
               test_registry_tar_path_resolution, test_prepare_download_events, test_auto_download_wiring,
               test_auto_download_cancel_passthrough,
               test_registry_auth_and_data_dir,
               test_manifest_lifecycle_vars, test_uninstall_flag_matrix,
               test_upgrade_sh_content, test_deploy_sh_branches, test_scripts_bash_syntax):
        print(fn.__name__ + ":")
        fn()
    failed = [n for n, ok in CHECKS if not ok]
    print(("== 单测通过 (%d 项) ==" if not failed else "== 单测失败: %s ==") % (len(CHECKS) if not failed else failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
