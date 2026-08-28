# 官方游戏美术资产工作流验收记录（2026-08-28）

## 结论

本次完整玩家头像库存、官方来源解析、基础包提取、Addressables 合并、轻量图标保留、清单生成、独立文件验收和视觉抽检均通过。验收对象为本机 `mmtm-assets-fallback` 工作树；远端提交、推送与 CI 状态在发布后另行核对。

| 项目 | 结果 |
| --- | --- |
| 游戏版本 | `4.21.0` |
| Addressables 版本 | `4a851fa27b84c8c1a05e57608a16c6486c404219_1` |
| Master 版本 | `1787808602106` |
| 需求清单 SHA-256 | `1e46d7cb9b007bab8dfbd8e4362a68727ee5e069de65f8b57bf43301bda1fe61` |
| 已登记必需资产 | 219（UI 86、普通玩家头像 133） |
| Master 差分头像声明 | 303（实际发布图片 128、无官方文件预留 175） |
| 最终仓库资产 | 1,631 |
| 最终仓库文件字节 | 58,661,938 |
| 基础 APK Bundle | 5,284 |
| 基础包导出 | 1,604 |
| catalog 精确目标 | 27 |
| tracker 测试 | 56 项通过 |
| 独立仓库验收 | `accepted` |

## 本次新增常用官方 UI

下列 16 项均从官方 APK/XAPK Bundle 的 Sprite/Container 提取，manifest 保存 `source_resource_key` 和 Bundle 来源：

- 通用控件：`toggle_04_off.png`、`base_metal.png`、`base_ribbon_01.png`；
- 公会战城池：`Castle_0_0.png` 至 `Castle_0_2.png`、`Castle_1_0.png` 至 `Castle_1_2.png`；
- 公会战原始地图：`Map_0.png`、`Map_1.png`，均为 `2780×2500`；
- 快速战斗奖励图：`RQB_000001.png` 至 `RQB_000005.png`。

旧 Helper 文件 `base_ribbon.png`、`localgvg.png`、`globalgvg.png` 不是客户端中的规范 Sprite 名称。官方原名分别为 `base_ribbon_01.png`、`Map_0.png`、`Map_1.png`；两张旧地图还包含项目侧缩放/路线合成结果。因此资产仓库只保存官方原始 Sprite，派生展示继续由消费端负责。

## 完整玩家头像与差分库存

当前 `CharacterMB` 定义 133 个普通玩家头像，全部作为强制资产验收。`SpecialIconItemMB` 有 303 条声明，其中 128 个文件能从当前 4.21.0 官方包或 Addressables catalog 实际解析，175 条为没有官方图片文件的预留记录。工作流保存全部 303 条 Master 映射用于漂移审计，但资产仓库只发布真实存在的 128 个差分 PNG，避免把预留槽位误报为缺失资产。

128 个差分头像全部带有官方 `source_resource_key` 和 Bundle 证据，其中 15 个同时保留当前 catalog key；相较上一个仓库版本新增 114 个文件。127 个为 `128×128`，`CHR_000078_00_em_001.png` 的官方 Sprite 裁切为 `126×128`。`CHR_000063_00_em_001.png`（本次缺失账号实际需要）与 `CHR_000002_00_em_003.png`（多差分样本）均通过 64、40、28 px 水彩浅底/深底双背景抽检，主体和辨识度完整。

普通与小型角色图标共 149 个：133 个 `_00_s` 普通头像和 16 个 `_01_s` 魔女化小头像；完整 characters 分类为 277 个（149 个小头像 + 128 个可兑换差分）。

## 消费端语义验收

React 前端在 `1440×900` 桌面视口通过真实 5700 缓存验收：

- 单账号主页把已合成缓存 URL 还原为 `/api/character-icon?icon=71&type=0`，请求中不含 `rarity`、`level`、`link`，最终展示纯头像和简约角线；缺少头像 ID 时显示诚实的玩家文字占位，不再冒充某个默认角色。
- 角色工作台共渲染 97 个信息型头像，首屏缓存路径包含真实稀有度、等级和联结状态，例如 `0_8_lr2_290_1.svg`；列表与详情均保留稀有度框、等级和星级。
- 页面控制台错误为 0；review-only 截图为 `screenshots/avatar-policy-account-home-1440x900.png`、`screenshots/avatar-policy-account-home-missing-1440x900.png` 与 `screenshots/avatar-policy-character-workspace-1440x900.png`。

## 门禁覆盖

- 头像消费语义：角色工作台使用真实稀有度、等级、等级联结和星级合成图；账号主页、账号列表和账号选择器使用无数据层的装饰头像；
- 清单结构、消费者、原因、分类和交付方式；
- 普通头像 CharacterMB 映射、差分头像 SpecialIconItemMB 映射、规范文件名和 Addressables key 一致性；
- Master 预留记录与当前真实可发布图片分开统计，不把不存在的槽位伪造成仓库缺失；
- 需求清单变化触发同版本基础包重扫；
- 同版本 catalog 专属资源保留，避免基础包重建导致无意义重编码；
- 跨平台 PNG 像素一致时复用仓库规范字节；
- manifest 唯一路径、文件存在、大小、SHA-256、PNG 解码与实际尺寸；
- 每个登记资产存在 `source_resource_key` 或 `source_catalog_keys`；
- 资产仓库 CI 在数据提交前独立运行需求清单验收。

视觉抽检图为 review-only 输出：

- `output/full-avatar-63-qa.png`
- `output/full-avatar-2-variant3-qa.png`
- `output/official-asset-workflow-common-ui-qa.png`
