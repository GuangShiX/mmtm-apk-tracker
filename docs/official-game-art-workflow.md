# 官方游戏美术资产按需补全工作流

本工作流适用于 Helper、React 前端及后续项目需要新增 MementoMori 官方头像、图标、边框、地图或其他游戏美术时。目标不是镜像完整游戏资源，而是维护一套**有消费者、有官方身份证据、可重复生成、可验收**的轻量资产仓库。玩家可选择头像是明确例外：其单文件很小、运行时会任意切换，因此按当前 Master 保留完整库存。

## 仓库职责

- `mmtm-apk-tracker`：唯一的需求登记、官方身份解析、基础包/Addressables 提取和验收规则来源。
- `mmtm-assets-fallback`：由 tracker 生成的发布结果；`assets/`、`manifest.json`、`latest.json` 不手工编辑。
- Helper / React：只消费清单中需要的稳定路径，并按 `sha256` 缓存；项目派生图、截图和生成式素材不反向冒充官方资产。

需求清单位于 `config/official_asset_requests.json`。完整玩家头像库存位于自动生成的 `config/player_avatar_inventory.json`，由 `CharacterMB` 与 `SpecialIconItemMB` 产生；同一资源可以属于多个需求组，但最终只发布一份规范文件。

## 头像消费语义（强制）

头像是否带稀有度框、等级和星级，取决于它在界面中的业务语义，不能按页面美术偏好随意切换：

- **信息型角色头像**：角色一览、角色详情、阵容、主线推进等需要识别当前角色状态的界面，必须使用玩家快照中的真实稀有度、等级、等级联结和星级数据，由 Helper 的头像合成器生成；React 使用 `GameCharacterIcon`，不得用默认值伪造缺失数据。
- **装饰型身份头像**：单账号主页、账号列表、账号选择器、任务账号标识等只用于表达“这是哪个玩家”的位置，只显示官方原始头像和简约装饰线框；React 使用 `GameCharacterPortrait` 或 `resolveCharacterPortraitUrl`，不得显示稀有度框、等级或星级。
- 同一张官方原始头像可以服务两类消费端，但资产仓库只保存无玩家数据层的规范 Sprite。稀有度框、等级、星级等运行时合成结果不得作为通用美术资产入库，也不得写入静态资产文件名或 manifest。

评审新头像调用点时，必须先标记 `information` 或 `decorative`；语义不明确时不得直接复用现有组件。

## 新增资产的六步流程

### 1. 登记需求

默认只登记当前功能实际需要的精确文件，不使用宽泛正则收录大尺寸或无消费者的整类美术。以下紧凑、通用且高复用的类别持续保留完整集合：

- 当前 `CharacterMB` 的普通玩家头像；
- 当前 `SpecialIconItemMB` 的全部可兑换差分玩家头像；
- 官方包内符合规范命名的角色小头像（含魔女化差分）、敌人、装备、物品和符石小图标。

其他资产组必须写明：

- 稳定 `id`；
- `category`、`kind` 和交付位置；
- 至少一个消费端；
- 业务原因；
- 精确文件名；可用时同时登记精确 Addressables key。

玩家头像库存必须保留 Master 版本、Book SHA-256 和记录数。差分头像的 `special_icon_item_id`、`character_id`、`icon_id` 由 `SpecialIconItemMB` 自动同步；文件名和 Addressables key 由角色 ID 与 Icon ID 生成，不能直接拿道具 ID 拼接。

### 2. 证明官方身份

至少满足一种证据：

- 官方 MasterBook 映射到精确 Addressables key；
- 官方 Prefab / Container 路径指向精确 Sprite；
- 官方 APK/XAPK Bundle 扫描得到精确 `source_resource_key`；
- 当前官方 Addressables catalog 将精确 key 解析到 Bundle。

页面截图、浏览器渲染结果、改名文件和项目内重编码副本不能单独作为官方身份证据。若项目旧文件是官方原图的裁切、缩放或路线合成版本，应登记客户端中的官方原始 Sprite，派生处理留在消费端。

