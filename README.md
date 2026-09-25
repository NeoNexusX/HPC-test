# E-HPC INSTANT MassFlow UMAP

本地填好配置后，通过 HTTPS OpenAPI 向 E-HPC INSTANT 提交一个容器作业。容器从 OSS 下载一个 MassFlow MSI Zarr 数据集，用 MassFlow 的 `plot_umap_image` 做 3 维 UMAP 降维，把结果（`analysis/umap` 数组、`umap_image.jpg`）和可读的 `run.log` 上传回 OSS。算法与原来 FC 函数（`scripts/index.py`）一致，不需要 SSH、Slurm、登录节点或 NAS。

```text
① 代码 → 镜像（GitHub Actions，push 自动触发）
   git push → 单元测试（含真实 UMAP）→ 构建镜像（pip 安装 massflow）
            → 镜像内用 MassFlow 真实跑一次 UMAP（合成数据集）→ 推送到 ACR 公网地址
              crpi-y6a776c9l4k3agmh.cn-hongkong.personal.cr.aliyuncs.com/kawaru/sxo:{latest, sha-<commit>}

② 镜像 → 作业（本地执行 submit_instant.py）
   读取 config/instant.local.json（数据集可用 --dataset 临时替换）
   → STS AssumeRole（临时凭证只能列举/读取该数据集，只能写本次 run 的 OSS 前缀）
   → INSTANT CreateJob，请求里包含：
       Container.Image          = 配置里的 image（ACR 专有网络地址）
       ImageRegistryOptions     = ACR_PULL_USERNAME / ACR_PULL_PASSWORD
       EnvironmentVars          = OSS STS 临时凭证
   → INSTANT 在你的香港 VPC 里用这组账号密码拉取镜像
   → 列举 → 下载（跳过 ion_image/intensity 数据块）→ UMAP → 结果与 run.log 上传 OSS
```

ACR 和 INSTANT 之间没有任何控制台层面的“绑定”，唯一的连接就是每次 `CreateJob` 请求里写的镜像地址和拉取凭证。Actions 只负责把镜像推上 ACR，不读取本地配置，也不会提交作业。

### 配置放在哪里

| 配置 | 位置 | 原因 |
|---|---|---|
| ACR 镜像地址（公网）、命名空间、仓库名 | 仓库内 [.github/workflows/build-and-push-acr.yml](.github/workflows/build-and-push-acr.yml) 顶部 `env` | 不是秘密，Actions 构建推送需要 |
| ACR 登录名、密码 | GitHub **Secrets**：`ACR_USERNAME`、`ACR_PASSWORD` | 密码不能进仓库 |
| 打进镜像的 MassFlow 版本 | [image/requirements.txt](image/requirements.txt) 里的 `massflow==0.1.2`（PyPI） | 升级算法时改这一行，依赖由 pip 按 massflow 的声明安装 |
| INSTANT 作业配置（地域、镜像、vSwitch、安全组、RAM 角色、Bucket、数据集、UMAP 参数） | 本地 `config/instant.local.json`（被 `.gitignore` 排除） | 只有本地提交脚本用；本仓库是**公开**的，账号 ID、网络 ID 和 Bucket 名不宜公开 |
| 阿里云 AccessKey、ACR 拉取密码 | 本地 `.env`（被 `.gitignore` 排除，提交脚本自动加载） | 秘密，不能进仓库，也不会打进镜像 |

`config/instant.local.json` 本身不含密码。如果把仓库改成私有，也可以把它提交进去；公开仓库下建议保留在本地。

## 从 FC 迁移：对照

