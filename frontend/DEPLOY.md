# 重介密控系统 — 上线部署指南（中国大陆路线）

> 适用范围：本目录（纯静态前端，无构建步骤）。只部署前端，数据存浏览器 localStorage。
> 访问人群：中国大陆。因此必须走 **国内托管 + ICP 备案** 路线。

---

## 0. 路线总览（时间线）

```
第 1 天        注册域名 + 实名认证          （半小时）
第 1 天        购买轻量应用服务器            （十分钟，拿到备案服务码）
第 1~21 天     ICP 备案（管局审核 1~3 周）    （期间可临时预览）
备案通过后     部署静态文件 + 域名解析 + HTTPS（半天，可请 AI 助手协助执行）
```

## 1. 注册域名

| 事项 | 建议 |
| --- | --- |
| 注册商 | 腾讯云（DNSPod）或 阿里云（万网），二选一 |
| 后缀 | `.cn` 首年约 ¥29；`.com` 约 ¥60~80/年。两者备案流程相同 |
| 要求 | 注册人身份证实名认证（当天完成） |
| 注意 | 域名实名信息（个人姓名）需与后续备案人一致 |

## 2. 购买托管（推荐：轻量应用服务器）

**推荐方案 A：轻量应用服务器**（约 ¥99~120/年，2核2G）
- 腾讯云轻量 / 阿里云轻量均可，系统选 **Ubuntu 22.04**
- 优点：自带备案服务码；以后做前后端分离（见《前后端分离规划.xlsx》）可直接在同一台机器上跑后端
- 购买地域选离用户最近的大区（如华北、华东）

备选方案 B：对象存储（COS/OSS）+ CDN 静态托管，按量计费小流量每月几元，但备案要求相同、且以后跑后端仍需另购服务器。**本项目有后端规划，建议直接选方案 A。**

## 3. ICP 备案（免费，1~3 周）

1. 在服务器厂商控制台进入「备案」流程，填入备案服务码
2. 准备：本人身份证、手机号、常用居住地址、域名
3. 个人备案、网站性质选「其他」或「个人博客/信息展示」类非经营性用途
4. 网站名称避免含「系统、平台、官网」等敏感词（管局可能驳回，可写「密度分析与灰分监控」这类描述性名称）
5. 完成人脸核验 → 厂商初审（1~2 天）→ 管局终审（1~3 周）→ 短信/邮件通知备案号

**备案期间临时预览**：国内服务器未备案域名的 80/443 端口会被拦截，但可用非标准端口临时访问，如 `http://服务器IP:8080`（Nginx 配置见下文模板的第二段）。

## 4. 部署静态文件（备案通过后执行）

### 4.1 需要上传的内容

仅需以下文件，其余（`.idea/`、`AGENTS.md`、`DEPLOY.md`、`前后端分离规划.xlsx`）不上传：

```
index.html
css/style.css
js/*.js          （全部 .js 文件）
```

打包命令（在本目录执行，Windows PowerShell）：

```powershell
Compress-Archive -Path index.html, css, js -DestinationPath deploy.zip -Force
```

上传到服务器后解压到 `/var/www/mikong/`：

```bash
# 服务器上执行
sudo mkdir -p /var/www/mikong
# 上传 deploy.zip 后（scp 或厂商网页端上传）
cd /var/www/mikong && sudo unzip ~/deploy.zip
sudo chown -R www-data:www-data /var/www/mikong
```

### 4.2 Nginx 配置模板

`/etc/nginx/sites-available/mikong.conf`：

```nginx
# ============ 正式域名（备案通过后启用）============
server {
    listen 80;
    server_name 你的域名.com;              # ← 替换为备案域名

    root /var/www/mikong;
    index index.html;

    # 静态资源缓存：js/css 带 ?v=N 版本号，可长期缓存
    location ~* \.(js|css)$ {
        expires 7d;
        add_header Cache-Control "public";
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}

# ============ 备案期间临时预览（备案通过后删除本段）============
server {
    listen 8080;
    server_name _;
    root /var/www/mikong;
    index index.html;
    location / { try_files $uri $uri/ /index.html; }
}
```

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/mikong.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
# 轻量服务器防火墙/安全组：放行 80、443、8080（临时）
```

## 5. 域名解析 + HTTPS

备案通过后：

1. **DNS 解析**：在注册商控制台添加 A 记录
   - 主机记录 `@` → 服务器公网 IP
   - 主机记录 `www` → 服务器公网 IP
2. **HTTPS 证书**（免费）：
   - 云厂商控制台可申请免费 DV 证书（每年 20 张额度），下载 Nginx 版部署；
   - 或用 certbot：`sudo certbot --nginx -d 你的域名.com -d www.你的域名.com`
3. 在 Nginx 中启用 443 并将 80 跳转 https（certbot 可自动完成）

## 6. 后续更新流程

1. 本地改完代码后，**务必按 AGENTS.md 规则**把 `index.html` 里对应 `<script>/<link>` 的 `?v=N` 版本号 +1
2. 重新打包改动文件上传覆盖服务器上同名文件即可，无需重启 Nginx
3. 更新清单模板见上文 4.1

## 7. 常见问题

| 问题 | 处理 |
| --- | --- |
| 域名打不开、提示备案 | 备案未通过或解析未生效，等管局短信；用 `http://IP:8080` 临时预览 |
| 改了 JS 不生效 | index.html 版本号未 +1，浏览器缓存了旧文件 |
| 后端上线后数据不共享 | localStorage 是每浏览器独立的；前后端分离后由 `/api/v1/state` 统一存取（api.js 已预留） |
| 想换服务器 | 备案需在新厂商做「接入备案」，域名可继续使用 |

---

**下一步行动**：按第 1、2 节购买域名与服务器（需本人实名，无法代办）。购买完成后，把服务器 IP 和登录方式交给 AI 助手，可协助完成第 4~5 节的全部部署操作。
