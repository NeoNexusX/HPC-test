# E-HPC Instant + EPC Insta 测试落地方案

本目录给出一套可先在本地验证、再迁移到阿里云 E-HPC Instant 的最小闭环：基础 Python 运行时镜像、单节点 smoke test、基于 FIO 的文件型 SSD 测试、OSS 上传/下载速度测试、Slurm 多节点测试入口、云上配置清单，以及 GitHub Actions 推送 ACR 的工作流。

## 0. 术语和假设

本文把 **EPC Insta** 视为待验证的 EPC Insta 程序/服务（可执行文件、Python 包或容器），把 **ARC 镜像**视为你们内部或阿里云控制台中名为 ARC 的自定义镜像/镜像族。阿里云控制台的地域、实例族、镜像 ID 和产品命名会随账号、地域和发布时间变化，因此部署时必须以控制台实际可选项为准。

E-HPC Instant 的公共基础设施建议采用：VPC + 交换机 + 安全组 + 登录密钥 + OSS + Slurm 调度器 + E-HPC 管理/计算节点。NAS 不作为本方案的测试依赖；只有 EPC Insta 明确需要共享 POSIX 文件系统时才额外配置 NAS。

## 1. 推荐目标架构

```text
管理员电脑
    |
    | SSH（只允许堡垒机/办公网段）
    v
VPC
  ├─ 管理节点（E-HPC/Slurm controller、登录节点）
  ├─ 计算节点 × N（EPC Insta 任务）
  ├─ OSS：输入、镜像构建产物、结果归档
  └─ OSS：输入、结果、日志和归档
```

建议把公网暴露面压到最小：管理节点使用私网，SSH 仅开放给固定办公 CIDR；计算节点只开放 VPC 内部通信。生产环境把登录节点放在堡垒机后面，并给 OSS 使用 RAM 角色而不是把 AccessKey 写进脚本或镜像。

### 这些云资源分别做什么

| 资源 | 作用 | 在本方案中的位置 |
|---|---|---|
| VPC | 你的云上私有网络，隔离节点和访问边界 | 放置 E-HPC 管理节点和计算节点 |
| vSwitch | VPC 中按可用区划分的子网，分配私网 IP | 让节点在同一可用区内互通 |
| 安全组 | 节点级虚拟防火墙 | 只开放 SSH、Slurm 和必要的应用端口 |
| OSS | 对象存储，适合大文件、数据集、镜像构建产物和结果归档 | 保存 `datasets/`、`results/` 和构建产物 |

VPC/vSwitch/安全组解决“节点怎么安全联网”；OSS 解决“数据和结果怎么长期归档”；ACR 解决“容器镜像怎么版本化和分发”。NAS 是可选的共享文件系统，本方案不检查 NAS 的连通性、读写或性能。

## 2. 阿里云准备项

### 2.1 账号和配额

1. 选择一个支持 E-HPC Instant、目标计算实例族和目标镜像的地域/可用区。
2. 通过 RAM 创建部署角色，至少覆盖 E-HPC、ECS、VPC、OSS、云监控和日志服务的最小权限；不要使用主账号 AccessKey。
3. 提前检查 vCPU、实例数量、云盘和 GPU（如果 EPC Insta 使用 GPU）配额。
4. 创建密钥对，私钥只保存在管理员电脑；不要上传到 OSS 或写入镜像。

### 2.2 网络

创建一个专用 VPC 和一个与计算节点同可用区的 vSwitch。安全组规则建议如下：

| 方向 | 协议/端口 | 来源/目标 | 用途 |
|---|---|---|---|
| 入站 | TCP 22 | 办公网段或堡垒机安全组 | 管理登录 |
| 入站 | TCP 6817-6819 | VPC 安全组自身 | Slurm controller/agent |
| 入站 | TCP/UDP 6000-6100 | VPC 安全组自身 | MPI/分布式运行时（按 EPC Insta 文档收窄） |
| 出站 | TCP 443 | 0.0.0.0/0 或 NAT 网关 | 拉取镜像/依赖；生产可改为白名单 |
| 入站 | 其他 | 拒绝 | 默认拒绝 |

如果使用 RDMA/EFA 类网络或 GPU 通信，必须选择产品文档明确支持的实例族、镜像和驱动组合；不要仅凭普通 ECS 实例规格推断可用。

### 2.3 存储