| FC 原实现（`scripts/index.py`） | INSTANT 实现 | 说明 |
|---|---|---|
| HTTP 触发器 `handler`，入参 `run_id` / `source_zarr_path` | `submit_instant.py` 调 `CreateJob`；数据集取配置 `umap.source_zarr_path` 或 `--dataset` | `run_id` 由提交脚本生成 |
| FC 函数角色注入的 STS 凭证 | 提交时 AssumeRole，会话策略收窄到“读该数据集 + 写本次 run 前缀”，经环境变量传入 | 本地长期 AccessKey 不进容器 |
| 列举、路径校验、跳过 `ion_image/intensity` 数据块 | 原样移植（`scripts/umap_job.py`） | 该数组是 `spectra/intensity` 的离子主序副本，UMAP 不读取；元数据仍下载 |
| 8 线程下载，条件 GET + 大小/ETag/CRC 校验，原子替换 | 原样移植 | 镜像编译了 `crcmod` C 扩展，CRC64 不拖慢下载 |
| 按 `/tmp` 与 NAS 剩余空间选落盘位置 | 只用作业系统盘（`resources.system_disk_gib`），空间不足时报错并提示调大 | INSTANT 每个作业一台新机器，不需要 NAS |
| 断点复用清单、900 秒时间预算 | 去掉 | 新机器上没有可复用的文件；运行时长不再受 FC 超时限制 |
| 抽样预算 `UMAP_FULL_MATRIX_MB` 等环境变量 | `umap.full_matrix_mib` / `sample_matrix_mib` / `max_fit_samples`，逻辑相同 | 峰值内存写入 `run.log`，便于核对 8 GiB 是否够用 |
| `plot_umap_image(save_matrix="zarr")` 写回源 Zarr，再增量上传变化文件 | 相同的快照差异；默认上传到本次 run 的 `zarr-delta/`，`write_back: true` 时写回源 Zarr | 测试数据默认只读，不会被改动 |
| 成功/失败回调后端，`failure_handler` 兜底 | `manifest.json`（最后上传，`overall_pass`）+ INSTANT 作业状态 + 容器退出码 | 作业在 VPC 内且无公网 IP，暂不回调，见“限制” |

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

镜像里记录了构建时的 commit，`run.log` 第一行的 `image_git_sha=` 会显示本次作业实际用的是哪次提交，第二行 `packages` 记录实际安装的 massflow、umap-learn、numba、numpy 等版本。


### 2. OSS Bucket 与数据集

直接使用已有的 `official-oss`（中国香港，私有）。

- 数据集是 MassFlow 预处理输出的 Zarr 目录，按原样上传到 OSS，例如当前测试数据：`official-oss/ehpc-benchmark/test_data/1e2de4_Mouse_Heart_MALDI_50_Negative.zarr/`（约 50 MiB、20346 像素 × 836 个 m/z）。
- 作业默认只**读取**数据集，结果写到 `official-oss/ehpc-benchmark/<run-id>/...`，测试数据不会被改动。
- 换数据集：把新的 `.zarr` 目录上传到 `ehpc-benchmark/` 下任意位置，提交时加 `--dataset`（见“运行”）。放在 `ehpc-benchmark/` 以外的前缀，需要同步放宽 3.1 的角色策略。

Bucket 概览里的“文件可以被公共访问”，意思是没有开启“阻止公共访问”。它不代表文件已经公开：Bucket 读写权限是私有，作业上传的对象也继承私有权限。如果这个 Bucket 不需要对外公开任何文件，建议在“权限控制 → 阻止公共访问”里开启。

### 3. RAM：一个角色 + 一个用户

需要两个身份，各管一件事：

| 身份 | 谁用 | 权限 |
|---|---|---|
| RAM **角色** `ehpc-oss-benchmark` | 作业容器，通过 STS 临时凭证使用 | 只能在 `official-oss/ehpc-benchmark/*` 下列举（ListObjects）、读取（Get）和写入（Put） |
| RAM **用户** `ehpc-submitter` | 你的本地电脑，AccessKey 写在 `.env` | 提交/查询 INSTANT 作业，以及扮演上面这个角色 |

每次提交时，脚本会用 RAM 用户申请一组 1 小时有效的临时凭证，并额外限制它只能列举和读取本次的数据集、只能写本次 run 的目录，然后交给容器使用。本地的长期 AccessKey 不会离开你的电脑。

主账号 AccessKey 不能用，因为主账号不能调用 AssumeRole。

