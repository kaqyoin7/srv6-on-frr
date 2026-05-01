ip link add vxlan_29300 type vxlan id 29300 dstport 4789 remote 192.168.0.27 local 192.168.0.26 dev ens5f0
ip link set vxlan_29300 up
PID=$(docker inspect -f '{{.State.Pid}}' srv6-r30n0)
ip link set dev vxlan_29300 netns $PID name r30n0-r29n0
nsenter -t $PID -n ip -6 addr add  fc00:9000:1d::2/64 dev r30n0-r29n0
nsenter -t $PID -n ip link set dev r30n0-r29n0 up