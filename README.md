# E-HPC Instant + EPC Insta 测试落地方案

本目录给出一套可先在本地验证、再迁移到阿里云 E-HPC Instant 的最小闭环：基础 Python 运行时镜像、单节点 smoke test、基于 FIO 的文件型 SSD 测试、Slurm 多节点测试入口、共享存储检查、云上配置清单，以及推送到 GitHub 后自动构建 GHCR 镜像的工作流。

## 快速配置清单

要让“本地触发 → E-HPC 执行 → 拉取镜像 → 测试 → OSS 归档”跑起来，最少需要配置以下内容：

1. **GitHub**：把代码放到仓库的 `main` 分支；在仓库 `Settings > Actions > General` 将 Workflow permissions 设为允许读写。推送 `main` 后，`.github/workflows/build-and-push-ghcr.yml` 会自动生成 `ghcr.io/<owner>/<repo>/epc-insta-test:latest` 和 SHA 标签。
2. **镜像内容**：把真实 EPC Insta 程序和依赖补进 `image/Dockerfile`、`image/requirements.txt`，并确认容器内命令可执行。当前镜像只包含测试框架，不包含你的业务程序。
3. **E-HPC**：准备可 SSH 登录的 Slurm 登录节点；计算节点安装 Docker、能访问 `ghcr.io`，并把 NAS 挂载到 `/shared`。`sbatch` 用户需要有权限运行 Docker。
4. **OSS**：创建私有 Bucket 和 `results/` 前缀；在计算节点安装并配置 `ossutil`，推荐给节点绑定 RAM Role。不要把 AccessKey 或 GitHub PAT 写入仓库、镜像或命令参数。
5. **本地电脑**：准备 Python 3.10+、`ssh`、`scp`，以及登录节点私钥。首次使用 GHCR 私有镜像时，还要在计算节点配置 `REGISTRY=ghcr.io`、`REGISTRY_USERNAME`、`REGISTRY_PASSWORD`（仅 `read:packages` 权限）；公开 GHCR 镜像不需要登录。

详细参数模板见 `config/cluster-parameters.example.yaml` 和 `config/github-ghcr.example.md`。

## 0. 术语和假设

本文把 **EPC Insta** 视为待验证的 EPC Insta 程序/服务（可执行文件、Python 包或容器），把 **ARC 镜像**视为你们内部或阿里云控制台中名为 ARC 的自定义镜像/镜像族。阿里云控制台的地域、实例族、镜像 ID 和产品命名会随账号、地域和发布时间变化，因此部署时必须以控制台实际可选项为准。

E-HPC Instant 的公共基础设施建议采用：VPC + 交换机 + 安全组 + 登录密钥 + OSS/NAS 共享存储 + Slurm 调度器 + E-HPC 管理/计算节点。EPC Insta 应先在容器中跑通，再决定是否做成自定义云镜像。

## 1. 推荐目标架构

```text
管理员电脑
    |
    | SSH（只允许堡垒机/办公网段）
    v
VPC
  ├─ 管理节点（E-HPC/Slurm controller、登录节点）
  ├─ 计算节点 × N（EPC Insta 任务）
  ├─ OSS：输入、测试结果和日志归档
  └─ NAS：/shared，保存程序、数据集、日志
```

建议把公网暴露面压到最小：管理节点使用私网，SSH 仅开放给固定办公 CIDR；计算节点只开放 VPC 内部通信。生产环境把登录节点放在堡垒机后面，并给 OSS/NAS 使用 RAM 角色而不是把 AccessKey 写进脚本或镜像。

### 这些云资源分别做什么

| 资源 | 作用 | 在本方案中的位置 |
|---|---|---|
| VPC | 你的云上私有网络，隔离节点和访问边界 | 放置 E-HPC 管理节点、计算节点和 NAS |
| vSwitch | VPC 中按可用区划分的子网，分配私网 IP | 让节点在同一可用区内互通 |
| 安全组 | 节点级虚拟防火墙 | 只开放 SSH、Slurm 和必要的应用端口 |
| OSS | 对象存储，适合大文件、数据集和结果归档 | 保存 `datasets/`、`results/` 和日志 |
| NAS | 共享文件系统，多个节点同时挂载读写 | 挂载到 `/shared`，放脚本、日志和任务结果 |
| GHCR | 容器镜像仓库，版本化和分发测试镜像 | 推送 `main` 自动更新 `latest` |

VPC/vSwitch/安全组解决“节点怎么安全联网”；NAS 解决“多个节点怎么看到同一目录”；OSS 解决“数据和结果怎么长期归档”；GHCR 解决“容器镜像怎么版本化和分发”。GHCR 不替代 VPC、NAS、OSS 或 E-HPC 节点镜像。