* OSS：创建私有 Bucket，目录建议为 `artifacts/`、`datasets/`、`results/<run-id>/`；开启版本控制和服务端加密。
* 云盘：管理节点保存系统和临时日志；计算节点使用独立 ESSD 作为 `/data`，任务结果写 OSS。

## 3. E-HPC Instant 集群配置

在控制台创建 E-HPC Instant 集群时按以下顺序填写：

1. **地域/可用区**：与 VPC、vSwitch 和 OSS 访问链路一致。
2. **网络**：选择上一步创建的 VPC、vSwitch、安全组；关闭不需要的公网 IP，使用 NAT 或专用出网。
3. **调度器**：选择 Slurm（或你账号已启用的等价调度器），记录 controller、登录节点和计算节点的主机名/IP。
4. **镜像**：先用阿里云官方 Linux 镜像验证集群；如果控制台确实提供 ARC 镜像，再在隔离测试集群中验证 ARC 镜像的驱动、Python、容器运行时和 Slurm 兼容性。
5. **节点规格**：管理节点使用通用型小规格；计算节点按 EPC Insta 的 CPU/内存/GPU/网络需求选择，并从 1 个节点开始。
6. **节点数量**：先固定 1 个管理节点 + 1 个计算节点；单节点通过后再扩展到 2、4、8 个计算节点。
7. **初始化脚本**：在节点启动阶段准备 Docker/Podman（若镜像已预装则跳过）、拉取 ACR 测试镜像、创建 `/opt/epc-insta-test`。FIO 和 ossutil 已包含在本方案的容器镜像中。
8. **日志**：启用云监控/日志服务，至少采集 Slurm、系统、容器 stdout/stderr 和任务退出码。

### ARC 镜像结论

ARC 不是 E-HPC Instant 的必选公共依赖。只有在你们已有 ARC 镜像 ID、镜像构建说明或内部制品仓库时，才把它作为节点镜像；否则使用官方基础镜像 + 下文 Dockerfile 构建自定义镜像。切换 ARC 前必须确认：操作系统版本、内核、GPU 驱动/CUDA（如有）、Docker/Containerd、Python 版本、Slurm 客户端、时钟同步和许可证/仓库访问。

### E-HPC Instant 镜像设计

建议分成两层：节点镜像负责操作系统、Slurm 客户端、Docker/Containerd、驱动和监控代理；ACR 容器镜像负责 EPC Insta、Python 依赖和应用启动命令。这样更新应用时只重新构建 ACR 镜像，不必频繁重做 E-HPC 节点镜像。节点需要能通过私网访问 ACR（或按控制台配置公网/NAT），并使用 RAM 角色、临时凭证或节点预置的登录机制拉取镜像。

## 4. 本地测试

### 4.1 直接运行

需要 Python 3.10+，在本目录执行：

```powershell
python .\scripts\epc_insta_smoke.py --out .\work\local-result.json
```

脚本默认验证 Python 运行时、本地临时目录读写和可选 EPC Insta 命令。它不会假设你已经安装了 EPC Insta。

```powershell
$env:EPC_INSTA_CMD = 'python -c "print(''epc-insta-ok'')"'
python .\scripts\epc_insta_smoke.py --out .\work\epc-result.json
```

真实程序建议通过环境变量传入，例如：

```powershell
$env:EPC_INSTA_CMD = "python -m epc_insta --version"
python .\scripts\epc_insta_smoke.py --out .\work\epc-version.json
```

### 4.2 容器运行

```powershell
docker build -f .\image\Dockerfile -t epc-insta-test:py311 .
docker run --rm -v "${PWD}\work:/work" epc-insta-test:py311
```

Dockerfile 当前安装 Python 3.11、FIO、阿里云官方 ossutil 2.4.0，并读取 `image/requirements.txt` 安装 Python 依赖。如果 EPC Insta 在私有仓库，先登录镜像仓库，再通过 `requirements.txt` 或派生 Dockerfile 安装/复制程序。不要把仓库密码写进 Dockerfile。

### 4.3 文件型 SSD 测试

脚本加入了标准 FIO 顺序/随机读写组合，参数与 `config/fio-ssd-file-test.fio` 对齐，使用挂载目录中的临时文件，不直接操作 `/dev/vd*` 等裸设备。默认不执行，避免误测或产生较大 I/O；在 E-HPC 节点上明确指定数据盘挂载点后运行：

```bash
python3 /opt/epc-insta-test/scripts/epc_insta_smoke.py \
  --ssd-test --ssd-dir /data --ssd-size 1G --ssd-runtime 30 \
  --out /tmp/ssd-${HOSTNAME}.json
```

