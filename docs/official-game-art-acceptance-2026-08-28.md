# 官方游戏美术资产工作流验收记录（2026-08-28）

## 结论

本次按需资产清单、官方来源解析、基础包提取、Addressables 合并、清单生成、独立文件验收和视觉抽检均通过。验收对象为本机 `mmtm-assets-fallback` 工作树，尚未执行远端提交、推送或 CI 发布。

| 项目 | 结果 |
| --- | --- |
| 游戏版本 | `4.21.0` |
| Addressables 版本 | `4a851fa27b84c8c1a05e57608a16c6486c404219_1` |
| Master 版本 | `1787808602106` |
| 需求清单 SHA-256 | `8ea09d7a12e0164fdf8e40ea461f18968d6d3acbb1d1676d4a57d9df147adc80` |
| 已登记必需资产 | 100（UI 86、差分头像 14） |
| 最终仓库资产 | 1,517 |
| 最终仓库文件字节 | 54,626,378 |
| 基础 APK Bundle | 5,284 |
| 基础包导出 | 1,501 |
| catalog 精确目标 | 16 |
| 跨平台复用规范 PNG | 1,475 |
| tracker 测试 | 55 项通过 |
| 独立仓库验收 | `accepted` |

## 本次新增常用官方 UI

下列 16 项均从官方 APK/XAPK Bundle 的 Sprite/Container 提取，manifest 保存 `source_resource_key` 和 Bundle 来源：

- 通用控件：`toggle_04_off.png`、`base_metal.png`、`base_ribbon_01.png`；
- 公会战城池：`Castle_0_0.png` 至 `Castle_0_2.png`、`Castle_1_0.png` 至 `Castle_1_2.png`；
- 公会战原始地图：`Map_0.png`、`Map_1.png`，均为 `2780×2500`；
- 快速战斗奖励图：`RQB_000001.png` 至 `RQB_000005.png`。

旧 Helper 文件 `base_ribbon.png`、`localgvg.png`、`globalgvg.png` 不是客户端中的规范 Sprite 名称。官方原名分别为 `base_ribbon_01.png`、`Map_0.png`、`Map_1.png`；两张旧地图还包含项目侧缩放/路线合成结果。因此资产仓库只保存官方原始 Sprite，派生展示继续由消费端负责。

## 本次新增差分玩家头像

官方 `SpecialIconItemMB` 当前共有 303 条记录。本次只登记账号快照实际出现的 14 个道具 ID，不批量发布全部差分头像：

| SpecialIconItemId | CharacterId | IconId | 规范文件 |
| ---: | ---: | ---: | --- |
| 42 | 42 | 1 | `CHR_000042_00_em_001.png` |
| 65 | 65 | 1 | `CHR_000065_00_em_001.png` |
| 68 | 68 | 1 | `CHR_000068_00_em_001.png` |
| 69 | 69 | 1 | `CHR_000069_00_em_001.png` |
| 71 | 71 | 1 | `CHR_000071_00_em_001.png` |
| 75 | 75 | 1 | `CHR_000075_00_em_001.png` |
| 76 | 76 | 1 | `CHR_000076_00_em_001.png` |
| 78 | 78 | 1 | `CHR_000078_00_em_001.png` |
| 89 | 89 | 1 | `CHR_000089_00_em_001.png` |
| 101 | 101 | 1 | `CHR_000101_00_em_001.png` |
| 102 | 102 | 1 | `CHR_000102_00_em_001.png` |
| 129 | 129 | 1 | `CHR_000129_00_em_001.png` |
| 135 | 135 | 1 | `CHR_000135_00_em_001.png` |
| 1003 | 45 | 2 | `CHR_000045_00_em_002.png` |

其中 10 项来自当前 4.21.0 基础 APK，4 项由当前官方 Addressables catalog 精确补齐。13 项为 `128×128`，`CHR_000078_00_em_001.png` 的官方 Sprite 裁切为 `126×128`；在账号列表 80×80 居中 `cover` 抽检中主体均完整。

## 消费端语义验收

React 前端在 `1440×900` 桌面视口通过真实 5700 缓存验收：

- 单账号主页把已合成缓存 URL 还原为 `/api/character-icon?icon=71&type=0`，请求中不含 `rarity`、`level`、`link`，最终展示纯头像和简约角线；缺少头像 ID 时显示诚实的玩家文字占位，不再冒充某个默认角色。
- 角色工作台共渲染 97 个信息型头像，首屏缓存路径包含真实稀有度、等级和联结状态，例如 `0_8_lr2_290_1.svg`；列表与详情均保留稀有度框、等级和星级。
- 页面控制台错误为 0；review-only 截图为 `screenshots/avatar-policy-account-home-1440x900.png`、`screenshots/avatar-policy-account-home-missing-1440x900.png` 与 `screenshots/avatar-policy-character-workspace-1440x900.png`。

## 门禁覆盖

- 头像消费语义：角色工作台使用真实稀有度、等级、等级联结和星级合成图；账号主页、账号列表和账号选择器使用无数据层的装饰头像；
- 清单结构、消费者、原因、分类和交付方式；
- 差分头像 Master 映射、规范文件名和精确 Addressables key 一致性；
- 需求清单变化触发同版本基础包重扫；
- 同版本 catalog 专属资源保留，避免基础包重建导致无意义重编码；
- 跨平台 PNG 像素一致时复用仓库规范字节；
- manifest 唯一路径、文件存在、大小、SHA-256、PNG 解码与实际尺寸；
- 每个登记资产存在 `source_resource_key` 或 `source_catalog_keys`；
- 资产仓库 CI 在数据提交前独立运行需求清单验收。

视觉抽检图为 review-only 输出：

- `output/official-asset-workflow-avatar-qa.png`
- `output/official-asset-workflow-common-ui-qa.png`