## 2. 阿里云准备项

### 2.1 账号和配额

1. 选择一个支持 E-HPC Instant、目标计算实例族和目标镜像的地域/可用区。
2. 通过 RAM 创建部署角色，至少覆盖 E-HPC、ECS、VPC、OSS、NAS、云监控和日志服务的最小权限；不要使用主账号 AccessKey。
3. 提前检查 vCPU、实例数量、云盘、NAS 吞吐和 GPU（如果 EPC Insta 使用 GPU）配额。
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
* NAS：创建与 VPC 同地域的文件系统和挂载点，挂载到所有节点的 `/shared`。E-HPC 创建向导中若已提供共享存储选项，优先使用向导创建的挂载配置。
* 云盘：管理节点保存系统和临时日志；计算节点使用按需临时盘，任务结果统一写 OSS/NAS。

## 3. E-HPC Instant 集群配置

在控制台创建 E-HPC Instant 集群时按以下顺序填写：

1. **地域/可用区**：与 VPC、vSwitch、NAS 挂载点和 OSS 访问链路一致。
2. **网络**：选择上一步创建的 VPC、vSwitch、安全组；关闭不需要的公网 IP，使用 NAT 或专用出网。
3. **调度器**：选择 Slurm（或你账号已启用的等价调度器），记录 controller、登录节点和计算节点的主机名/IP。
4. **镜像**：先用阿里云官方 Linux 镜像验证集群；如果控制台确实提供 ARC 镜像，再在隔离测试集群中验证 ARC 镜像的驱动、Python、容器运行时和 Slurm 兼容性。
5. **节点规格**：管理节点使用通用型小规格；计算节点按 EPC Insta 的 CPU/内存/GPU/网络需求选择，并从 1 个节点开始。
6. **节点数量**：先固定 1 个管理节点 + 1 个计算节点；单节点通过后再扩展到 2、4、8 个计算节点。
7. **共享存储**：挂载 NAS 到 `/shared`，或者通过 OSSUtil/SDK 在任务前后同步数据。
8. **初始化脚本**：在节点启动阶段安装 Docker/Podman（若镜像已预装则跳过）、安装 `ossutil`、创建 `/opt/epc-insta-test` 和 `/shared/results`。测试镜像由每次 Slurm 作业按参数拉取。
9. **日志**：启用云监控/日志服务，至少采集 Slurm、系统、容器 stdout/stderr 和任务退出码。

### ARC 镜像结论

ARC 不是 E-HPC Instant 的必选公共依赖。只有在你们已有 ARC 镜像 ID、镜像构建说明或内部制品仓库时，才把它作为节点镜像；否则使用官方基础镜像 + 下文 Dockerfile 构建自定义镜像。切换 ARC 前必须确认：操作系统版本、内核、GPU 驱动/CUDA（如有）、Docker/Containerd、Python 版本、Slurm 客户端、时钟同步和许可证/仓库访问。

### E-HPC Instant 镜像设计

建议分成两层：节点镜像负责操作系统、Slurm 客户端、Docker/Containerd、驱动和监控代理；GHCR 容器镜像负责 EPC Insta、Python 依赖和应用启动命令。这样更新应用时只重新构建 GHCR 镜像，不必频繁重做 E-HPC 节点镜像。节点需要能访问 `ghcr.io`，并使用公开镜像、Docker credential helper 或节点预置的只读 PAT 拉取镜像。

## 4. 本地测试

### 4.1 直接运行

需要 Python 3.10+，在本目录执行：

```powershell
python .\scripts\epc_insta_smoke.py --out .\work\local-result.json
```

脚本默认验证 CPU、内存、临时目录读写、共享目录（若存在）和可选 EPC Insta 命令。它不会假设你已经安装了 EPC Insta。

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

Dockerfile 当前安装 Python 3.11、FIO，并读取 `image/requirements.txt` 安装 Python 依赖。如果 EPC Insta 在私有仓库，先登录镜像仓库，再通过 `requirements.txt` 或派生 Dockerfile 安装/复制程序。不要把仓库密码写进 Dockerfile。

### 4.3 文件型 SSD 测试

脚本加入了标准 FIO 顺序/随机读写组合，参数与 `config/fio-ssd-file-test.fio` 对齐，使用挂载目录中的临时文件，不直接操作 `/dev/vd*` 等裸设备。默认不执行，避免误测或产生较大 I/O；在 E-HPC 节点上明确指定数据盘挂载点后运行：

