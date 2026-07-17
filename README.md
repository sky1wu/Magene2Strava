# Magene2Strava

把顽鹿运动中的 FIT 骑行记录安全同步到 Strava 的本地网页工具。网页服务仅监听本机地址，现有授权文件不会发送给其他服务。

## 启动

```powershell
python -m pip install -r requirements.txt
python web_app.py --open
```

浏览器会打开 `http://127.0.0.1:8848`。如未自动打开，手动访问该地址即可。

## Docker

镜像发布在 GHCR：`ghcr.io/sky1wu/magene2strava:latest`。

```powershell
New-Item -ItemType Directory -Force magene2strava-data
docker pull ghcr.io/sky1wu/magene2strava:latest
docker run --rm --name magene2strava `
  -p 127.0.0.1:8848:8848 `
  -v "${PWD}/magene2strava-data:/data" `
  ghcr.io/sky1wu/magene2strava:latest
```

打开 `http://127.0.0.1:8848`。`/data` 用于持久化 FIT 文件、授权、同步状态和页面缓存；不要把端口直接暴露到公网。

如需沿用本机授权，停止容器后将以下文件复制到 `magene2strava-data`：

- `.onelap_token.json`
- `.onelap_auth.json`（账号登录摘要与 token）
- `onelap_auth.har`（仅旧版 HAR 授权需要）
- `.strava_web_session.json`
- `.strava_auth.json`（仅 Strava API 模式需要）

镜像支持 `linux/amd64` 与 `linux/arm64`。每次推送到 `main` 都会自动发布 `latest` 和对应的 `sha-*` 标签；推送 `v*` 标签时还会发布版本号标签。

### Docker Compose

项目已提供 `compose.yaml`，默认在 Docker 主机的所有网卡监听 `8848` 端口，并将数据保存在当前目录的 `magene2strava-data`：

```powershell
docker compose up -d
docker compose logs -f
```

同机访问 `http://127.0.0.1:8848`；Docker 运行在 NAS、服务器或远程开发环境时，访问 `http://<Docker主机IP>:8848`。请勿将该端口直接暴露到公网。

停止服务：

```powershell
docker compose down
```

如需修改宿主机端口，请修改 `compose.yaml` 中端口映射左侧的 `8848`：

```yaml
ports:
  - "18848:8848"
```

## 使用流程

1. 点击“刷新数据”读取顽鹿活动与训练指标。
2. 点击“开始同步”，设置单次最大上传数量。
3. 可先运行“仅检查计划”，确认队列后再同步。
4. 同步日志会在页面内实时更新；重复活动会自动跳过。

## 授权维护

授权可直接在网页顶部的“连接状态”中维护：

- 顽鹿运动：直接填写账号和密码。原始密码仅用于本次登录，落盘保存的是账号、MD5 登录摘要、token 和 refresh token；过期后会先刷新 token，必要时自动重新登录。
- Strava：默认使用 Web 会话，可粘贴已登录浏览器请求中的 `Cookie` 请求头，或导入 Strava HAR。
- Strava API OAuth：在 Strava 授权弹窗的高级选项中填写 Client ID 和 Client Secret；仅作为 Web 会话的备用模式。

顽鹿登录摘要与 Strava Cookie 都属于可用凭据。不要把授权文件、HAR、Cookie 或整个数据目录分享给他人；通过 NAS 或远程服务器使用时，建议在反向代理中启用 HTTPS。

Cookie 获取方法：登录 Strava 后打开浏览器开发者工具，在“网络”中刷新页面，选择任一 `strava.com` 请求并复制 Request Headers 中的 `Cookie`。

也可通过命令行打开交互式浏览器并保存 Strava Web 会话：

```powershell
python sync_to_strava.py --web-login
```

如改用官方 Strava API：

```powershell
python sync_to_strava.py --authorize
python sync_to_strava.py --strava-mode api --dry-run
```

## 开发与验证

前端资源位于 `web/`，本地 API 与后台任务位于 `web_app.py`。运行测试：

```powershell
python -m unittest discover -s tests -v
node --check web/app.js
```

服务提供以下本地接口：

- `GET /api/health`：健康检查
- `GET /api/dashboard`：汇总、连接状态与活动数据
- `POST /api/refresh`：刷新顽鹿数据缓存
- `POST /api/auth/onelap/login`：使用账号密码登录顽鹿
- `POST /api/auth/onelap/har`：导入旧版顽鹿登录 HAR（兼容后备）
- `POST /api/auth/strava/web-session`：导入 Strava Cookie 请求头
- `POST /api/auth/strava/har`：从 HAR 导入 Strava Web 会话
- `POST /api/auth/strava/start`：发起备用的 Strava API OAuth
- `POST /api/jobs`：启动同步预检或正式同步
- `GET /api/jobs/{id}`：读取任务进度与日志
