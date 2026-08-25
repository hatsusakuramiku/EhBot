# 前端第三方静态资源（vendored）

这些文件**随镜像分发**，不从 CDN 加载。原因：离线部署必须可用，且不应让浏览器把请求发往第三方。

| 文件 | 版本 | 来源 | SHA-256 |
|------|------|------|---------|
| `htmx-2.0.4.min.js` | 2.0.4 | https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `htmx-ext-sse-2.2.2.js` | 2.2.2 | https://unpkg.com/htmx-ext-sse@2.2.2/sse.js | `83eca6fa0611fe2b0bf1700b424b88b5eced38ef448ef9760a2ea08fbc875611` |
| `alpine-3.14.8.min.js` | 3.14.8 | https://unpkg.com/alpinejs@3.14.8/dist/cdn.min.js | `b600e363d99d95444db54acbfb2deffec9ae792aa99a09229bcda078e5b55643` |

合计约 104 KB（未压缩），gzip 后约 35 KB。

## 用途分工

- **HTMX**：局部替换（取代 `<meta http-equiv="refresh">` 整页刷新）与表单提交。
- **htmx SSE 扩展**：订阅 `/api/v1/events`，状态跃迁时立即刷新对应行。
- **Alpine.js**：轻量客户端交互态（下拉、抽屉、多选、密度切换），不引入构建链。

## 升级方式

```powershell
$dst = 'app/web/static/vendor'
curl.exe -sS -L -o "$dst/htmx-<新版本>.min.js" "https://unpkg.com/htmx.org@<新版本>/dist/htmx.min.js"
Get-FileHash "$dst/htmx-<新版本>.min.js" -Algorithm SHA256
```

升级后需：更新本表的版本与校验和、更新 `base.html` 的引用文件名、删除旧文件、跑一遍全量测试。
文件名带版本号是为了让浏览器缓存自然失效，因此**不要**改成不带版本号的名字。