# GitHub 到 ACR 配置清单

在 GitHub 仓库的 `Settings > Secrets and variables > Actions > New repository secret` 添加：

| Secret | 示例值 | 说明 |
|---|---|---|
| `ACR_REGISTRY` | `registry.cn-hangzhou.aliyuncs.com` | ACR 登录域名，不带协议头 |
| `ACR_NAMESPACE` | `my-team` | ACR 命名空间 |
| `ACR_USERNAME` | `github-pusher` | 专用 RAM/ACR 推送账号 |
| `ACR_PASSWORD` | `***` | 访问凭证；不要写进仓库 |

建议为 GitHub 单独创建推送账号，并只授予一个目标仓库的 `Pull` 和 `Push` 权限。不要使用阿里云主账号 AccessKey。若企业已有 GitHub OIDC 到阿里云 RAM 的标准接入，优先采用 OIDC 临时凭证替代长期密码。

工作流文件：`.github/workflows/build-and-push-acr.yml`

触发条件：

* push 到 `main`
* 推送形如 `v0.1.0` 的 Git tag
* GitHub Actions 页面手动触发

镜像地址格式：

```text
<ACR_REGISTRY>/<ACR_NAMESPACE>/epc-insta-test:<tag>
```
