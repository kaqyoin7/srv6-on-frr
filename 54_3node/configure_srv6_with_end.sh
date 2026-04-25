#!/bin/bash

# 步骤1: 为每个节点配置SRv6 End行为

# -------------------------------------------------------------------------
# 容器一内执行
ip -6 route add fc00:a1::/48 encap seg6local action End dev lo

# 容器二内执行
ip -6 route add fc00:a2::/48 encap seg6local action End dev lo

# 容器三内执行
ip -6 route add fc00:a3::/48 encap seg6local action End dev lo
# -------------------------------------------------------------------------

# 容器一 -> 容器三 SRv6路径: container 1 -> contianer 2 -> container3
# -------------------------------------------------------------------------
# 容器一内执行
# 注：<iface_name>需要改为容器一内与容器二相连的网口名称
ip -6 route add fc00:a3::1/128 encap seg6 mode encap segs fc00:a2::1,fc00:a3::1 dev <iface_name>
# -------------------------------------------------------------------------
