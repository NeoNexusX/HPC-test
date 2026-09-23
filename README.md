# E-HPC INSTANT 基础设施测试

本地填好配置后，通过 HTTPS OpenAPI 向 E-HPC INSTANT 提交一个容器作业。容器里依次做 CPU 信息采集、FIO 磁盘测试和 OSS 上传/下载测速，把可读结果写进 `run.log`，再连同完整 JSON 一起上传到 OSS。不需要 SSH、Slurm、登录节点或 NAS。

```text
① 代码 → 镜像（GitHub Actions，push 自动触发）
   git push → 单元测试 → 构建镜像 → 镜像内冒烟测试 → 推送到 ACR 公网地址
              crpi-y6a776c9l4k3agmh.cn-hongkong.personal.cr.aliyuncs.com/kawaru/sxo:{latest, sha-<commit>}

② 镜像 → 作业（本地执行 submit_instant.py）
   读取 config/instant.local.json
   → STS AssumeRole（临时凭证只允许写本次 run 的 OSS 前缀）
   → INSTANT CreateJob，请求里包含：
       Container.Image          = 配置里的 image（ACR 专有网络地址）
       ImageRegistryOptions     = ACR_PULL_USERNAME / ACR_PULL_PASSWORD
       EnvironmentVars          = OSS STS 临时凭证
   → INSTANT 在你的香港 VPC 里用这组账号密码拉取镜像
   → 运行 CPU / FIO / OSS 测试 → run.log 等结果上传 OSS
```

ACR 和 INSTANT 之间没有任何控制台层面的“绑定”，唯一的连接就是每次 `CreateJob` 请求里写的镜像地址和拉取凭证。Actions 只负责把镜像推上 ACR，不读取本地配置，也不会提交作业。

### 配置放在哪里

| 配置 | 位置 | 原因 |
|---|---|---|
| ACR 镜像地址（公网）、命名空间、仓库名 | 仓库内 [.github/workflows/build-and-push-acr.yml](.github/workflows/build-and-push-acr.yml) 顶部 `env` | 不是秘密，Actions 构建推送需要 |
| ACR 登录名、密码 | GitHub **Secrets**：`ACR_USERNAME`、`ACR_PASSWORD` | 密码不能进仓库 |
| INSTANT 作业配置（地域、镜像、vSwitch、安全组、RAM 角色、Bucket、测试参数） | 本地 `config/instant.local.json`（被 `.gitignore` 排除） | 只有本地提交脚本用；本仓库是**公开**的，账号 ID、网络 ID 和 Bucket 名不宜公开 |
| 阿里云 AccessKey、ACR 拉取密码 | 本地 `.env`（被 `.gitignore` 排除，提交脚本自动加载） | 秘密，不能进仓库，也不会打进镜像 |

`config/instant.local.json` 本身不含密码。如果把仓库改成私有，也可以把它提交进去；公开仓库下建议保留在本地。

## 需求对照