在 [RAM 控制台](https://ram.console.aliyun.com/) 操作。账号 ID 在控制台右上角头像 → 账号中心里查看：

**3.1 创建容器用的角色**

如果已经有一个信任“当前云账号”、并带 OSS 权限的普通角色（例如 `AliyunOSSFullAccess`），可以跳过 3.1，直接把它的 ARN 填进 `oss_role_arn`。脚本申请临时凭证时附带的会话策略，会把权限缩小到“数据集前缀的 ListObjects/Get + `<bucket>/<oss_prefix>/<run-id>/*` 的 Put”，最终权限取两者的交集。服务关联角色（`AliyunServiceRoleFor...`）只能由云服务自己扮演，不能用在这里。

1. 权限管理 → 权限策略 → 创建权限策略 → 脚本编辑。
   - 粘贴 [config/oss-policy.example.json](config/oss-policy.example.json)，把 `<your-bucket>` 换成 `official-oss`。
   - 策略名填 `ehpc-oss-benchmark`。
2. 身份管理 → 角色 → 创建角色。
   - 信任主体类型选“云账号”，信任主体名称选“当前云账号”。
   - 角色名填 `ehpc-oss-benchmark`。
   - 创建后得到的信任策略与 [config/oss-role-trust-policy.example.json](config/oss-role-trust-policy.example.json) 相同。
3. 角色详情 → 权限管理 → 新增授权，选择自定义策略 `ehpc-oss-benchmark`。
4. 复制角色详情页的 **ARN**（形如 `acs:ram::<账号ID>:role/ehpc-oss-benchmark`），填到配置的 `oss_role_arn`。

**从 FIO/OSS 测速版本升级的注意**：旧版策略只有 Put/Get/Delete，没有 `oss:ListObjects`，作业会在列举数据集时报 `AccessDenied`。请把角色上的自定义策略更新为新的 [config/oss-policy.example.json](config/oss-policy.example.json)。如果角色挂的是 `AliyunOSSFullAccess`，不用改。

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
| `image` | ACR“专有网络”地址 + `/<命名空间>/<仓库名>:<标签>`。**推荐用 `:sha-<完整 commit SHA>`**：INSTANT 的执行节点可能复用本地缓存的同名标签，用 `:latest` 时，刚推送的新镜像不一定会被拉取，作业会悄悄跑旧代码。每次 push、CI 变绿后，把标签换成新的 commit SHA |
| `private_registry` | 私有仓库设为 `true`，需要 `.env` 里的 `ACR_PULL_*`；公开仓库设为 `false` |
| `vswitch_id` / `security_group_id` | 第 4 步创建的交换机和安全组 |
| `enable_external_ip` | 是否给作业分配公网 IP，默认 `false` |
| `oss_role_arn` | 第 3.1 步的角色 ARN |
| `resources.cores` / `memory_gib` / `system_disk_gib` | 作业规格。当前使用 `ecs.c9a.xlarge`：4 核 / 8 GiB / 100 GiB。系统盘要能装下下载量再加 1 GiB 余量；内存要覆盖 UMAP 峰值（测试数据约 1 GiB，见 `run.log` 的 `peak_rss`） |
| `resources.instance_types`（可选） | 最多 5 个实例规格，例如 `["ecs.g7.large"]`，用于固定 CPU 代际。按顺序尝试，全部售罄时作业不会自动改用其他规格。不写时 INSTANT 会按核数和内存自选规格，成功率最高 |
| `resources.fallback_any_type`（可选） | 默认 `false`。设为 `true` 时，如果 CreateJob 返回售罄错误（如 `RecommendEmpty.InstanceTypeSoldOut`），会去掉 `instance_types` 再提交一次，改由 INSTANT 自选规格。只对这一种错误重试，此时服务端没有创建作业，所以不会重复运行 |
| `umap.oss_*` | Bucket（`official-oss`）、地域、内网 Endpoint、结果归档前缀（`ehpc-benchmark`） |
| `umap.source_zarr_path` | 数据集的 OSS 前缀（不含 bucket，以 `.zarr` 结尾），当前为 `ehpc-benchmark/test_data/1e2de4_Mouse_Heart_MALDI_50_Negative.zarr`。`--dataset` 可临时覆盖 |
| `umap.write_back` | 默认 `false`：结果放到本次 run 的 `zarr-delta/`，数据集只读。`true`：与 FC 一样把 `analysis/umap` 写回源 Zarr，会话策略只额外放开 `<数据集>/analysis/*` 的写入 |
| `umap.work_dir` | 容器内下载和计算目录，默认 `/tmp/umap-work`（系统盘）。不要指向 `/dev` 或 `/` |
| `umap.skip_ion_image_chunks` | 默认 `true`，跳过 `ion_image/intensity` 数据块（测试数据可少下载约 40%）。`false` 为完整下载 |
| `umap.full_matrix_mib` / `sample_matrix_mib` / `max_fit_samples` | 同 FC：float32 矩阵不超过 `full_matrix_mib`（1024）时全量拟合；超过时抽样，样本数不超过 `max_fit_samples`（20000）且样本矩阵不超过 `sample_matrix_mib`（1024） |

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
# 1) 预览：校验配置，按官方 SDK 模型检查请求和 STS 会话策略；不需要凭证，不调用任何 API
python scripts/submit_instant.py --config config/instant.local.json --dry-run

# 2) 用配置里的数据集提交并等待结束（默认最多等 1800 秒）
python scripts/submit_instant.py --config config/instant.local.json --wait

# 3) 换一个数据集：OSS key 前缀或控制台复制的 oss:// 地址都可以，末尾的 / 可有可无
python scripts/submit_instant.py --dataset ehpc-benchmark/test_data/<其他数据集>.zarr --wait
python scripts/submit_instant.py --dataset oss://official-oss/ehpc-benchmark/test_data/<其他数据集>.zarr/ --wait

# 4) 只查询已有作业，不会重复提交
python scripts/submit_instant.py --config config/instant.local.json --job-id job-xxxxxxxx --wait-timeout 1800
```

提交成功后会打印 `run_id`、`job_id`、实际请求的实例规格（`instance_types`，回退后为 `any`）、数据集地址、结果地址和 OSS 归档前缀，并保存到本地 `work/<run-id>/submission.json`。作业失败时还会打印 INSTANT 给出的失败原因（`StatusReason`）。

本地退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 作业成功；不带 `--wait` 时表示已提交 |
| 1 | 配置错误或 API 调用失败，会打印服务端错误码 |
| 2 | 作业以失败状态结束 |
| 4 | 本地等待超时，**云端作业不会被取消** |

注意事项：

- 如果 `CreateJob` 报错，作业仍可能已经创建。请先到控制台确认，再决定是否重新提交。
- STS 有效期取 `max(3600, wait-timeout + 900)` 秒，需要覆盖“排队 + 拉镜像 + 下载 + UMAP + 上传”的全过程。大数据集请调大 `--wait-timeout`。
- 调大 `--wait-timeout` 时，要同步调大角色的最大会话时间。

## 三、结果

```text
oss://<bucket>/<oss_prefix>/<run-id>/<attempt-id>/
  run.log            可读的逐行日志（见下）；失败时包含错误和 Python 调用栈
  result.json        全部数据：CPU 信息、数据集统计、各阶段耗时、抽样参数、峰值内存、结果文件列表
  umap_image.jpg     UMAP 前三维映射成 RGB 的空间图像
  massflow.log       MassFlow 自身的日志
  zarr-delta/        UMAP 新增到 Zarr 的文件（write_back=false 时），与源 Zarr 叠加即得到完整结果：
    analysis/umap/scaled_embedding/...   每个像素的 3 维嵌入（0～1 缩放）
    analysis/umap/coordinates/...        对应的 0 起始像素坐标
  oss-upload.json    每个归档文件的上传状态
  manifest.json      最后上传；overall_pass=true 表示 UMAP 成功且结果、日志全部上传
```

`write_back: true` 时，`analysis/umap/...` 直接写回 `<数据集>.zarr/analysis/umap/`（与 FC 行为相同），`zarr-delta/` 不再生成。

`run.log` 示例（本地用测试数据集跑出的真实数值；机器不同，耗时会不同）：

```text
... start run=<run-id> attempt=<attempt-id> host=... image_git_sha=...
... packages massflow=0.1.2 umap-learn=... pynndescent=... numba=... numpy=... scikit-learn=... zarr=...
... cpu model: <型号> (<厂商>, x86_64)
... dataset oss://official-oss/ehpc-benchmark/test_data/1e2de4_Mouse_Heart_MALDI_50_Negative.zarr/ objects=239 size=50.3MiB download=29.9MiB skipped_ion_image_chunks=70
... finish download ...s
... finish umap 10.1s
... umap pixels=20346 features=836 matrix=64.9MiB fit_samples=20346 sample_ratio=1.000000
... upload 6 new zarr files -> oss://official-oss/ehpc-benchmark/<run-id>/<attempt-id>/zarr-delta/
... umap_pass=True peak_rss=974.9MiB stages_seconds={'list': ..., 'download': ..., 'import': ..., 'umap': ..., 'upload': ...}
```

以上内容同时输出到容器 stdout，可以在 INSTANT 控制台的作业日志里看到。

关于 numba 编译：镜像构建时会用合成数据跑一次 UMAP，把 numba 编译缓存和 matplotlib 字体缓存打进镜像，`import` 阶段因此只剩加载缓存的时间（本地模拟由 11.2 秒降到 2.7 秒）。为了让 CI 构建机上生成的缓存在 INSTANT 主机上也能命中，镜像把 numba 的编译目标固定为 `x86-64-v3`（AVX2），因此要求实例 CPU 支持 AVX2（c9a 等当代规格都支持）。umap-learn 有一部分函数没有标记为可缓存，仍会在每个作业的 `umap` 阶段首次调用时编译，这部分约占测试数据 `umap` 阶段的大半，数据越大占比越小。

容器退出码：`0` 成功，`2` UMAP 流水线失败（列举、下载、计算或结果上传），`3` 日志/manifest 归档失败，`64` 启动配置或凭证错误。这些退出码不会触发 INSTANT 重试；被 OOM 杀掉等其他退出码会按 `RetryCount: 1` 在新机器上重跑一次。如果镜像拉取失败、容器被强制终止，或 OSS 完全不可达，OSS 上可能没有日志，这时请查看 INSTANT 控制台。

## 四、限制

- **没有回调后端。** FC 版本在结束时回调 `CLUSTERING_FC_CALLBACK_URL`。INSTANT 作业默认在 VPC 内、没有公网 IP，接入后端时二选一：
  - 后端直接调用 `CreateJob`（逻辑同 `submit_instant.py`），再轮询 `GetJob` 或读取 `manifest.json` 判断终态。不需要改网络，推荐。
  - 在容器里回调：需要 VPC 有 NAT 网关或后端在同一 VPC，并把回调 Token 作为环境变量传入。
- **UMAP 调用不能中途打断。** 与 FC 相同，native 的 UMAP 计算期间无法做协作式检查；内存不足时进程会被系统杀掉，只能从 INSTANT 控制台和缺失的 `manifest.json` 判断。数据集变大时先看 `run.log` 的 `peak_rss`，必要时调小 `sample_matrix_mib` 或换更大内存的规格。
- **不设最大源文件限制。** FC 的 `FC_MAX_FILE_SIZE_MB`（20 GiB）是因为 `/tmp` 和 NAS 有限；这里只检查系统盘剩余空间，不够时报 `ENOSPC` 并提示调大 `system_disk_gib`。
- STS 临时凭证通过容器环境变量传入：
  - 有权限查看作业配置的人能看到这些值。
  - 凭证已被会话策略限制在本次数据集和本次 run 的前缀内，并且会自动过期。
  - 本地 API 的 AccessKey 不会传进容器。

## 开发

MassFlow 要求 Python 3.12 及以上，完整测试需要 3.12 环境（本地提交脚本仍可在 3.9 上运行）：

```bash
uv venv --python 3.12 .venv312 && source .venv312/bin/activate
uv pip install -r requirements.txt -r image/requirements.txt
python -m unittest discover -s tests -v
```

- `tests/test_submit.py`：SDK 请求结构、参数分块、实例规格与售罄回退、STS 会话策略（数据集只读、只写本次 run）、`--dataset` 解析、凭证脱敏、终态等待。
- `tests/test_umap_job.py`：列举与路径校验、`ion_image` 数据块跳过、ETag 校验与部分文件清理、抽样预算、结果写到 `zarr-delta/` 或写回源 Zarr、失败日志与 OSS 错误脱敏、归档失败退出码。
- 其中 `MassFlowIntegrationTests` 用 MassFlow 写一个合成 MSI Zarr，经内存版 OSS 走完整的 `run()`，是真实的 UMAP 计算。CI 的单元测试和镜像冒烟测试（`REQUIRE_MASSFLOW=1`）都会运行它；没装 MassFlow 时自动跳过。

在本地用真实数据集跑同一个流程：

```bash
UMAP_TEST_ZARR=/path/to/1e2de4_Mouse_Heart_MALDI_50_Negative.zarr \
  python -m unittest discover -s tests -p test_umap_job.py -k test_real_umap
```

## 官方依据

- [INSTANT CreateJob](https://help.aliyun.com/zh/e-hpc/e-hpc-instant/developer-reference/api-ehpcinstant-2023-07-01-createjob)
- [INSTANT GetJob](https://help.aliyun.com/zh/e-hpc/e-hpc-instant/developer-reference/api-ehpcinstant-2023-07-01-getjob)
- [INSTANT 服务关联角色](https://help.aliyun.com/zh/e-hpc/e-hpc-instant/security-and-compliance/service-linked-role-of-e-hpc-instant-service)
- [STS AssumeRole](https://help.aliyun.com/zh/ram/developer-reference/api-sts-2015-04-01-assumerole)
- [ACR 推送/拉取镜像](https://help.aliyun.com/zh/acr/getting-started/use-a-container-registry-enterprise-edition-instance-to-push-and-pull-images)
