# -*- coding: utf-8 -*-
"""packerlib: 打包器实现包(由 packer.py 单文件拆分而来)。

原 packer.py 的全部顶层符号在此聚合导出: 薄壳 packer.py 与
`from packerlib import *` 均可获得完整兼容符号表(含测试引用的下划线名)。
"""
from .paths import (  # 路径常量与根 logger
    BASE_DIR, CATALOG_FILE, CLUSTER_SH, DIST_DIR, HTML_FILE, LOGOS_JS,
    PLUGINS_DIR, SERVER_SH, SQL_DIR, TPL_DIR, WAREHOUSE, log,
)
from .util import (  # 通用工具/正则/密码
    IP_RE, LINUX_PATH_RE, MIRROR_RE, PASSWORD_RE, PROJECT_RE, PackError,
    USERNAME_RE, WEBHOOK_RE, bash_quote, env_quote, gen_nacos_token,
    gen_random_password, has_multihost, is_multihost, master_server_ip, tr,
    valid_ipv4,
)
from .catalog import (  # 服务目录与插件加载
    PLUGINS, PROXYSQL_META, _load_plugins, load_catalog,
)
from .validate import (  # 配置校验
    _ETCD_ENV_KEYS, _ETCD_ENV_ORDER, check_warehouse, validate_config,
)
from .nacos_sql import (  # nacos SQL 提取
    build_nacos_sql, extract_nacos_sql_from_tar,
)
from .compose import (  # compose/.env/备份生成
    DB_BACKUP_SERVICES, backup_crons, backupable_services, gen_backup_conf,
    gen_compose, gen_env, plugin_ctx, resolve_db,
    validate_compose_with_docker,
)
from .cluster import (  # K8s 集群配置与物料
    _PREPARE, _auto_build_artifact, _auto_download_cluster_materials,
    _prepare_module, _tar_valid, _yq, cluster_materials,
    gen_cluster_config, gen_cluster_inventory, gen_upgrade_sh,
)
from .manifest import (  # manifest.sh/摘要/images.txt
    build_summary_lines, gen_images_txt, gen_manifest_sh, services_of,
)
from .multinode import (  # 多机节点产物
    KRAFT_CLUSTER_ID, _node_backup_files, _node_kafka_block,
    _node_mysql_block, _node_redis_blocks, _node_sentinel_conf,
    gen_distribute_sh, gen_node_install_sh, gen_nodes, gen_proxysql_conf,
)
from .nginx_conf import (  # nginx 反代配置
    _PX_PROXY_HEADERS, _px_locations, _px_real_ip_lines, _px_server_block,
    gen_nginx_confs, px_ssl_rel,
)
from .progress import (  # 进度回调
    LogProgress, PackProgress,
)
from .builder import (  # 打包主流程
    BUNDLE_README, CLUSTER_ONLY_SH, MIRROR_PREFIXES,
    _tar_has_mirror_prefix, _tar_repo_tags, gen_pull_script,
    gen_retag_mirrors_sh, missing_materials, pack,
)
from .web import (  # 本地 Web 界面
    PACK_STATE, _fsize_mb, catalog_response, list_bundles, make_handler,
    run_web, start_pack_async,
)
from .cli import (  # CLI 入口
    EXAMPLE_CONFIG, main,
)

__all__ = [
    'BASE_DIR', 'CATALOG_FILE', 'CLUSTER_SH', 'DIST_DIR',
    'HTML_FILE', 'LOGOS_JS', 'PLUGINS_DIR', 'SERVER_SH',
    'SQL_DIR', 'TPL_DIR', 'WAREHOUSE', 'log',
    'IP_RE', 'LINUX_PATH_RE', 'MIRROR_RE', 'PASSWORD_RE',
    'PROJECT_RE', 'PackError', 'USERNAME_RE', 'WEBHOOK_RE',
    'bash_quote', 'env_quote', 'gen_nacos_token', 'gen_random_password',
    'has_multihost', 'is_multihost', 'master_server_ip', 'tr',
    'valid_ipv4', 'PLUGINS', 'PROXYSQL_META', '_load_plugins',
    'load_catalog', '_ETCD_ENV_KEYS', '_ETCD_ENV_ORDER', 'check_warehouse',
    'validate_config', 'build_nacos_sql', 'extract_nacos_sql_from_tar', 'DB_BACKUP_SERVICES',
    'backup_crons', 'backupable_services', 'gen_backup_conf', 'gen_compose',
    'gen_env', 'plugin_ctx', 'resolve_db', 'validate_compose_with_docker',
    '_PREPARE', '_auto_build_artifact', '_auto_download_cluster_materials', '_prepare_module',
    '_tar_valid', '_yq', 'cluster_materials', 'gen_cluster_config',
    'gen_cluster_inventory', 'gen_upgrade_sh', 'build_summary_lines', 'gen_images_txt',
    'gen_manifest_sh', 'services_of', 'KRAFT_CLUSTER_ID', '_node_backup_files',
    '_node_kafka_block', '_node_mysql_block', '_node_redis_blocks', '_node_sentinel_conf',
    'gen_distribute_sh', 'gen_node_install_sh', 'gen_nodes', 'gen_proxysql_conf',
    '_PX_PROXY_HEADERS', '_px_locations', '_px_real_ip_lines', '_px_server_block',
    'gen_nginx_confs', 'px_ssl_rel', 'LogProgress', 'PackProgress',
    'BUNDLE_README', 'CLUSTER_ONLY_SH', 'MIRROR_PREFIXES', '_tar_has_mirror_prefix',
    '_tar_repo_tags', 'gen_pull_script', 'gen_retag_mirrors_sh', 'missing_materials',
    'pack', 'PACK_STATE', '_fsize_mb', 'catalog_response',
    'list_bundles', 'make_handler', 'run_web', 'start_pack_async',
    'EXAMPLE_CONFIG', 'main',
]