```bash
python3 /shared/epc-insta-test/scripts/epc_insta_smoke.py \
  --ssd-test --ssd-dir /mnt --ssd-size 1G --ssd-runtime 30 \
  --out /shared/results/ssd-${HOSTNAME}.json
```

如果数据盘挂载在 `/data`，将 `--ssd-dir` 改为 `/data`。结果会记录顺序读写、随机读写的 IOPS、带宽和平均完成延迟。该测试会创建并删除测试文件，仍然应使用专用测试目录，不要指向系统根目录或生产数据目录。

如果要使用完整 FIO job 文件进行复测：

```bash
mkdir -p /mnt/epc-insta-fio
sed 's#directory=/mnt/epc-insta-fio#directory=/data/epc-insta-fio#' \
  /shared/epc-insta-test/config/fio-ssd-file-test.fio > /tmp/epc-insta.fio
mkdir -p /data/epc-insta-fio
fio /tmp/epc-insta.fio --output-format=json \
  > /shared/results/fio-${HOSTNAME}.json
```

基准测试中的 `1G` 文件大小和 `30` 秒运行时间适合 smoke test；性能验收时应按业务数据规模、队列深度和重复次数重新设定，并记录 ESSD 磁盘类型、容量、PL 等级、实例规格和可用区。

## 5. 本地触发到 OSS 的自动闭环

项目提供 `scripts/trigger_epc_test.py`，用于从本地电脑发起一次单节点远程测试。它通过 SSH 登录 E-HPC 登录节点，自动上传/更新作业执行脚本并提交 Slurm 作业；计算节点收到作业后会自动拉取 GHCR 镜像、启动容器、执行测试，最后使用节点上的 `ossutil` 将运行目录上传到 OSS。

```powershell
python .\scripts\trigger_epc_test.py `
  --ssh-target user@login-node `
  --ssh-key $env:EPC_SSH_KEY `
  --image ghcr.io/<github-owner>/<github-repo>/epc-insta-test:latest `
  --oss-uri oss://private-bucket/results `
  --epc-command "python -m epc_insta --version" `
  --wait
```

一次运行的结果会放在 `oss://private-bucket/results/<run-id>/`，通常包括：

* `manifest.json`：镜像、Slurm 作业、测试退出码、上传状态和最终判定。
* `smoke-result.json`：容器内 Smoke Test 的详细结果。
* `docker-pull.log`、`docker.log`：拉镜像和容器标准输出/错误。
* `oss-upload.log`：OSS 上传日志。

### 自动闭环的前置条件

1. GHCR 中已经存在目标镜像，且镜像的启动命令能够执行 `epc_insta_smoke.py`。真实 EPC Insta 程序及其依赖需要在 `image/Dockerfile`/`image/requirements.txt` 中加入，或由镜像入口自行提供。
2. E-HPC 登录节点可以执行 `sbatch`，并且 `--remote-dir`（默认 `/shared/epc-insta-test`）可写。触发器默认会把 `run_epc_job.sh` 同步到该目录；使用 `--wait` 时还需要 Slurm accounting 的 `sacct` 可用。
3. 计算节点已安装 Docker，并允许 Slurm 用户运行 Docker；节点可以访问 `ghcr.io`。私有 GHCR 使用节点预置的 Docker credential helper 或受保护的 `REGISTRY_*` 环境变量，公开 GHCR 镜像无需登录。
4. 计算节点已安装并配置 `ossutil`，或通过组织批准的方式获得 OSS 临时凭证。建议使用 RAM Role，不要把 AccessKey 写入触发命令、脚本或镜像。
5. `/shared` 已挂载到所有计算节点。默认开启 `--require-shared`，共享目录异常会使测试失败。

远程触发默认要求 `--epc-command`，避免真实 EPC Insta 测试被跳过却显示成功；仅验证基础运行环境时显式改用 `--baseline-only`。如果本机使用 `uv` 管理 Python，也可以把命令前缀替换为 `uv run --no-project python`。

触发器会打印 `run_id`、`job_id` 和 OSS 结果前缀。加上 `--wait` 会轮询 Slurm 并等待作业结束；不加则提交成功后立即返回。作业退出码为 `2` 表示镜像拉取或测试失败，`3` 表示 OSS 上传失败。

## 6. Slurm 多节点测试

把本目录上传到登录节点（例如 `/shared/epc-insta-test`），然后：

```bash
srun -N 2 -n 2 --ntasks-per-node=1 \
  python /shared/epc-insta-test/scripts/epc_insta_smoke.py --require-shared \
  --out /shared/results/${SLURM_JOB_ID}-${SLURM_PROCID}.json
```

