# 凭证和权限放置说明

## GitHub Actions 推送 ACR

在 GitHub 仓库的 `Settings > Secrets and variables > Actions` 添加以下 Repository secrets：

| 名称 | 是否敏感 | 填写内容 |
|---|---|---|
| `ACR_REGISTRY` | 否 | ACR 登录域名，例如 `registry.cn-hangzhou.aliyuncs.com`，不要带 `https://` |
| `ACR_NAMESPACE` | 否 | ACR 控制台创建的命名空间，例如 `my-team` |
| `ACR_USERNAME` | 是 | ACR 控制台的访问凭证用户名，或专用 RAM 推送账号 |
| `ACR_PASSWORD` | 是 | ACR 访问凭证密码/令牌；不要填写阿里云主账号密码 |

ACR 侧需要先创建目标命名空间和仓库，并给推送账号授权目标仓库的拉取和推送权限。推送账号不需要管理 VPC、E-HPC、OSS 或 NAS。

## E-HPC 节点拉取 ACR

GitHub Actions 的推送凭证只用于 GitHub Runner，不能自动让 E-HPC 节点获得拉取权限。E-HPC 节点需要单独配置以下任一方式：

1. 推荐：给 E-HPC 节点绑定 RAM 角色，通过阿里云认可的临时凭证或节点集成机制访问 ACR。
2. 测试环境：在登录节点执行 `docker login`，使用 ACR 的拉取账号；不要把密码写入 Git、cloud-init 或作业脚本。
3. 私有网络：为 ACR 配置 VPC 访问、域名解析和安全组，让节点能够访问 ACR 私网端点。

如果使用节点本地登录，验证命令如下：

```bash
docker login <ACR_REGISTRY>
docker pull <ACR_REGISTRY>/<ACR_NAMESPACE>/epc-insta-test:<tag>
```

## SSH 密钥

SSH 私钥只保存在管理员电脑，用于登录 E-HPC 登录节点和执行 `scp`。它不应该放进 GitHub Secrets、ACR、OSS、Dockerfile 或 cloud-init。GitHub Actions 不需要 SSH 私钥就能构建并推送 ACR 镜像。

## 不要提交到仓库的内容

不要提交以下内容：

* 阿里云主账号 AccessKey/Secret
* ACR 密码或长期访问令牌
* E-HPC SSH 私钥
* NAS、OSS、ACR 的临时凭证
* 含真实密码的 `.env` 文件

仓库中的 `cluster-parameters.example.yaml` 只能填写资源 ID 和非敏感参数；真实凭证应使用 RAM、GitHub Secrets 或节点运行时凭证。
