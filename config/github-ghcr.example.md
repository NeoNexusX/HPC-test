# GitHub Actions + GHCR 配置清单

工作流文件是 `.github/workflows/build-and-push-ghcr.yml`。推送到 `main` 后，GitHub Actions 会自动构建并推送：

```text
ghcr.io/<github-owner>/<github-repo>/epc-insta-test:latest
ghcr.io/<github-owner>/<github-repo>/epc-insta-test:sha-<short-sha>
```

工作流使用内置的 `GITHUB_TOKEN`，不需要配置额外的镜像推送 Secret。仓库的 Actions 设置需要允许 workflow 写入 packages：`Settings > Actions > General > Workflow permissions > Read and write permissions`。YAML 本身也声明了 `packages: write`。

## 镜像可见性

第一次发布后，在 GitHub 仓库的 `Packages` 中找到 `epc-insta-test`：

* **Public**：E-HPC 节点直接 `docker pull`，不需要登录，配置最简单。
* **Private**：在 E-HPC 节点配置一个只读 GitHub fine-grained PAT，至少授予 `read:packages`，然后通过节点 credential helper 或受保护环境变量提供：

```bash
export REGISTRY=ghcr.io
export REGISTRY_USERNAME=<github-user>
export REGISTRY_PASSWORD=<read-packages-token>
```

不要把 PAT 写入仓库、Dockerfile、Slurm 参数或 OSS。生产环境优先使用节点上的 Docker credential helper 或 Secret 管理服务。
