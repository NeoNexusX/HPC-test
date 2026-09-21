# 阿里云配置指南

这份文档按最小测试环境编写：1 个管理/登录节点、1 个计算节点、1 块独立 ESSD 测试盘、一个私有 OSS Bucket 和一个 ACR 仓库。NAS 不作为本方案的测试依赖；如果你的 EPC Insta 应用需要共享 POSIX 文件系统，可以后续单独增加 NAS。

## 1. 资源拓扑

```text
VPC
  └─ vSwitch
       ├─ E-HPC 管理/登录节点
       └─ E-HPC 计算节点 ── ESSD (/data)

OSS：输入数据、测试结果、日志和归档
ACR：EPC Insta 容器镜像
```

VPC 是私有网络，vSwitch 是可用区内的子网，安全组是节点级虚拟防火墙，OSS 是对象存储，ACR 是容器镜像仓库。NAS 在本方案中不是必选项，也不由 smoke test 检查。

## 2. RAM 和配额

使用 RAM 用户或 RAM 角色执行部署，避免使用主账号 AccessKey。提前检查以下配额：

* E-HPC Instant 集群和计算节点数量
* 目标 ECS 实例族的 vCPU
* ESSD 容量和 PL 等级
* OSS、ACR 和 NAT 的使用权限
* 如果使用 GPU，检查 GPU 实例和驱动配额

创建一对 E-HPC SSH 密钥。私钥只保存于管理员电脑，不上传到 GitHub、OSS 或镜像。

## 3. 创建 VPC、vSwitch 和安全组

在 `VPC -> 专有网络` 创建：

```text
VPC：hpc-test-vpc
CIDR：10.20.0.0/16
```

在相同地域和可用区创建 vSwitch：

```text
vSwitch：hpc-test-vswitch
CIDR：10.20.1.0/24
```

创建安全组 `hpc-test-sg`，建议规则：

| 方向 | 协议/端口 | 来源 | 说明 |
|---|---|---|---|
| 入站 | TCP 22 | 固定办公公网 IP 或堡垒机 | SSH |
| 入站 | TCP 6817-6819 | 安全组自身 | Slurm 内部通信 |
| 入站 | TCP/UDP 6000-6100 | 安全组自身 | 只有使用 MPI/分布式运行时才开放 |
| 出站 | TCP 443 | 允许访问 | 系统更新、依赖下载、ACR/OSS |

计算节点不要分配公网 IP。没有公网 IP 时，使用 NAT 网关让节点访问 HTTPS。不要把 SSH 22 端口开放到 `0.0.0.0/0`。

## 4. 创建 OSS

在 `OSS -> Bucket -> 创建 Bucket` 创建私有 Bucket：

```text
地域：和 E-HPC 相同
读写权限：私有
服务端加密：开启
版本控制：建议开启
```

建议目录：

```text
datasets/
results/
logs/
artifacts/
```

OSS 测试使用 `ossutil`，脚本会生成临时文件、上传、下载、输出上传/下载速度和 SHA-256 校验结果，并默认删除临时对象。节点需要先安装阿里云官方 `ossutil`，完成登录或绑定 RAM 角色：

```bash
export OSS_TEST_URI=oss://<bucket>/results/oss-smoke-${HOSTNAME}.bin
python3 scripts/epc_insta_smoke.py \
  --oss-test --oss-size 64M --oss-uri "$OSS_TEST_URI" \
  --out /tmp/oss-result.json
cat /tmp/oss-result.json
```

需要保留对象时添加 `--oss-keep-object`。生产环境应使用专用测试前缀，避免覆盖业务对象。

## 5. 创建 ACR

在 `容器镜像服务 ACR` 创建命名空间和私有仓库：

```text
命名空间：my-team
仓库：epc-insta-test
权限：私有
```

创建专用推送账号，只授予目标仓库拉取和推送权限。登录域名以控制台显示为准，常见格式为 `registry.cn-hangzhou.aliyuncs.com`。GitHub 官方 Runner 不在你的 VPC 内；如果 ACR 只开放私网，需要使用 VPC 内的 self-hosted runner。

## 6. 创建 E-HPC Instant 集群

选择上面的 VPC、vSwitch 和安全组，调度器选择 Slurm，初始配置 1 个管理/登录节点和 1 个计算节点。第一轮使用 E-HPC 官方支持的 Linux 镜像，不要一开始使用未知 ARC/自定义节点镜像。

节点镜像负责 Linux、Slurm、容器运行时、驱动和监控；ACR 镜像负责 Python、EPC Insta 和应用依赖。为计算节点增加单独 ESSD，并挂载到 `/data`，不要用系统盘做 FIO 测试。

## 7. 节点验证

```bash
python3 scripts/epc_insta_smoke.py --out /tmp/baseline.json
python3 scripts/epc_insta_smoke.py --ssd-test --ssd-dir /data \
  --ssd-size 1G --ssd-runtime 30 --out /tmp/ssd.json
export OSS_TEST_URI=oss://<bucket>/results/oss-smoke-${HOSTNAME}.bin
python3 scripts/epc_insta_smoke.py --oss-test --oss-uri "$OSS_TEST_URI" \
  --oss-size 64M --out /tmp/oss.json
```

双节点 Slurm 测试只验证节点调度和应用运行，不测试 NAS：

```bash
srun -N 2 -n 2 --ntasks-per-node=1 \
  python3 /path/to/scripts/epc_insta_smoke.py \
  --out /tmp/${SLURM_JOB_ID}-${SLURM_PROCID}.json
```

## 8. 清理

测试完成后删除或停止 E-HPC 集群、ECS 节点、NAT 网关、临时 ESSD、不再需要的 ACR 实例和 OSS 测试对象。保留这些资源可能继续产生费用。