### 3. 校验清单

```bash
python asset_requests.py sync-player-avatar-inventory \
  --master-dir <MasterBook目录> \
  --master-version <masterVersion>
python asset_requests.py verify-player-avatar-inventory \
  --master-dir <MasterBook目录> \
  --master-version <masterVersion>
python asset_requests.py list
python -m unittest discover -s tests -v
```

计划任务每 6 小时运行一次 `sync-player-avatar-inventory --auto-update`。只有 `CharacterMB` 或 `SpecialIconItemMB` 实际变化时才提交库存；资产仓库的计划任务随后按新清单补齐文件。加载器会拒绝重复大小写、非法路径、不完整消费者信息，以及普通/差分头像文件名、Addressables key 与 Master 映射不一致等情况。

### 4. 生成资产仓库

```bash
python fallback.py \
  --auto-update \
  --output <mmtm-assets-fallback> \
  --repository GuangShiX/mmtm-assets-fallback
```

更新键不仅包含 `appVersion` 和 `assetVersion`，还包含需求清单及玩家头像库存内容共同计算的 SHA-256：

- 游戏版本变化：重新下载官方 APK 并扫描基础包；
- 需求清单变化：即使游戏版本没变，也重新扫描基础包；
- Addressables 版本变化或基础包缺少已登记热更新素材：只下载精确 key 对应的 Bundle；
- 清单、基础包和热更新合并后仍缺任一登记资产：生成失败，不发布残缺仓库。

### 5. 独立验收

```bash
python asset_requests.py verify-repository --repository <mmtm-assets-fallback>
```

验收门禁会检查：

- `request_registry_sha256` 与当前清单一致，状态为 `complete`；
- 所有登记资产都位于 `assets/<category>/<name>`；
- 清单无重复、路径不越界；
- 每个 manifest 条目的文件大小和 SHA-256 与磁盘一致；
- 文件是可解码 PNG 且尺寸有效；
- 每个登记资产保留 `source_resource_key` 或 `source_catalog_keys` 来源证据；
- 普通与差分玩家头像最小边至少 64 px、宽高比在 0.9–1.1，并输出实际尺寸供 UI 复核；官方 Sprite 允许存在少量透明边缘裁切差异，消费端统一使用居中 `cover` 裁切。

消费端还需在真实桌面尺寸复核最终展示。头像类至少检查列表实际尺寸、裁切、透明边缘、占位回退和多账号数据绑定；地图等大图检查构图和缩放，不用派生截图替代仓库原图。

### 6. 发布与消费

tracker 规则、生成后的资产仓库、消费端同步分别检查工作树和差异。只有本地验收全部通过后，才单独提交并推送 tracker，再由资产仓库 CI 或明确的本地发布操作生成远端内容。远端推送、触发 CI 和部署消费端属于独立外部写入，不由资产扫描命令隐式执行。

## 本次基线

当前强制需求清单包含四组，并另有一条完整差分头像发现规则：

- 21 个头像合成公共 UI；
- 58 个角色工作台 Prefab 依赖（与上一组有 9 个复用项）；
- 16 个常用玩法 UI 原始 Sprite；
- 133 个 `CharacterMB` 普通玩家头像；
- 当前官方包和 catalog 中实际存在、且能回映到 `SpecialIconItemMB` 的全部差分玩家头像。

当前去重后共 219 个强制验收资产：86 个 UI、133 个普通玩家头像。`SpecialIconItemMB` 的全部声明保留在库存中，但只有能从当前基础包或 catalog 解析到官方文件的记录才发布；当前为 303 条声明、128 个真实差分 PNG、175 条无文件预留记录。除此之外，官方包中符合规范命名的角色小头像（包括 `_01_s` 魔女化差分）、敌人、装备、符石和物品仍由既有精确命名规则完整维护，不需要逐个登记；大地图和其他大尺寸图只按明确消费者登记。
