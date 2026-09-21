# GitHub Actions 自动构建并推送 ACR

本仓库已包含 `.github/workflows/build-and-push-acr.yml`。它在 `main` 分支、版本 tag 或手动触发时构建 `image/Dockerfile`，并将镜像推送到阿里云 ACR。

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

## 2. GitHub Secrets

进入：

`Repository -> Settings -> Secrets and variables -> Actions -> New repository secret`

创建以下 4 个 Repository secrets：

| 名称 | 示例值 | 是否敏感 |
|---|---|---|
| `ACR_REGISTRY` | `registry.cn-hangzhou.aliyuncs.com` | 否 |
| `ACR_NAMESPACE` | `my-team` | 否 |
| `ACR_USERNAME` | `github-pusher` | 是 |
| `ACR_PASSWORD` | ACR 访问凭证/令牌 | 是 |

不要在 workflow 中硬编码真实值。`GITHUB_TOKEN` 由 GitHub 自动提供，不需要手工创建；本 workflow 也不需要 SSH 私钥。

## 3. 触发构建

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

## 4. E-HPC 节点拉取

GitHub 推送凭证不等于 E-HPC 拉取凭证。E-HPC 节点需要绑定 RAM 角色，或者单独执行：

```bash
docker login <ACR_REGISTRY>
docker pull <ACR_REGISTRY>/<ACR_NAMESPACE>/epc-insta-test:<tag>
```

生产环境建议给节点绑定可拉取 ACR 的 RAM 角色，不要把长期密码写入 cloud-init、Slurm 作业脚本或 Git 仓库。

## 5. 工作流失败排查

* `unauthorized`：ACR 用户名/密码错误，或没有目标仓库 Push 权限。
* `repository does not exist`：命名空间、仓库名或地域登录域名错误。
* `timeout`：GitHub Runner 无法访问 ACR，检查 ACR 公网访问或使用 VPC 内 self-hosted runner。
* 构建找不到脚本：必须从仓库根目录构建，Dockerfile 中的 context 是 `.`。
* E-HPC 拉取失败：节点到 ACR 的 DNS、路由、安全组和拉取权限需要单独检查。
