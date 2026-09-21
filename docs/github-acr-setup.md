# GitHub Actions 自动构建并推送 ACR

本仓库已包含 `.github/workflows/build-and-push-acr.yml`。它在 `main` 分支、版本 tag 或手动触发时构建 `image/Dockerfile`，并将镜像推送到阿里云 ACR。

工作流推送成功后，会在带有 `self-hosted, linux, e-hpc` 标签的 E-HPC runner 上自动拉取同一个镜像，执行 SSD 和 OSS 测试，并将 JSON 结果上传为 GitHub Actions artifact。因此不需要再手动输入测试命令。

## 1. ACR 侧准备

在阿里云 ACR 中创建私有命名空间和仓库：

```text
命名空间：my-team
仓库：epc-insta-test
```

创建专用推送账号并授权目标仓库的 Pull 和 Push 权限。使用 ACR 控制台提供的访问凭证，不要使用阿里云主账号密码或主账号 AccessKey。

ACR 登录域名从控制台复制，示例：

```text
registry.cn-hangzhou.aliyuncs.com
```

如果 ACR 只允许 VPC 私网访问，GitHub 托管 Runner 无法访问它。此时需要临时开放公网访问，或在同一 VPC 部署 GitHub self-hosted runner。

## 2. 在 E-HPC 节点安装 self-hosted runner

测试 job 需要在能看到 ESSD `/data`、能访问 ACR/OSS、并且有 Docker 权限的固定测试节点运行。建议使用专用的 `github-runner` Linux 用户，不要让 runner 运行在生产节点。

在 GitHub 仓库进入 `Settings -> Actions -> Runners -> New self-hosted runner`，选择 Linux x64，复制 GitHub 页面生成的下载和注册命令，在 E-HPC 测试节点执行。注册时增加自定义标签 `e-hpc`；GitHub 默认会提供 `self-hosted`、`linux` 和架构标签。

确认 runner 服务在线后，测试节点应满足：

```bash
docker version
test -d /data
id
```

将 runner 用户加入 Docker 用户组或按你们的安全规范授予 Docker 权限。若节点重启后仍要自动测试，按 GitHub 页面提供的命令安装 runner service。

如果 ACR 仅开放 VPC 私网，测试 runner 必须在该 VPC 内；构建 job 也应改为使用 VPC 内 runner，或为 GitHub hosted runner 临时开放 ACR 公网访问。

## 3. GitHub Secrets

进入：

`Repository -> Settings -> Secrets and variables -> Actions -> New repository secret`

创建以下 4 个 Repository secrets：

| 名称 | 示例值 | 是否敏感 |
|---|---|---|
| `ACR_REGISTRY` | `registry.cn-hangzhou.aliyuncs.com` | 否 |
| `ACR_NAMESPACE` | `my-team` | 否 |
| `ACR_USERNAME` | `github-pusher` | 是 |
| `ACR_PASSWORD` | ACR 访问凭证/令牌 | 是 |
| `OSS_ACCESS_KEY_ID` | 仅测试 Bucket 的 RAM 账号 ID | 是 |
| `OSS_ACCESS_KEY_SECRET` | 上述 RAM 账号的 Secret | 是 |
| `OSS_SESSION_TOKEN` | 使用 STS/RAM 临时凭证时填写；长期凭证可留空 | 是 |

不要在 workflow 中硬编码真实值。`GITHUB_TOKEN` 由 GitHub 自动提供，不需要手工创建；本 workflow 也不需要 SSH 私钥。

在 `Settings > Secrets and variables > Actions > Variables` 添加以下普通 Variables：

| Variable | 示例值 | 说明 |
|---|---|---|
| `OSS_BUCKET` | `hpc-test-bucket` | 测试 Bucket 名称，不带 `oss://` |
| `OSS_PREFIX` | `epc-insta/github` | 测试对象前缀 |
| `OSS_REGION` | `cn-hangzhou` | OSS 地域 ID |
| `EPC_SSD_DIR` | `/data` | self-hosted runner 上 ESSD 挂载目录 |
| `SSD_TEST_SIZE` | `1G` | FIO 文件大小 |
| `SSD_TEST_RUNTIME` | `30` | 每个 FIO profile 的秒数 |

如果 E-HPC runner 使用 RAM 角色访问 OSS，可以删除 workflow 中的 OSS 密钥 Secrets 和对应的 `-e` 参数，保留 `OSS_REGION`，让 ossutil 使用节点的临时身份。

## 4. 触发构建

在仓库根目录执行：

```powershell
git add .
git commit -m "Add E-HPC ACR build and OSS performance test"
git push origin main
```

也可以推送版本 tag：

```powershell
git tag v0.1.0
git push origin v0.1.0
```

工作流会生成以下标签：

```text
main
<git-sha>
v0.1.0
latest                  # 仅默认分支
```

镜像地址：

```text
<ACR_REGISTRY>/<ACR_NAMESPACE>/epc-insta-test:<tag>
```

## 5. E-HPC 节点拉取

GitHub 推送凭证不等于 E-HPC 拉取凭证。E-HPC 节点需要绑定 RAM 角色，或者单独执行：

```bash
docker login <ACR_REGISTRY>
docker pull <ACR_REGISTRY>/<ACR_NAMESPACE>/epc-insta-test:<tag>
```

生产环境建议给节点绑定可拉取 ACR 的 RAM 角色，不要把长期密码写入 cloud-init、Slurm 作业脚本或 Git 仓库。

## 6. 工作流失败排查

* `unauthorized`：ACR 用户名/密码错误，或没有目标仓库 Push 权限。
* `repository does not exist`：命名空间、仓库名或地域登录域名错误。
* `timeout`：GitHub Runner 无法访问 ACR，检查 ACR 公网访问或使用 VPC 内 self-hosted runner。
* 构建找不到脚本：必须从仓库根目录构建，Dockerfile 中的 context 是 `.`。
* E-HPC 拉取失败：节点到 ACR 的 DNS、路由、安全组和拉取权限需要单独检查。
* 测试 job 没有开始：检查 E-HPC 节点的 GitHub runner 是否在线，并且标签包含 `self-hosted`、`linux`、`e-hpc`。
* OSS 测试失败：检查 OSS RAM 账号是否拥有目标 Bucket 的对象读、写、删权限，以及 `OSS_BUCKET`/`OSS_REGION` 是否正确。