更稳定的做法是提交 `sbatch` 作业：

```bash
#!/bin/bash
#SBATCH -N 2
#SBATCH --ntasks-per-node=1
#SBATCH -J epc-insta-smoke
#SBATCH -o /shared/results/%x-%j.out
set -euo pipefail
python /shared/epc-insta-test/scripts/epc_insta_smoke.py --require-shared \
  --out /shared/results/${SLURM_JOB_ID}-${SLURM_PROCID}.json
```

多节点通过条件：每个 task 的 JSON 中 `overall_pass=true`，节点名不同且所有节点均能读写 `/shared`。如果 EPC Insta 自带启动器，替换 `srun` 的命令部分，并保留退出码和结果归档。

## 7. GitHub Actions 自动推送 GHCR

工作流文件是 `.github/workflows/build-and-push-ghcr.yml`。它只在推送到 `main` 时运行；每次成功会推送 `latest` 和 Git SHA 两个标签。

工作流使用 GitHub 内置的 `GITHUB_TOKEN`，不需要额外的镜像推送 Secret。仓库必须允许 Actions 写入 packages，并在第一次发布后到 `Packages` 设置镜像为 Public，或者给 E-HPC 节点配置只读 `read:packages` PAT。配置细节见 `config/github-ghcr.example.md`。

镜像地址格式为：

```text
ghcr.io/<github-owner>/<github-repo>/epc-insta-test:latest
ghcr.io/<github-owner>/<github-repo>/epc-insta-test:sha-<short-sha>
```

推送代码后，可在 E-HPC 登录节点验证镜像：

```bash
docker pull ghcr.io/<github-owner>/<github-repo>/epc-insta-test:latest
docker run --rm --user 10001 \
  -v /shared/results:/work \
  ghcr.io/<github-owner>/<github-repo>/epc-insta-test:latest
```

生产测试建议把触发命令中的 `:latest` 换成不可变的 `:sha-<short-sha>`，这样 OSS 结果可准确对应代码版本；日常联调使用 `latest` 即可。GHCR 负责容器镜像分发，E-HPC 节点镜像仍负责操作系统、驱动和 Slurm 环境。

## 8. 结果判定和扩容顺序

* **P0 基线**：单节点、无 GPU、官方基础镜像，脚本所有基础检查通过。
* **P1 应用**：注入实际 EPC Insta 命令，命令退出码为 0，版本和依赖输出可追溯。
* **P2 共享存储**：2 个节点并行运行，所有节点能看到同一共享标记文件，结果写入 OSS/NAS。
* **P3 性能**：固定输入、节点数、镜像 digest 和实例规格，重复 3 次，记录 wall time、CPU/GPU 利用率、吞吐、失败重试。

每次实验保存：Git commit、Docker image digest、E-HPC 集群 ID、地域/可用区、实例规格、镜像 ID、Slurm job ID、输入数据版本和结果 JSON。

## 9. 常见故障定位

* 作业 `PENDING`：检查配额、节点健康、分区和实例库存。
* 节点能 SSH 但 `srun` 失败：检查 Slurm 时间同步、主机名解析、安全组 6817-6819 和 controller/agent 状态。
* 容器无法启动：检查 Docker/Containerd 服务、用户组权限、镜像架构（x86_64/ARM64）和磁盘空间。
* GPU 程序报驱动错误：核对 GPU 实例、宿主机驱动、容器 runtime 和 CUDA 版本矩阵。
* `/shared` 不一致：确认 NAS 挂载点、挂载选项、UID/GID 和每个节点的挂载路径完全一致。
* 拉包失败：不要把凭据放入镜像；使用 RAM 角色、私有镜像仓库登录令牌或预热镜像。

## 10. 上线前检查清单

- [ ] 控制台中确认 E-HPC Instant、EPC Insta、ARC 的准确产品名和地域可用性。
- [ ] 确认实例、GPU、NAS、OSS、vCPU 和公网带宽配额。
- [ ] 完成 RAM 最小权限、VPC/安全组、密钥对和审计日志配置。
- [ ] 官方基础镜像单节点通过。
- [ ] ARC 镜像（若确有）单节点通过并记录镜像 ID/版本。
- [ ] EPC Insta 版本、依赖、许可证和数据集版本固定。
- [ ] 2 节点 Slurm smoke test 通过，结果已写入 OSS/NAS。
- [ ] SSD FIO 测试使用专用挂载目录，结果已保存并与实例规格关联。
- [ ] GitHub Actions 已推送 GHCR 镜像，未在日志中暴露凭据。
- [ ] 完成成本预算、自动伸缩/关机策略和失败告警。