如果数据盘挂载在 `/data`，将 `--ssd-dir` 改为 `/data`。结果会记录顺序读写、随机读写的 IOPS、带宽和平均完成延迟。该测试会创建并删除测试文件，仍然应使用专用测试目录，不要指向系统根目录或生产数据目录。

如果要使用完整 FIO job 文件进行复测：

```bash
mkdir -p /mnt/epc-insta-fio
sed 's#directory=/mnt/epc-insta-fio#directory=/data/epc-insta-fio#' \
  /opt/epc-insta-test/config/fio-ssd-file-test.fio > /tmp/epc-insta.fio
mkdir -p /data/epc-insta-fio
fio /tmp/epc-insta.fio --output-format=json \
  > /tmp/fio-${HOSTNAME}.json
```

基准测试中的 `1G` 文件大小和 `30` 秒运行时间适合 smoke test；性能验收时应按业务数据规模、队列深度和重复次数重新设定，并记录 ESSD 磁盘类型、容量、PL 等级、实例规格和可用区。

### 4.4 OSS 上传/下载速度测试

OSS 测试使用容器镜像内的阿里云官方 `ossutil`，不需要在宿主机另行安装。通过 GitHub Secrets 传入 OSS 测试凭证，或在 E-HPC 节点使用等价的 RAM 角色，然后执行：

```bash
export OSS_TEST_URI=oss://<bucket>/results/oss-smoke-${HOSTNAME}.bin
python3 /path/to/scripts/epc_insta_smoke.py \
  --oss-test \
  --oss-uri "$OSS_TEST_URI" \
  --oss-size 64M \
  --out /tmp/oss-${HOSTNAME}.json
```

JSON 结果包含上传/下载字节数、耗时、`speed_mbps`、`speed_bytes_per_sec` 和 SHA-256 校验。默认会删除测试对象；需要保留时添加 `--oss-keep-object`。OSS 测试测量的是当前节点到 OSS 的对象传输路径，不等于 NAS 或本地 ESSD 性能。

## 5. Slurm 多节点测试

把本目录上传到登录节点的本地目录（例如 `/opt/epc-insta-test`），或者使用你已有的共享目录，然后：

```bash
srun -N 2 -n 2 --ntasks-per-node=1 \
  python /opt/epc-insta-test/scripts/epc_insta_smoke.py \
  --out /tmp/${SLURM_JOB_ID}-${SLURM_PROCID}.json
```

更稳定的做法是提交 `sbatch` 作业：

```bash
#!/bin/bash
#SBATCH -N 2
#SBATCH --ntasks-per-node=1
#SBATCH -J epc-insta-smoke
#SBATCH -o /tmp/%x-%j.out
set -euo pipefail
python /opt/epc-insta-test/scripts/epc_insta_smoke.py \
  --out /tmp/${SLURM_JOB_ID}-${SLURM_PROCID}.json
```

多节点通过条件：每个 task 的 JSON 中 `overall_pass=true` 且节点名不同。如果 EPC Insta 自带启动器，替换 `srun` 的命令部分，并保留退出码和结果归档。结果可在作业结束后上传 OSS。

## 6. GitHub Actions 自动推送 ACR

阿里云配置见 `docs/alicloud-setup.md`，GitHub/ACR 配置见 `docs/github-acr-setup.md`，凭证放置和权限边界见 `config/credentials-and-permissions.md`。

仓库根目录应包含 `image/Dockerfile`、`scripts/`、`config/` 和 `.github/workflows/build-and-push-acr.yml`。工作流在 `main` 分支或 `v*.*.*` 标签推送时构建镜像，并推送以下标签：分支名、Git 短 SHA、版本标签，以及默认分支的 `latest`。

先在 ACR 控制台创建实例（个人版、经济版或企业版）、命名空间和私有仓库，并确认 GitHub 公网 Runner 能访问 ACR 登录域名。E-HPC 节点如果只允许私网访问，则另行配置 ACR VPC 访问/域名解析和安全组；GitHub Runner 不在你的 VPC 内，不能直接访问只开放私网的 ACR 端点。ACR 可以被 E-HPC 节点使用，但节点镜像是否预装 Docker/Containerd、是否能访问 ACR、以及是否支持 GPU runtime，仍取决于 E-HPC 节点镜像和网络配置。

在 GitHub 仓库的 **Settings > Secrets and variables > Actions** 中创建以下 Secrets：

