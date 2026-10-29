# 旅游规划器：Render＋Neon 部署版

这是用于上传 GitHub 的独立部署副本。旅行算法、交通/酒店查询和 v17 页面保持原版。
原本电脑上的 TravelPlanner 和 .venv 保留用于本机运行，不用用这个副本覆盖。

## 1. 上传 GitHub

解压后进入 TravelPlanner 文件夹。将里面的 config、travel、deploy 和同层文件上传到仓库根目录。
GitHub 的 Code 页点 Add file（或＋）→ Upload files，拖入文件和文件夹，提交。
仓库根目录必须直接看到 Dockerfile 和 manage.py；不要只上传 ZIP，也不要再嵌套一层 TravelPlanner。
保持仓库 Private。不要上传 .venv、db.sqlite3、ticket_results、.env 或真实密钥。

## 2. Render 创建服务

- 仓库：你刚创建的 travel-planner
- Name：travel-planner（若重名则使用可用名称）
- Language：Docker。Python 应用仍运行 Python，Docker 负责一起安装 Node.js 和途牛 CLI。
- Branch：main
- Region：Singapore
- Root Directory：留空（前提是 Dockerfile、manage.py 在仓库根目录）
- Dockerfile Path：默认 ./Dockerfile
- Compute：Free / $0 per month
- Docker Command：留空，使用 Dockerfile 自带启动程序
- Health Check Path：/healthz/

切换 Docker 后不再填写 Python 模式下的 Build Command 和 Start Command。

## 3. Render 环境变量（Environment Variables）

必填：

|名称|值|
|---|---|
|DATABASE_URL|Neon Connect 中复制的完整 postgresql:// 连接串，含真实密码和 sslmode 参数；不带 psql、引号或换行|
|DJANGO_SECRET_KEY|独立的新随机字符串，至少50字符；不能使用代码中的开发密钥|
|TRAVEL_AMAP_KEY|你已有的高德 Web服务 Key|
|TUNIU_API_KEY|你已有的途牛 API Key|
|TUNIU_AUTH_TYPE|apiKey|

Render 会自动设置 RENDER_EXTERNAL_HOSTNAME、PORT，不需要自己猜网址填写。
本机可用 `python -c "import secrets; print(secrets.token_hex(32))"` 生成 DJANGO_SECRET_KEY，
只将结果填写到 Render，不要提交到 GitHub。

可选管理员：若需要维护景点，在首次部署时额外填写 BOOTSTRAP_ADMIN_USERNAME、
BOOTSTRAP_ADMIN_PASSWORD（至少12位且不能是常见密码）、BOOTSTRAP_ADMIN_EMAIL。
程序只会创建不存在的账号，不会重设已有账号密码。创建成功后在后台删除这三个环境变量。
公众使用旅行功能不需要管理员账号。

## 4. 启动与数据库

首次启动会检查生产设置、自动 migrate 创建 Neon 数据表；数据库没有景点时导入434条现有景点、429条匹配的高德资料。
这些是历史资料，不表示今天的实时票价或开放时间。平台实时查询仍按原逻辑运行。
不会导入本地账号、登录会话、个人旅行方案、报价缓存或历史票价下载文件。
不会清空已有 Neon 景点。以后发布代码也不会覆盖已维护的景点。
数据库使用 SSL、连接健康检查，并关闭不适合连接池的服务端游标。

## 5. 上线验收

等待 Render 显示 Live，打开它分配的 HTTPS 网址，在 /journey/ 分别测试：
景点搜索、去回程交通、酒店、票价、每日路线链接、照片、预算、方案导出。
再用另一浏览器打开，确认两个人的方案互不混用。
健康检查成功只说明网站和数据库能连通，不代表高德/途牛接口一定返回数据。
如高德 Key 设置了 IP 白名单，需使用 Render 后台显示的 Outbound IP 配置，而不是本机 IP。

## 6. 免费资源与限制

Render 空闲会休眠，下次访问需唤醒；免费资源和外部 API 额度有限。
现有代码按单实例、单进程运行，耗时查询分时处理；默认每浏览器每小时最多3次新建/重查类操作，
全站每小时15次，进程重启会重置计数。这只是演示站保护，不是 API 账单硬上限。
人数多时会等待或提示稍后重试；需要通过实际查询评估速度与内存。
云端音乐与手工照片仍保存在各访客自己的浏览器，不会自动带入你电脑上的音乐。

## 验证范围

发布前运行现有功能测试、静态文件收集、生产设置检查、初始数据导入与连接串解析检查。
本地验证不代表已经在用户 Render、Neon 和真实高德/途牛密钥上成功运行。
真正上线后仍须完成上述验收。
