部署步骤：

1. 构建Docker镜像
sudo docker build -t frr-srv6-node:latest .

2. 生成400节点拓扑定义
python3 generate_topology.py
- 输出文件：topology_400nodes.json

3. 生成FRR配置文件
python3 generate_configs.py topology_400nodes.json
输出目录：configs/ (包含400个 frr-*.conf 文件)

4. 部署网络
sudo python3 deploy.py topology_400nodes.json
- 清理旧环境：2-3分钟
- 启动400个容器：5-8分钟（并行10线程）
- 创建约800条链路：8-12分钟（并行10线程）
- ISIS收敛：3分钟
- 总计：约20-30分钟

5. 配置SRv6 End行为
chmod +x configure_srv6.sh
./configure_srv6.sh