# 阿里云 OSS 与 CDN 配置

这套发布流程使用 OSS 保存源文件，CDN 只负责缓存和分发。图片使用 SHA-256 文件名，因此更新时不会覆盖旧图片；程序先上传所有新增图片，再上传版本清单，最后更新 `latest.json`。

## 1. 创建 Bucket

在 OSS 控制台创建 Bucket：

- 地域：`华北 2（北京）`
- 存储类型：`标准存储`
- 存储冗余：`本地冗余 LRS`
- 读写权限：`私有`
- 版本控制：关闭
- 服务端加密：OSS 托管密钥或关闭均可

Bucket 名称必须全局唯一。下文用 `YOUR_BUCKET` 表示真实名称。

## 2. 创建仅用于发布的 RAM 用户

不要使用阿里云主账号 AccessKey。

1. 进入 RAM 控制台，创建用户 `mmtm-oss-publisher`。
2. 关闭控制台登录，只开启 OpenAPI AccessKey。
3. 创建自定义权限策略，把下面两处 `YOUR_BUCKET` 替换成 Bucket 名称。
4. 将策略授权给该 RAM 用户，然后创建 AccessKey。

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "oss:ListObjects",
      "Resource": "acs:oss:*:*:YOUR_BUCKET",
      "Condition": {
        "StringLike": {
          "oss:Prefix": [
            "mementomori",
            "mementomori/*"
          ]
        }
      }
    },
    {
      "Effect": "Allow",
      "Action": [
        "oss:GetObject",
        "oss:PutObject"
      ],
      "Resource": "acs:oss:*:*:YOUR_BUCKET/mementomori/*"
    }
  ]
}
```

该用户没有删除 Bucket、删除文件或修改权限的能力。AccessKey 只放入本机 `.env` 或 GitHub Actions Secrets，不写入 `config.json`，也不要发送到聊天中。

## 3. 首次本地发布

安装依赖并运行交互式配置：

```powershell
python -m pip install -r requirements.txt
python publish.py --configure
python publish.py --check
```

北京公网 Endpoint 使用：

```text
https://oss-cn-beijing.aliyuncs.com
```

尚未创建 CDN 时，`CDN_BASE_URL` 可以留空。先验证本地清单，再执行首次发布：

```powershell
python publish.py --version 4.18.0 --dry-run
python run.py --version 4.18.0 --publish-only
```

当前 4.18.0 会生成 7,321 条图片记录，内容去重后约 3,998 个唯一文件、1.14 GB。首次上传时间取决于本地上行带宽；中断后重新运行会复用已上传对象。

发布完成后的关键对象：

```text
mementomori/objects/<hash-prefix>/<sha256>.png
mementomori/manifests/versions/4.18.0.json
mementomori/manifests/latest.json
```

## 4. 开启自动检查

本机长期运行时，安装每小时执行一次的 Windows 计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows_task.ps1
```

执行日志位于 `reports/scheduled-task.log`。流程会自动：

1. 查询 APKPure 最新版本。
2. 检查 OSS 的 `latest.json`，已发布时立即结束。
3. 下载并 CRC 校验新 XAPK。
4. 完整解包并验证原始对象覆盖率。
5. 只上传 OSS 中不存在的图片哈希。
6. 更新版本清单和 `latest.json`。

电脑关机期间不会执行；再次开机后计划任务会补跑。

## 5. 配置 CDN

月活和请求量很小时可以先不启用 CDN。要启用时需要一个可管理 DNS 的子域名，例如 `assets.example.com`；中国内地 CDN 域名通常需要完成 ICP 备案。

1. 在阿里云 CDN 控制台添加加速域名。
2. 业务类型选择图片或静态内容分发。
3. 源站类型必须选择 `OSS 域名`，再选择这个北京 Bucket。
4. 开启“OSS 私有 Bucket 回源”，完成同账号授权。
5. 按控制台提示把子域名 CNAME 指向阿里云 CDN。
6. 配置 HTTPS 证书。
7. 设置缓存规则：

```text
/mementomori/objects/                  365 天
/mementomori/manifests/versions/       365 天
/mementomori/manifests/latest.json     1 分钟
```

CDN 生效后重新运行 `python publish.py --configure`，把 `CDN_BASE_URL` 设置为 `https://assets.example.com`，然后执行：

```powershell
python run.py --version 4.18.0 --publish-only --force
```

这只会重建清单并复用已有哈希对象。之后图片更新会产生新的 URL，不需要刷新旧图片缓存；`latest.json` 最多约一分钟后指向新版本。

## 6. GitHub Actions

仓库包含 `.github/workflows/update-assets.yml`，每 6 小时检查一次。在 GitHub 仓库的 `Settings > Secrets and variables > Actions` 中添加：

Secrets：

```text
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
```

Variables：

```text
OSS_BUCKET       必填
OSS_PREFIX       可选，默认 mementomori
CDN_BASE_URL     启用 CDN 后填写
ASSET_RUNNER     可选
```

`OSS_BUCKET` 未设置时定时任务会保持跳过状态，不会产生失败运行或云端费用。

完整解包会创建 128 万个原始对象和大量中间文件。GitHub 免费托管 Runner 可能因磁盘或六小时上限失败；稳定方案是本机计划任务、带足够磁盘的 ECS，或者标签为 `mmtm-assets` 的 Windows 自托管 Runner，并把 `ASSET_RUNNER` 设置为 `mmtm-assets`。

## 7. 消费端缓存

helper 应先读取 `manifests/latest.json`，再读取其中的 `manifest_url`。图片下载后按 `sha256` 保存到本地；本地已存在且哈希一致时不再访问 CDN。不要按固定 URL 覆盖旧图片，否则会重新引入缓存失效问题。
