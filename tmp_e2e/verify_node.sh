#!/bin/bash
# S6 三节点产物核验 (在节点容器内执行)
C=/opt/middleware/docker-compose.yml
I=/opt/middleware/install-node.sh
S=/opt/middleware/conf/redis/sentinel.conf
g(){ grep -c "$1" "$2" 2>/dev/null || echo 0; }
echo "kid=$(grep -o 'KAFKA_CFG_NODE_ID: [0-9]*' $C | awk '{print $2}')"
echo "voters=$(g '1@10.66.0.11:9093,2@10.66.0.12:9093,3@10.66.0.13:9093' $C)"
echo "adv=$(g "INTERNAL://$(hostname -i | awk '{print $1}'):9092" $C)"
echo "mro=$(g -- '--read-only=ON' $C)"
echo "msrc=$(g 'CHANGE REPLICATION SOURCE TO' $I)"
echo "mhost=$(grep -o 'SOURCE_HOST=.[0-9.]*' $I | head -1)"
echo "rrepl=$(g 'replicaof 10.66.0.11 16379' $C)"
echo "sent=$(g 'sentinel monitor mymaster 10.66.0.11' $S)"
echo "load=$(g 'load -i' /var/log/fake-docker.log)"
echo "up=$(g 'compose up -d' /var/log/fake-docker.log)"
echo "mycnf=$(ls /opt/middleware/mysql/my.cnf 2>/dev/null | wc -l)"
echo "kafkaconf=$(ls /opt/middleware/conf/kafka/server.properties 2>/dev/null | wc -l)"
