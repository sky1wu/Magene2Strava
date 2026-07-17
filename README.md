# Magene2Strava

把顽鹿运动中的 FIT 骑行记录安全同步到 Strava 的本地网页工具。网页服务仅监听本机地址，现有授权文件不会发送给其他服务。

## 启动

```powershell
python -m pip install -r requirements.txt
python web_app.py --open
```

浏览器会打开 `http://127.0.0.1:8848`。如未自动打开，手动访问该地址即可。

## 使用流程

1. 点击“刷新数据”读取顽鹿活动与训练指标。
2. 点击“开始同步”，设置单次最大上传数量。
3. 可先运行“仅检查计划”，确认队列后再同步。
4. 同步日志会在页面内实时更新；重复活动会自动跳过。

## 授权维护

顽鹿授权过期时，工具会使用现有 HAR 登录信息刷新令牌。Strava Web 会话过期时运行：

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
- `POST /api/jobs`：启动同步预检或正式同步
- `GET /api/jobs/{id}`：读取任务进度与日志