| Secret | 示例 | 说明 |
|---|---|---|
| `ACR_REGISTRY` | `registry.cn-hangzhou.aliyuncs.com` | ACR 登录域名，不带 `https://` |
| `ACR_NAMESPACE` | `my-team` | ACR 命名空间 |
| `ACR_USERNAME` | `github-pusher` | ACR/RAM 推送账号 |
| `ACR_PASSWORD` | `***` | ACR 访问凭证或短期密码 |

给这个账号最小化授予目标命名空间/仓库的推送权限。不要把 AccessKey 写入 YAML；若组织已有 GitHub OIDC 到阿里云 RAM 的方案，可把 `docker/login-action` 换成组织批准的 OIDC 登录步骤。

提交到 `main` 后，工作流会把镜像推送到：

```text
<ACR_REGISTRY>/<ACR_NAMESPACE>/epc-insta-test:<tag>
```

GitHub Actions 成功后，在 E-HPC 登录节点验证拉取：

```bash
docker login <ACR_REGISTRY>
docker pull <ACR_REGISTRY>/<ACR_NAMESPACE>/epc-insta-test:<git-sha-or-version>
docker run --rm --user 10001 \
  -v /tmp/epc-insta-results:/work \
  <ACR_REGISTRY>/<ACR_NAMESPACE>/epc-insta-test:<git-sha-or-version>
```

ACR 负责容器镜像分发，E-HPC 节点镜像仍负责操作系统、驱动、Slurm 和 GitHub self-hosted runner；二者不要混成一个镜像层次。推送到 `main` 后，工作流会先构建/推送镜像，再在带有 `self-hosted, linux, e-hpc` 标签的 E-HPC runner 上自动执行 SSD 和 OSS 测试，并上传 JSON 结果 artifact。

## 7. 结果判定和扩容顺序

* **P0 基线**：单节点、无 GPU、官方基础镜像，脚本所有基础检查通过。
* **P1 应用**：注入实际 EPC Insta 命令，命令退出码为 0，版本和依赖输出可追溯。
* **P2 OSS/节点**：OSS 上传和下载校验通过，2 个节点并行运行且 JSON 结果均为 `overall_pass=true`。
* **P3 性能**：固定输入、节点数、镜像 digest、ESSD 规格和 OSS 对象大小，重复 3 次，记录 wall time、CPU/GPU 利用率、SSD IOPS、OSS 吞吐和失败重试。

每次实验保存：Git commit、Docker image digest、E-HPC 集群 ID、地域/可用区、实例规格、镜像 ID、Slurm job ID、输入数据版本和结果 JSON。

## 8. 常见故障定位

* 作业 `PENDING`：检查配额、节点健康、分区和实例库存。
* 节点能 SSH 但 `srun` 失败：检查 Slurm 时间同步、主机名解析、安全组 6817-6819 和 controller/agent 状态。
* 容器无法启动：检查 Docker/Containerd 服务、用户组权限、镜像架构（x86_64/ARM64）和磁盘空间。
* GPU 程序报驱动错误：核对 GPU 实例、宿主机驱动、容器 runtime 和 CUDA 版本矩阵。
* OSS 测试失败：检查 `ossutil` 是否安装、节点到 OSS 的 DNS/路由、RAM 角色或访问凭证，以及 Bucket/前缀权限。
* 拉包失败：不要把凭据放入镜像；使用 RAM 角色、私有镜像仓库登录令牌或预热镜像。

## 9. 上线前检查清单

- [ ] 控制台中确认 E-HPC Instant、EPC Insta、ARC 的准确产品名和地域可用性。
- [ ] 确认实例、GPU、OSS、vCPU 和公网带宽配额。
- [ ] 完成 RAM 最小权限、VPC/安全组、密钥对和审计日志配置。
- [ ] 官方基础镜像单节点通过。
- [ ] ARC 镜像（若确有）单节点通过并记录镜像 ID/版本。
- [ ] EPC Insta 版本、依赖、许可证和数据集版本固定。
- [ ] 2 节点 Slurm smoke test 通过，结果已上传 OSS。
- [ ] OSS 上传/下载速度测试通过，并记录对象大小、地域和节点规格。
- [ ] SSD FIO 测试使用专用挂载目录，结果已保存并与实例规格关联。
- [ ] GitHub Actions 使用 Secrets 推送 ACR，未在日志中暴露凭据。
- [ ] 完成成本预算、自动伸缩/关机策略和失败告警。