| 需求 | 实现 | 说明 |
|---|---|---|
| 本地配置后提交作业 | `scripts/submit_instant.py`，官方 `EhpcInstant/2023-07-01` SDK 调 `CreateJob` / `GetJob` | `--dry-run` 不需要凭证，可先检查请求 |
| OSS 上传、下载测速 | 单对象 PUT/GET，默认 1 GiB（`oss_size_mib: 1024`），记录耗时、MiB/s、SHA-256 校验，测完删除 | 单连接端到端吞吐，不是最大并发吞吐 |
| 本地 SSD 测试（官方方法） | 8 组 FIO 负载，参数与[阿里云官方本地盘测试命令](https://www.alibabacloud.com/help/en/ecs/user-guide/test-the-performance-of-block-storage-devices)逐项一致 | 见下方“限制”：INSTANT 下测到的是容器所在磁盘 |
| CPU 型号等 | `lscpu --json`：型号、厂商、架构、核数/线程、主频、L3、虚拟化；另记录 affinity 与 cgroup 限额 | 只采集信息，不做压力测试 |
| 结果输出到日志并上传 OSS | `run.log` 逐行写可读结果；`result.json`、FIO 原始 JSON、`manifest.json` 一并上传 | 见“结果” |
| push 自动构建并推送 ACR | `.github/workflows/build-and-push-acr.yml` | 所有分支 push、`v*` 标签、手动触发；PR 只构建不推送 |

## 一、需要你配置的内容（一次性）

### 1. ACR 镜像仓库 + GitHub Secrets

1. 在[容器镜像服务 ACR](https://cr.console.aliyun.com/) 选择地域。**必须与 INSTANT 作业、OSS 在同一地域**，例如 `cn-hongkong`。
2. 创建命名空间和镜像仓库：
   - 仓库类型选私有。
   - 代码源选**本地仓库**。“代码变更自动构建镜像”“海外机器构建”都不要勾选，构建交给 Actions。
3. 在 ACR“访问凭证”里设置固定密码。这个密码不是阿里云网页登录密码，也不是 AccessKey。
4. 如果仓库地址和当前不同，修改工作流顶部的 `ACR_REGISTRY`（公网域名）和 `ACR_IMAGE`（公网域名/命名空间/仓库名）。当前值为 `crpi-y6a776c9l4k3agmh.cn-hongkong.personal.cr.aliyuncs.com/kawaru/sxo`。
5. 在 GitHub 仓库 **Settings → Secrets and variables → Actions → New repository secret** 添加：

| Secret | 填什么 |
|---|---|
| `ACR_USERNAME` | ACR 仓库“操作指南”中 `docker login --username=` 后面的登录名（当前为 `NeoNexus`） |
| `ACR_PASSWORD` | 第 3 步设置的访问凭证密码 |

push 后到 GitHub **Actions** 页查看。成功后 ACR 里会出现以下标签：

- `latest`：仅默认分支（main），本地配置默认使用它，平时不用改配置。
- `sha-<完整 40 位 commit SHA>`：每次提交都有。需要固定到某次构建时，把配置里的 `:latest` 换成它。
- 分支名 / `v*` 标签名。

镜像里记录了构建时的 commit，`run.log` 第一行的 `image_git_sha=` 会显示本次测试实际用的是哪次提交。

### 2. OSS Bucket

直接使用已有的 `official-oss`（中国香港，私有）。测试只会写入 `official-oss/ehpc-benchmark/<run-id>/...` 这个前缀：1 GB 的测速对象测完会删除，只留下日志和结果文件。

Bucket 概览里的“文件可以被公共访问”，意思是没有开启“阻止公共访问”。它不代表文件已经公开：Bucket 读写权限是私有，测试上传的对象也继承私有权限。如果这个 Bucket 不需要对外公开任何文件，建议在“权限控制 → 阻止公共访问”里开启。

### 3. RAM：一个角色 + 一个用户

需要两个身份，各管一件事：

| 身份 | 谁用 | 权限 |
|---|---|---|
| RAM **角色** `ehpc-oss-benchmark` | 作业容器，通过 STS 临时凭证使用 | 只能对 `official-oss/ehpc-benchmark/*` 执行 Put/Get/Delete |
| RAM **用户** `ehpc-submitter` | 你的本地电脑，AccessKey 写在 `.env` | 提交/查询 INSTANT 作业，以及扮演上面这个角色 |

每次提交时，脚本会用 RAM 用户申请一组 1 小时有效的临时凭证，并额外限制它只能写本次 run 的目录，然后交给容器使用。本地的长期 AccessKey 不会离开你的电脑。

主账号 AccessKey 不能用，因为主账号不能调用 AssumeRole。

在 [RAM 控制台](https://ram.console.aliyun.com/) 操作。账号 ID 在控制台右上角头像 → 账号中心里查看：

**3.1 创建容器用的角色**

如果已经有一个信任“当前云账号”、并带 OSS 权限的普通角色（例如 `AliyunOSSFullAccess`），可以跳过 3.1，直接把它的 ARN 填进 `oss_role_arn`。脚本申请临时凭证时附带的会话策略，会把权限缩小到 `<bucket>/<oss_prefix>/<run-id>/*` 的 Put/Get/Delete，最终权限取两者的交集。服务关联角色（`AliyunServiceRoleFor...`）只能由云服务自己扮演，不能用在这里。

1. 权限管理 → 权限策略 → 创建权限策略 → 脚本编辑。
   - 粘贴 [config/oss-policy.example.json](config/oss-policy.example.json)，把 `<your-bucket>` 换成 `official-oss`。
   - 策略名填 `ehpc-oss-benchmark`。
2. 身份管理 → 角色 → 创建角色。
   - 信任主体类型选“云账号”，信任主体名称选“当前云账号”。
   - 角色名填 `ehpc-oss-benchmark`。
   - 创建后得到的信任策略与 [config/oss-role-trust-policy.example.json](config/oss-role-trust-policy.example.json) 相同。
3. 角色详情 → 权限管理 → 新增授权，选择自定义策略 `ehpc-oss-benchmark`。
4. 复制角色详情页的 **ARN**（形如 `acs:ram::<账号ID>:role/ehpc-oss-benchmark`），填到配置的 `oss_role_arn`。

角色的“最大会话时间”默认 3600 秒，够用。只有在把 `--wait-timeout` 设到 2700 秒以上时，才需要调大它。

**3.2 创建本地提交用的 RAM 用户**

这里必须建**用户**（身份管理 → 用户），不能建**角色**（身份管理 → 角色）。原因是：

- 只有用户才有 AccessKey，能写进 `.env` 给本地脚本用。
- 角色没有 AccessKey，只能被别的身份“扮演”。

1. 身份管理 → **用户** → 创建用户。
   - 登录名填 `ehpc-submitter`，访问方式勾选“使用永久 AccessKey 访问”（OpenAPI 调用）。
   - 创建后**立即保存** AccessKey ID 和 Secret（Secret 只显示这一次），填进 `.env`。
2. 用户列表中该用户 → 添加权限，二选一：
   - **省事**：选系统策略 `AliyunEHPCFullAccess` 和 `AliyunSTSAssumeRoleAccess`。
   - **最小权限**：粘贴 [config/submit-policy.example.json](config/submit-policy.example.json)，把 `<account-id>` 和 `<oss-benchmark-role>` 换成实际值，创建为自定义策略后授权。它只允许 `ehpc:CreateJob`、`ehpc:GetJob`，以及扮演 3.1 的那一个角色。

这个用户没有 OSS 权限，所以查看结果请用主账号登录 OSS 控制台。

### 4. E-HPC INSTANT 与香港 VPC

为什么必须有 VPC：

- INSTANT 每次运行作业都会临时开一台机器，这台机器要在 VPC 里挂网卡才能联网。官方把 VPC、交换机、安全组列为前提条件。
- 这台机器要通过**内网**拉取 ACR 镜像（`-vpc` 地址），并用内网访问 OSS（`-internal` 地址）。这两个内网地址只有香港的 VPC 能访问。
- 不用 VPC 就只能走公网：OSS 公网下载要收流量费，而且测到的是公网速度，失去了测试意义。

VPC、交换机、安全组本身都是免费的。

INSTANT 没有集群要创建或维护。它是按作业计费的无服务器计算：每次 `CreateJob` 时临时分配机器，跑完即释放。镜像、CPU、内存、磁盘、网络都写在每次的提交请求里，由提交脚本根据配置文件自动生成。

所以“HPC 的配置”只有下面几步：

1. **开通 INSTANT**（需要已完成实名认证）：在 E-HPC 控制台进入 E-HPC Instant 开通页，页面上点“创建服务关联角色”，同意协议并开通，等待 1～5 分钟。服务关联角色用于授权 INSTANT 在你的账号里创建机器、挂网卡，属于平台自身的权限，与上面的 RAM 角色无关。
2. **创建香港 VPC 和交换机**（官方把 VPC、交换机、安全组列为前提条件）：
   - [VPC 控制台](https://vpc.console.aliyun.com/) → 地域选“中国香港” → 创建专有网络，例如网段 `172.16.0.0/12`。
   - 同时创建交换机：选一个可用区，例如 `172.16.0.0/24`。
   - 如果香港已经有 VPC（例如默认 VPC），可以直接复用它的交换机。
   - 复制交换机 ID（`vsw-...`），填到配置的 `vswitch_id`。
3. **创建安全组**：[ECS 控制台](https://ecs.console.aliyun.com/) → 网络与安全 → 安全组 → 地域选中国香港 → 创建安全组。
   - 专有网络选上一步的 VPC，规则保持默认：入方向不用开端口，出方向默认放行。
   - 复制 `sg-...`，填到配置的 `security_group_id`。

作业默认不分配公网 IP，通过内网访问：

- 拉镜像：ACR“专有网络”地址 `crpi-…-vpc.cn-hongkong.personal.cr.aliyuncs.com`，只有**同地域 VPC** 能访问。
- 访问 OSS：`https://oss-cn-hongkong-internal.aliyuncs.com`。

所以 ACR、OSS、VPC、INSTANT 都必须在香港，内网流量免费。如果提交时报交换机所在可用区没有库存，就在另一个可用区再建一个交换机，把 `vswitch_id` 换成新的。

### 5. 本地环境

需要 Python 3.9 及以上版本（本地 3.9 和 CI 3.11 都已验证）。在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/instant.example.json config/instant.local.json   # 已被 .gitignore 排除
cp .env.example .env && chmod 600 .env                     # 已被 .gitignore 排除
```

**`config/instant.local.json`（非秘密参数）**：把所有 `<...>` 占位符都换掉。

| 字段 | 说明 |
|---|---|
| `region` | `cn-hongkong`，需要与 ACR、OSS、vSwitch 同地域 |
| `image` | ACR“专有网络”地址 + `/<命名空间>/<仓库名>:latest`，当前为 `crpi-y6a776c9l4k3agmh-vpc.cn-hongkong.personal.cr.aliyuncs.com/kawaru/sxo:latest` |
| `private_registry` | 私有仓库设为 `true`，需要 `.env` 里的 `ACR_PULL_*`；公开仓库设为 `false` |
| `vswitch_id` / `security_group_id` | 第 4 步创建的交换机和安全组 |
| `enable_external_ip` | 是否给作业分配公网 IP，默认 `false` |
| `oss_role_arn` | 第 3.1 步的角色 ARN |
| `resources.cores` / `memory_gib` / `system_disk_gib` | 作业规格，默认 2 核 / 4 GiB / 40 GiB。系统盘要能装下 FIO 文件和 2 倍 OSS 测试对象 |
| `resources.instance_types`（可选） | 最多 5 个实例规格，例如 `["ecs.g7.large"]`，用于固定 CPU 代际 |
| `benchmark.oss_*` | Bucket（`official-oss`）、地域、内网 Endpoint、归档前缀、测速对象大小（默认 1024 MiB） |
| `benchmark.disk_dir` | 容器内的 FIO 测试目录，默认 `/tmp/ehpc-benchmark`。不要指向 `/dev` 或 `/` |
| `benchmark.fio_size_mib` / `fio_runtime_seconds` | FIO 文件大小和每组运行时长。官方为每组 1000 秒，这里默认 30 秒 |

**`.env`（秘密）**：提交脚本每次运行都会自动读取，不需要手动 `export`。终端里已经 export 的同名变量优先。

```bash
ALIBABA_CLOUD_ACCESS_KEY_ID=<第 3.2 步 RAM 用户的 AccessKey ID>
ALIBABA_CLOUD_ACCESS_KEY_SECRET=<对应 Secret>
ACR_PULL_USERNAME=NeoNexus
ACR_PULL_PASSWORD=<ACR 访问凭证密码>
ALIBABA_CLOUD_ECS_METADATA_DISABLED=true
```

`.env` 和 `config/instant.local.json` 都已被 `.gitignore` 排除，也被 `.dockerignore` 排除，不会进入 Git 或镜像。

## 二、运行

```bash
# 1) 预览：校验配置，按官方 SDK 模型检查请求；不需要凭证，不调用任何 API
python scripts/submit_instant.py --config config/instant.local.json --dry-run

# 2) 提交并等待结束（默认最多等 1800 秒）
python scripts/submit_instant.py --config config/instant.local.json --wait

# 3) 只查询已有作业，不会重复提交
python scripts/submit_instant.py --config config/instant.local.json --job-id job-xxxxxxxx --wait-timeout 1800
```

提交成功后会打印 `run_id`、`job_id` 和 OSS 归档前缀，并保存到本地 `work/<run-id>/submission.json`。

本地退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 作业成功；不带 `--wait` 时表示已提交 |
| 1 | 配置错误或 API 调用失败，会打印服务端错误码 |
| 2 | 作业以失败状态结束 |
| 4 | 本地等待超时，**云端作业不会被取消** |

注意事项：

- 如果 `CreateJob` 报错，作业仍可能已经创建。请先到控制台确认，再决定是否重新提交。
- STS 有效期取 `max(3600, wait-timeout + 900)` 秒，需要覆盖“排队 + 拉镜像 + 测试 + 归档”的全过程。
- 调大 `--wait-timeout` 或 `fio_runtime_seconds` 时，要同步调大角色的最大会话时间。

## 三、结果

```text
oss://<bucket>/<oss_prefix>/<run-id>/<attempt-id>/
  run.log            可读的逐行结果（见下）
  result.json        全部原始数据：lscpu JSON、findmnt/lsblk、每组 FIO 指标、OSS 测速
  fio-prefill.json   FIO 预填充
  fio-<profile>.json 每组 FIO 的完整 JSON 输出
  oss-upload.json    每个文件的上传状态
  manifest.json      最后上传；overall_pass=true 表示测试全部通过且归档完整
```

`run.log` 格式如下（数值为占位）：

```text
... cpu model: <型号> (<厂商>, x86_64)
... cpu topology: cpus=2 sockets=1 cores/socket=1 threads/core=2 max_mhz=... l3=... hypervisor=KVM
... disk target=/tmp/ehpc-benchmark fstype=overlay source=overlay runtime=30s/profile physical_local_ssd_verified=false
... fio seqwrite          write     bs=128k iodepth=128 numjobs=1 bw=...MiB/s iops=... lat_mean=...us lat_p99=...us
... fio randread          randread  bs=4k   iodepth=32  numjobs=4 bw=...MiB/s iops=... lat_mean=...us lat_p99=...us
... oss upload: ...MiB/s (...s)
... oss download: ...MiB/s (...s)
... oss sha256_match=True cleanup=passed error=None
```

以上内容同时输出到容器 stdout，可以在 INSTANT 控制台的作业日志里看到。

容器退出码：`0` 全部通过，`2` 有测试失败，`3` 归档失败，`64` 启动配置或凭证错误。如果镜像拉取失败、容器被强制终止，或 OSS 完全不可达，OSS 上可能没有日志，这时请查看 INSTANT 控制台。

## 四、限制

- **INSTANT 不暴露物理本地 NVMe 盘。** `CreateJob` 的 `Resource.Disks` 目前只支持 `System`，挂载方式只支持 NAS/OSS。
  - FIO 实际测的是容器 `/tmp` 所在的文件系统，通常是系统云盘上的 overlay。结果会记录 `findmnt`/`lsblk`，并固定标记 `physical_local_ssd_verified=false`。
  - FIO 的块大小、队列深度和并发数与官方本地盘方法一致。但官方是裸盘、每组 1000 秒，这里是文件型、默认每组 30 秒，结果不能直接等同官方裸盘数据。
  - 如果必须测物理本地 SSD，需要换用带本地盘的 ECS 实例（如 i 系列）直接跑 FIO，或者先向阿里云确认 INSTANT 是否支持。
- OSS 数值是单连接端到端吞吐，包含 SDK 和本地文件读写开销。
- STS 临时凭证通过容器环境变量传入：
  - 有权限查看作业配置的人能看到这些值。
  - 凭证已被会话策略限制在本次 run 的前缀内，并且会自动过期。
  - 本地 API 的 AccessKey 不会传进容器。

## 开发

```bash
pip install -r requirements.txt -r image/requirements.txt
python -m unittest discover -s tests -v
```

离线测试覆盖以下内容：

- SDK 请求结构、参数分块、实例规格
- STS 会话策略范围、凭证脱敏
- 终态等待、超时不取消作业
- OSS 数据校验和清理、归档失败的退出码
- `run.log` 可读结果、`lscpu` 解析

CI 还会在构建出的镜像里实际运行 `lscpu` 和短时 FIO。真实的 INSTANT/ACR/OSS 连通性需要填好账号配置后首次联调才能验证。

## 官方依据

- [INSTANT CreateJob](https://help.aliyun.com/zh/e-hpc/e-hpc-instant/developer-reference/api-ehpcinstant-2023-07-01-createjob)
- [INSTANT GetJob](https://help.aliyun.com/zh/e-hpc/e-hpc-instant/developer-reference/api-ehpcinstant-2023-07-01-getjob)
- [INSTANT 服务关联角色](https://help.aliyun.com/zh/e-hpc/e-hpc-instant/security-and-compliance/service-linked-role-of-e-hpc-instant-service)
- [块存储/本地盘 FIO 测试方法](https://www.alibabacloud.com/help/en/ecs/user-guide/test-the-performance-of-block-storage-devices)
- [STS AssumeRole](https://help.aliyun.com/zh/ram/developer-reference/api-sts-2015-04-01-assumerole)
- [ACR 推送/拉取镜像](https://help.aliyun.com/zh/acr/getting-started/use-a-container-registry-enterprise-edition-instance-to-push-and-pull-images)
