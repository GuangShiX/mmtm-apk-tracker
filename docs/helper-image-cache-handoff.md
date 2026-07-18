# Helper 图片缓存改造交接提示词

将下面内容作为新 session 的首条提示词：

```text
请改造 D:\NewProjects\memento\mementomori-helper-master 的图片资源读取流程。

职责边界：
- 不要在 helper 中实现游戏版本检测、APK 下载、Unity 解包或 GitHub 发布。
- 这些工作由 D:\NewProjects\mmtm-apk-tracker 和公开仓库
  https://github.com/GuangShiX/mmtm-assets-fallback 负责。
- helper 只在实际需要某张图片时下载一次，校验后保存到本地；以后优先使用本地文件。
- 大型且基本不变化、已经随 helper 发布的图片继续保存在 helper 仓库，不迁移到远端。

远端协议：
- 清单：
  https://raw.githubusercontent.com/GuangShiX/mmtm-assets-fallback/main/manifest.json
- 图片 URL：{manifest.base_url}/{asset.path}
- 图片路径不包含游戏版本，更新判断必须使用每条 asset 的 sha256，不能只看 URL 或游戏版本。
- 主要分类为 characters、enemies、equipment、spheres、items、ui。
- 角色小头像名为 CHR_<六位角色ID>_<两位变体>_s.png，默认变体是 00。

开始修改前先检查真实代码路径，重点查看 CharacterIconService、
CharacterIconController、GameIconRenderService、CharacterIcon.razor、
wwwroot/cache/character-icons、wwwroot/cache/character-icon-assets 和
wwwroot/images/items，确认现有缓存键和头像合成路径。

实现要求：
1. 新增一个统一的 GitHubImageAssetService（名称可按现有风格调整），负责清单缓存、
   资源查找、下载、SHA-256 校验和本地路径返回；其他页面不要各自直接发 HTTP 请求。
2. 清单保存到本地并设置合理刷新间隔（建议 6 小时）。网络失败时可继续使用上次成功清单，
   但不能把失败响应覆盖到本地。
3. 图片按 category/name 保存在 helper 的持久缓存目录，并维护对应 sha256。
   本地 SHA 与清单一致时不联网；不一致时重新下载。
4. 下载到同目录临时文件，限制单文件最大 5 MiB，检查 HTTP 2xx、PNG 签名、文件大小和
   SHA-256，全部通过后原子替换正式文件。失败时删除临时文件。
5. 同一资源的并发请求必须合并，避免一个页面同时触发多次下载。设置连接/总超时和有限重试，
   不允许无限重试。
6. CharacterIconService 的合成素材改为从本服务取得。至少支持角色头像、plate_character、
   icon_rarity_plus_star_1、icon_rarity_plus_star_2、frame_common_*、
   frame_decoration_* 和 icon_element_*。
7. 最终合成头像的缓存键必须包含所有输入图片的 SHA-256，或在输入 SHA 变化时准确失效；
   不能继续只用角色 ID、等级和稀有度，否则远端同名图片更新后仍会返回旧合成图。
8. 角色、敌人、装备、符石和物品找不到远端资源时，沿用现有本地图或占位图，不让页面报错，
   也不要在每次渲染时反复请求同一个不存在的文件。
9. 防止 category/name 路径穿越；不要把 GitHub token、账号或任何密钥加入配置，公开 Raw URL
   不需要凭据。
10. 保留现有账号作用域和只读页面行为，不要借此重构无关功能。

验证要求：
- 为清单命中、本地缓存命中、SHA 变化重下、损坏 PNG、哈希不符、网络失败使用旧清单、
  并发请求合并、合成缓存失效补测试。
- 用 CHR_000149_00_s.png 做真实首次下载和第二次本地命中验证。
- 构建整个解决方案并报告实际缓存目录、测试结果和修改文件。
```
