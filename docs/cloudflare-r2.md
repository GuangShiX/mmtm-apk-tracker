# Cloudflare R2 关键图标主源

R2 保存 helper 需要的关键小图标，GitHub 仓库 `GuangShiX/mmtm-assets-fallback` 作为灾备。公开图片 URL 不包含游戏版本号：

```text
https://assets.example.com/mementomori/assets/characters/CHR_000001_00_s.png
https://assets.example.com/mementomori/manifest.json
```

游戏版本只保留在 `manifest.json` 元数据和 Git 标签中。每个图片记录包含 SHA-256；同一路径的内容发生变化时，helper 根据哈希更新本地缓存。

## 1. 创建 R2 存储桶

1. 登录 <https://dash.cloudflare.com/>。
2. 进入 `Storage & databases` -> `R2 object storage`。
3. 首次使用时按页面提示启用 R2。
4. 选择 `Create bucket`。
5. Bucket 名称建议使用 `mementomori-assets`。
6. Storage class 使用 `Standard`，Location 默认自动即可。

不要在存储桶中手动建立版本目录。发布器使用 `mementomori/assets/...` 固定对象键，并保留不再出现在新清单中的旧对象。

## 2. 配置公开域名

正式使用推荐准备一个已接入同一 Cloudflare 账号的域名，例如：

```text
assets.example.com
```

进入 R2 Bucket -> `Settings` -> `Custom Domains` -> `Add`，填写该域名并等待状态变为 `Active`。自定义域名会启用 Cloudflare Cache。

没有域名时可以暂时在 Bucket 设置中开启 `Public Development URL`，获得 `https://pub-....r2.dev` 地址。`r2.dev` 有速率限制，只用于初次验证，后续再换自定义域名。切换域名不会改变对象键，只需修改 `R2_PUBLIC_BASE_URL` 并重新运行工作流。

## 3. 创建最小权限写入凭据

1. R2 Overview 页面找到 `API Tokens`，选择 `Manage`。
2. 创建 Account API Token。
3. Permission 选择 `Object Read & Write`。
4. Scope 选择 `Apply to specific buckets only`，只勾选 `mementomori-assets`。
5. 创建后立即保存页面显示的 `Access Key ID` 和 `Secret Access Key`。Secret 之后不会再次显示。
6. 在 R2 Overview 复制 `Account ID`。

这些凭据只交给发布工作流，不能放进 helper、网页 JavaScript、`config.json` 或公开仓库。

## 4. 配置 GitHub Actions

打开备用仓库：

<https://github.com/GuangShiX/mmtm-assets-fallback/settings/secrets/actions>

在 `Repository variables` 添加：

| 名称 | 示例 |
|---|---|
| `R2_ACCOUNT_ID` | Cloudflare Account ID |
| `R2_BUCKET` | `mementomori-assets` |
| `R2_PREFIX` | `mementomori` |
| `R2_PUBLIC_BASE_URL` | `https://assets.example.com` 或临时 `r2.dev` 地址 |

在 `Repository secrets` 添加：

| 名称 | 内容 |
|---|---|
| `R2_ACCESS_KEY_ID` | R2 Access Key ID |
| `R2_SECRET_ACCESS_KEY` | R2 Secret Access Key |

## 5. 执行首次发布

进入：

<https://github.com/GuangShiX/mmtm-assets-fallback/actions/workflows/update-icons.yml>

选择 `Run workflow`。即使当前游戏版本没有更新，工作流也会把仓库现有的 1,403 个图标同步到 R2。首次会上传约 32 MB，之后相同 SHA-256 的文件全部跳过。

完成后检查：

```text
{R2_PUBLIC_BASE_URL}/{R2_PREFIX}/latest.json
{R2_PUBLIC_BASE_URL}/{R2_PREFIX}/manifest.json
{R2_PUBLIC_BASE_URL}/{R2_PREFIX}/assets/ui/frame_common_slice.png
```

## 6. helper 缓存与回退协议

1. 每天或游戏更新后读取 R2 `manifest.json`。
2. 本地图片存在且保存的 SHA-256 与清单一致时直接返回本地文件。
3. 不存在或哈希变化时请求 `{base_url}/{path}?sha256={sha256前12位}`。
4. 下载后验证完整 SHA-256，验证成功后原子替换本地文件。
5. R2 超时、非 2xx 或哈希不匹配时，改用 GitHub `main/manifest.json` 和 `main/assets/...`。
6. 两个远端都失败时继续使用已有本地文件或内置占位图。

查询参数不是游戏版本号，只在极少数同名图片内容变化时绕过旧 CDN 缓存。R2 对象路径和 helper 本地文件名始终固定。图片响应缓存一小时，清单要求每次重新验证；正常新增图片不需要清理 CDN 缓存。

## 本地验证

将 `.env.example` 复制为本地 `.env` 并填写 R2 配置后运行：

```powershell
python publish_r2.py --check
python publish_r2.py --source D:\NewProjects\mmtm-assets-fallback --dry-run
python publish_r2.py --source D:\NewProjects\mmtm-assets-fallback
```

`.env` 已被 Git 忽略。不要把真实凭据粘贴到 Issue、日志或提交中。
