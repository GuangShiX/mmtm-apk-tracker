# GitHub CI 维护窗口检测与新角色信息

本流程使用 GitHub 托管的 Ubuntu runner，检测和产物生成不依赖个人电脑、Codex 心跳或本机定时任务。部署前的文件与测试通过不代表云端已经启用。

## 执行顺序

1. `watch-maintenance.yml` 每 15 分钟读取官方匿名维护表和公告。在维护开始前 30 分钟至结束后 30 分钟内启动持续检测，普通时段只做轻量窗口检查。
2. 同一运行中的 `cloud_update_watch.py --watch` 每 20 秒为目标间隔比较 app/asset/master 及目录签名；每 5 分钟复核维护时间。延长、提前结束与失败分别记录。单任务有界运行，超长窗口由后续排程继续接管。
3. 从已发布 `skills` 和上次验证成功缓存恢复基线。探测错误不能显示成版本不变；下载/校验/导出失败不能晋升成功基线。
4. 更新后先获取关键主表，在 runner 临时目录保存并校验原始字节。原表、完整包体与私有模拟器代码不上传到公开仓库或公开 artifacts。
5. 派生技能 JSON 和 `brief.md` 导出成功后立即结束检测步骤，上传 `character-skills-<runId>`，先交付文字信息；海报任务随后单独消费这份确定的快照。
6. 海报任务只生成确认新增且已导出的 CharacterId，沿用 `split` 横版。它的失败不撤回已交付技能信息。图片/文本缺失须明确保留，不以退出成功代表所有官方素材齐全。
7. 私有 5830 模型通过该角色信息包接入；公共工作流不包含私有技能模型源码或自动改写个人电脑。

GitHub schedule 可能排队延迟或被丢弃，最短 schedule 间隔为 5 分钟；20 秒检测来自已启动 runner 内的循环，不是 cron。提前 30 分钟准备降低晚启动风险，但不能保证某个秒点上线。需立即开始时可手动运行同一 workflow，仍在云端执行。

参考：[GitHub schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[托管任务时长限制](https://docs.github.com/en/actions/reference/limits)。

## 缓存、并发与产物

- `contents: read`；本工作流不推送代码、数据或标签，也不触发公开图片发布。
- workflow concurrency 保证一个活动检测，避免同一窗口多个检测进程抢写。后来的计划任务等待，开始后重新读取维护状态。
- `reports/cloud-update` 缓存成功状态和派生输出；raw 位于 `${runner.temp}/mmtm-private-master`，不在缓存/上传路径中。
- 技能 artifact 只允许 `export/skills/`、独立的 `export/preview/skills/` 与 `export/brief.md`，保留 14 天。预览保留官方实际开放时间，不混入正式技能基线。海报 artifact 单独上传，不将整个 reports 目录作为交付物。
- 缓存缺失时读取公开技能基线；完全无可比较基线时建立基线，不能把全部已有角色当新增。缓存只用于恢复，不证明在线数据新鲜。
- 普通新角色按 ID 差分和真实角色技能引用解析。未来开放的主表角色仅标作预载/预览；不靠技能 ID 前缀猜技能，也不把“最近七天开放”替代 ID 差分。

## 当前部署依赖与边界

本流程只发布检测器、测试、工作流和使用说明，不混入本地其它素材/音乐改动。

2026-10-29 已核对远端 tracker `master` 的 `58e73b8`：维护版 split 生成器、官方初始速度和 `cloud_verify_cards.py` 已上线。本工作流复用它们，不能静默回退到旧竖版模板。检测器与技能 artifact 不因海报任务失败而撤回。

工作流需合入 GitHub 默认分支并确认启用。真实云端网络、排队和海报素材完整性由实际 run 验证，不能用本地 fake-clock 测试代替。

首次上线时复用已有云端任务结果。现有 assets updater 使用另一 concurrency group，可能独立运行；本流程不写 assets 仓库，因此不与其 Git 推送抢写。

## 部署与本次维护验收

1. 在最新 `origin/master` 的独立工作树审查上述文件，运行 `python -B -m unittest discover -s tests -v` 并检查暂存路径；只推送检测流程所需源码、测试和说明。
2. 确认 `watch-maintenance.yml` 在默认分支且处于 active，再通过 `gh workflow run watch-maintenance.yml --repo GuangShiX/mmtm-apk-tracker --ref master` 启动一次云端验收，避免等待 schedule 排队。
3. `plan` 必须从官方公告识别当前维护窗。`skills` 的 `maintenance-plan` 日志记录有效窗口与硬截止时间，后续每条 `version-probe` 记录实际 UTC 探测时间和 app/asset/master/catalog 签名；据此核对重复探测及真实更新，不能只看任务绿色。
4. 未变化时继续检测，不把冷缓存重建当成新角色。发现内容变化后检查 `character-skills-<runId>`：先有 `brief.md` 和派生角色 JSON，来源主表版本一致，原始 MasterBook 未进入 artifact；未实装角色仅进入独立 preview。
5. 有新增或预览角色时，`poster` 按同份技能 artifact 生成并校验 `3840×2500` 横版候选图，交付 `character-poster-candidates-<runId>`。图片内容与缺失素材仍需视觉验收，不能以脚本成功代替。
6. 分别记录“已部署”“云端持续检测已确认”“更新捕获与技能产物已确认”“海报已确认”。官方未更新时后两项保留待验收，不伪造更新触发。完整 APK 解包和私有模拟器接入是独立流程，不包含在本工作流的验收结论内。

## 朋友脚本的参考范围

用户提供的 `检技新池专秘v2声觉en.py` 只读审阅，未执行。可复用的是匿名 Master 版本/目录检测思路、角色/技能/专武/秘仪信息需求。CI 不照搬其 100 并发全量下载、宿主时区显示、失败批次推进基线、缓存旧版本冒充在线结果，以及仅按表名和版本判断变化的做法。
