<template>
  <div class="ui-page">
    <div class="ui-head">
      <h1>系统设置</h1>
      <span class="sub">存储目录 · AI 时段 · 加工分档 · 类型提示词 · 依赖诊断</span>
    </div>

    <!-- 页签头用 V3 样式；el-tabs 只保留内容切换能力（导航条由 CSS 隐藏） -->
    <div class="ui-tabs">
      <button v-for="t in tabs" :key="t.k" class="ui-tab" :class="{ on: tab === t.k }" @click="tab = t.k">
        {{ t.label }}
      </button>
    </div>
    <el-tabs v-model="tab" class="tabs-body">
      <!-- 存储目录 -->
      <el-tab-pane label="存储目录" name="storage">
        <p class="tip">素材保存根目录，支持本机路径、NAS 挂载，Windows 盘符或 UNC（如 Z:\xsai 或 \\NAS\xsai）。</p>
        <div class="bar">
          <button class="ui-btn primary" @click="storageDialog = true">新增根目录</button>
          <button class="ui-btn" :disabled="scanningOrphans || !roots.length" @click="scanOrphans">
            {{ scanningOrphans ? '扫描中…' : '扫描孤儿文件' }}
          </button>
          <span class="grow" />
          <span class="ui-badge">共 {{ roots.length }}</span>
        </div>
        <section class="ui-panel table-panel" v-loading="loadingRoots">
          <div v-if="!loadingRoots && !roots.length" class="ui-empty">
            <div class="big">还没有配置存储根目录</div>
            <p>选一个目录作为素材落盘位置，下载与转录都会写到这里。</p>
          </div>
          <table v-else class="ui-table">
            <thead>
              <tr>
                <th style="width: 150px">名称</th>
                <th>路径</th>
                <th style="width: 80px">默认</th>
                <th style="width: 200px">可用性</th>
                <th style="width: 90px; text-align: right">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in roots" :key="row.id">
                <td class="strong">{{ row.name }}</td>
                <td class="mono ellip" :title="row.path">{{ row.path }}</td>
                <td><span v-if="row.is_default" class="ui-badge acc">默认</span><span v-else class="muted">—</span></td>
                <td>
                  <span class="ui-badge" :class="row.available ? 'ok' : 'bad'">{{ row.available ? '可用' : '不可用' }}</span>
                  <span class="detail">{{ row.detail }}</span>
                </td>
                <td style="text-align: right">
                  <button class="ui-btn ghost sm danger" @click="delRoot(row)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
        </section>
        <section v-if="orphanReport" class="ui-panel">
          <div class="ui-panel-head">
            <h2>孤儿文件报告</h2>
            <span class="ui-badge" :class="orphanReport.orphan_count ? 'warn' : 'ok'">
              {{ orphanReport.orphan_count }} 个
            </span>
            <span class="grow" />
            <button class="ui-btn ghost sm" @click="orphanReport = null">关闭</button>
          </div>
          <div class="ui-panel-body">
            <p class="tip" style="margin-top: 0">只读报告，不会自动删除。相对路径如下：</p>
            <ul v-if="orphanReport.orphans.length" class="orphan-list">
              <li v-for="path in orphanReport.orphans" :key="path"><code>{{ path }}</code></li>
            </ul>
            <p v-else class="ui-empty" style="padding: 8px 0">没有发现孤儿文件。</p>
          </div>
        </section>
      </el-tab-pane>

      <!-- 模型接入快捷入口：模型配置已独立成一级页面，避免两套入口并存 -->
      <el-tab-pane label="模型接入" name="llm">
        <section class="ui-panel">
          <div class="ui-panel-head">
            <div>
              <h2>模型接入</h2>
              <div class="tip" style="margin: 4px 0 0">导入供应商模型、检测能力，并给文字分析、封面判类、视频理解分别配置主备模型。</div>
            </div>
            <span class="grow" />
            <router-link class="ui-btn primary" to="/models">打开模型接入</router-link>
          </div>
          <div class="ui-panel-body">
            <p class="tip" style="margin: 0">系统会自动推荐合适模型；只有你手动改过的工作分配才会被视为人工配置。</p>
          </div>
        </section>
      </el-tab-pane>

      <!-- 评论抓取 -->
      <el-tab-pane label="评论抓取" name="comments">
        <p class="tip">抖音评论富集门控与抓取参数。仅对“够热”的作品抓评论，走 worker 队列，可随时调整。</p>
        <el-form label-width="200px" style="max-width: 640px">
          <el-form-item label="热门分位 top_pct">
            <el-input-number v-model="commentSettings.top_pct" :min="0" :max="1" :step="0.05" :precision="2" />
            <span class="detail">批内按互动分取前该比例的作品（0~1）</span>
          </el-form-item>
          <el-form-item label="单批预算 budget">
            <el-input-number v-model="commentSettings.budget" :min="0" :precision="0" />
            <span class="detail">每批最多入队多少个作品</span>
          </el-form-item>
          <el-form-item label="最低评论数 min_comments">
            <el-input-number v-model="commentSettings.min_comments" :min="0" :precision="0" />
          </el-form-item>
          <el-form-item label="最低点赞数 min_likes">
            <el-input-number v-model="commentSettings.min_likes" :min="0" :precision="0" />
          </el-form-item>
          <el-form-item label="评论互动权重 weight">
            <el-input-number v-model="commentSettings.weight" :min="0" :step="1" :precision="1" />
            <span class="detail">互动分 = 点赞 + 评论数 × weight</span>
          </el-form-item>
          <el-form-item label="抓取页数 pages">
            <el-input-number v-model="commentSettings.pages" :min="1" :precision="0" />
          </el-form-item>
          <el-form-item label="每页条数 per_page">
            <el-input-number v-model="commentSettings.per_page" :min="1" :precision="0" />
          </el-form-item>
          <el-form-item label="取 Top-N top_n">
            <el-input-number v-model="commentSettings.top_n" :min="1" :precision="0" />
            <span class="detail">本地按点赞排序后保留的条数</span>
          </el-form-item>
          <el-form-item label="翻页间隔（秒）interval">
            <el-input-number v-model="commentSettings.interval" :min="0" :step="0.5" :precision="2" />
          </el-form-item>
          <el-form-item label="抓取二级评论">
            <el-switch v-model="commentSettings.with_sub_comments" />
            <span class="detail">开启后每平台会额外抓取一级评论下的回复（请求量更大，默认关闭）</span>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="saveCommentSettings">保存</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- AI 任务时段 -->
      <el-tab-pane label="AI 时段" name="ai-schedule">
        <p class="tip">限制需要调用大模型的任务在哪些时段开始执行。窗口外的任务会留在队列里，到时段自动继续；已经开始的调用会跑完。</p>
        <section class="ui-panel">
          <div class="ui-panel-head">
            <div>
              <h2>AI 任务可运行时段</h2>
              <div class="tip" style="margin: 4px 0 0">适用于 AI 速览、视频理解（含判类）和爆款拆解；下载、转录、采集不受影响。</div>
            </div>
            <span class="grow" />
            <span class="ui-badge" :class="aiSchedule.is_open ? 'ok' : 'warn'">
              {{ aiSchedule.is_open ? '当前开放' : '当前等待' }}
            </span>
          </div>
          <div class="ui-panel-body">
            <el-form label-width="140px" style="max-width: 760px">
              <el-form-item label="启用时段限制">
                <el-switch v-model="aiSchedule.enabled" />
                <span class="detail">关闭时不限制，AI 任务随时开始</span>
              </el-form-item>
              <el-form-item label="时区">
                <el-select v-model="aiSchedule.timezone" style="width: 220px">
                  <el-option label="Asia/Shanghai（北京时间）" value="Asia/Shanghai" />
                  <el-option label="UTC" value="UTC" />
                </el-select>
              </el-form-item>
            </el-form>

            <div class="ai-window-head">
              <span class="strong">时段</span>
              <span class="grow" />
              <button class="ui-btn sm" type="button" @click="addAiWindow">新增时段</button>
            </div>
            <div v-if="!aiSchedule.windows.length" class="ui-empty ai-window-empty">
              <div class="big">还没有时段</div>
              <p>启用限制前先添加一个时段。</p>
            </div>
            <div v-else class="ai-window-list">
              <div v-for="(w, idx) in aiSchedule.windows" :key="idx" class="ai-window-row">
                <div class="ai-window-days">
                  <button
                    v-for="(d, di) in WEEKDAYS"
                    :key="d.value"
                    type="button"
                    class="ai-day"
                    :class="{ on: w.days.includes(d.value) }"
                    @click="toggleAiDay(w, d.value)"
                  >{{ d.label }}</button>
                </div>
                <span class="ai-window-sep">从</span>
                <el-time-select v-model="w.start" start="00:00" step="00:30" end="23:30" style="width: 110px" />
                <span class="ai-window-sep">到</span>
                <el-time-select v-model="w.end" start="00:00" step="00:30" end="23:30" style="width: 110px" />
                <span v-if="w.start >= w.end" class="ui-badge warn">跨午夜</span>
                <span class="grow" />
                <button class="ui-btn sm ghost danger" type="button" @click="removeAiWindow(idx)">删除</button>
              </div>
            </div>

            <p class="tip ai-window-note">
              跨午夜时，后半天仍算在开始日：例如周一 23:00–07:00 包含周二凌晨。
              <span v-if="aiSchedule.summary?.length">当前规则：{{ aiSchedule.summary.join('；') }}（{{ aiSchedule.timezone }}）。</span>
              {{ aiSchedule.next_open_at ? `下一次开放：${aiSchedule.next_open_local || fmtAiTime(aiSchedule.next_open_at)}` : '' }}
            </p>
            <el-alert
              v-if="aiSchedule.enabled && aiSchedule.windows.length === 0"
              type="warning"
              :closable="false"
              show-icon
              title="启用限制前请至少添加一个时段，否则保存会失败。"
              style="margin-bottom: 14px"
            />
            <div class="ui-bar">
              <button class="ui-btn primary" type="button" :disabled="!aiScheduleDirty" @click="saveAiSchedule">保存</button>
              <span class="detail">队列页会同步显示当前开放状态。</span>
            </div>
          </div>
        </section>
      </el-tab-pane>

      <!-- 视频理解 -->
      <el-tab-pane label="视频理解" name="process">
        <p class="tip">素材分档加工门控（普通=转录 / 高级=视频理解）与类型提示词。阈值存 system_setting，可在线调整，不写死。</p>
        <el-form label-width="210px" style="max-width: 680px">
          <el-form-item label="高级档热度阈值 min_heat">
            <el-input-number v-model="proc.min_heat" :min="0" :precision="0" />
          </el-form-item>
          <el-form-item label="最低点赞下限 min_likes_floor">
            <el-input-number v-model="proc.min_likes_floor" :min="0" :precision="0" />
          </el-form-item>
          <el-form-item label="单批预算 budget">
            <el-input-number v-model="proc.budget" :min="0" :precision="0" />
            <span class="detail">全局限额：高级档在途 job（含 queued/running）上限；单条下载路径也受约束</span>
          </el-form-item>
          <el-form-item label="热门分位 top_pct">
            <el-input-number v-model="proc.top_pct" :min="0" :max="1" :step="0.05" :precision="2" />
            <span class="detail">仅批量路径（采集/同步）生效：每批按热度取前该比例升档；单条下载路径不适用</span>
          </el-form-item>
          <el-form-item label="点赞权重 weight_like">
            <el-input-number v-model="proc.weight_like" :min="0" :step="0.1" :precision="2" />
          </el-form-item>
          <el-form-item label="评论权重 weight_comment">
            <el-input-number v-model="proc.weight_comment" :min="0" :step="0.1" :precision="2" />
          </el-form-item>
          <el-form-item label="播放权重 weight_play">
            <el-input-number v-model="proc.weight_play" :min="0" :step="0.1" :precision="2" />
          </el-form-item>
          <el-form-item label="视频内联上限 video_max_mb">
            <el-input-number v-model="proc.video_max_mb" :min="0" :step="1" :precision="2" />
            <span class="detail">
              超过上限会按「短边」逐级压缩：720 → 540 → 480 像素，码率按时长反算以保证落在上限内。
              实测短边 ≥480 时画面文字仍可读，≤360 时 24px 小字基本读不出来；
              压得越狠，视频理解的画面描述越不可靠。模型支持大请求时建议调高。
            </span>
          </el-form-item>

          <div class="proc-group">
            <div class="proc-group-head">
              <span class="proc-group-title">长视频分块理解</span>
              <span class="proc-group-note">超长视频整片压不进上限，切成带重叠的块分别理解，再汇总成整体结论。</span>
            </div>
            <el-form-item label="分块阈值（秒） chunk_threshold_sec">
              <el-input-number v-model="proc.chunk_threshold_sec" :min="0" :step="60" :precision="0" />
              <span class="detail">
                超过该时长就分块（默认 600 秒）。填 0 = 关闭分块，所有视频走单次请求。
                18 分钟以上的视频不分块几乎必然压到不可用的画质。
              </span>
            </el-form-item>
            <el-form-item label="单块时长（秒） chunk_seconds">
              <el-input-number v-model="proc.chunk_seconds" :min="30" :step="30" :precision="0" />
              <span class="detail">每块的目标长度（默认 300 秒）。块越多越贵，块越小单块画质越好。</span>
            </el-form-item>
            <el-form-item label="块间重叠（秒） chunk_overlap_sec">
              <el-input-number v-model="proc.chunk_overlap_sec" :min="0" :step="5" :precision="0" />
              <span class="detail">相邻块重复的秒数（默认 15），给跨块的句子留上下文；重叠区的事件合并时自动去重。</span>
            </el-form-item>
          </div>

          <div class="proc-group">
            <div class="proc-group-head">
              <span class="proc-group-title">视听信号</span>
              <span class="proc-group-note">ffmpeg 本地测量响度 / 镜头切换 / 运动量，给「高能区在哪」提供客观依据。</span>
            </div>
            <el-form-item label="采集视听信号 signals_enabled">
              <el-switch v-model="proc.signals_enabled" />
              <span class="detail">
                纯本地解码，不产生模型费用。开启后每片会多花几十秒到几分钟（取决于时长），
                换来的是：不懂台词的跳舞/游戏/萌宠类素材也有可定位的重点时段。
              </span>
            </el-form-item>
          </div>

          <div class="proc-group">
            <div class="proc-group-head">
              <span class="proc-group-title">两级漏斗（省钱）</span>
              <span class="proc-group-note">先让便宜的小模型圈出可能有爆点的区间，云端大模型只精读入围部分。</span>
            </div>
            <el-form-item label="启用两级漏斗 prefilter_enabled">
              <el-switch v-model="proc.prefilter_enabled" />
              <span class="detail">
                默认关闭。需要先在「模型接入 → 工作分配」给「初筛」槽位配一个便宜模型
                （本地 Ollama / llama.cpp 都行）。任何失败都会自动回退全文，不会让任务失败。
              </span>
            </el-form-item>
            <el-form-item label="启用阈值（字符） prefilter_min_chars">
              <el-input-number v-model="proc.prefilter_min_chars" :min="0" :step="500" :precision="0" />
              <span class="detail">转录短于该长度直接送云端，初筛的固定开销不划算。</span>
            </el-form-item>
            <el-form-item label="初筛超时（秒） prefilter_timeout_sec">
              <el-input-number v-model="proc.prefilter_timeout_sec" :min="1" :step="10" :precision="0" />
              <span class="detail">超时即回退全文；本地小模型冷启动通常要十几秒。</span>
            </el-form-item>
          </div>
          <el-form-item label="封面内联上限 cover_max_mb">
            <el-input-number v-model="proc.cover_max_mb" :min="0" :step="1" :precision="2" />
          </el-form-item>
          <el-form-item label="低置信度阈值">
            <el-input-number v-model="proc.low_confidence_threshold" :min="0" :max="1" :step="0.05" :precision="2" />
          </el-form-item>
          <el-form-item label="开启判类 classify_enabled">
            <el-switch v-model="proc.classify_enabled" />
          </el-form-item>
          <el-form-item label="判类默认用封面 use_cover">
            <el-switch v-model="proc.use_cover" />
          </el-form-item>
          <el-form-item label="自动转录新素材">
            <el-switch v-model="proc.auto_transcribe" />
            <span class="detail">全局默认：素材下载完成后自动转录；订阅里可单独覆盖</span>
          </el-form-item>
          <el-form-item label="自动生成速览">
            <el-switch v-model="proc.auto_analyze" />
            <span class="detail">全局默认：转录后自动生成摘要 + 标签（速览）；深度爆款拆解仍需手动触发。订阅里可单独覆盖</span>
          </el-form-item>
          <el-alert type="info" :closable="false" show-icon style="margin-bottom: 14px"
            title="视频理解使用的模型已在「模型接入 → 工作分配」中设置。" />
          <el-form-item label="类型目录 taxonomy（JSON）">
            <el-input v-model="proc.taxonomyText" type="textarea" :rows="6" spellcheck="false" />
            <span class="detail">枚举 → 中文，可增删类型</span>
          </el-form-item>
          <el-form-item label="类型提示词 type_prompts（JSON）">
            <el-input v-model="proc.typePromptsText" type="textarea" :rows="10" spellcheck="false" />
            <span class="detail">类型 → {focus, dimensions:[{key,name,ask}]}，保存前本地校验</span>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="saveProcessSettings">保存</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- 爆款拆解 -->
      <el-tab-pane label="爆款拆解" name="breakdown">
        <p class="tip">自动爆款判定「阀门」+ 命中后的自动加工链。视频下载完成后，命中任一/全部阀门的才会被自动拆解。</p>
        <el-form label-width="200px" style="max-width: 720px">
          <el-divider content-position="left">谁来做拆解</el-divider>
          <el-form-item label="执行器 executor">
            <el-radio-group v-model="bd.executor">
              <el-radio-button value="external">外部 Agent</el-radio-button>
              <el-radio-button value="internal">平台自己跑</el-radio-button>
            </el-radio-group>
            <span class="detail" style="display:block;margin-top:6px">
              {{ bd.executor === 'external'
                ? '素材会排进外部队列等 Agent 认领，平台只负责转录、视频理解与发料。'
                : '平台调用自己的模型产出报告，不排队等外部。' }}
            </span>
          </el-form-item>
          <p v-if="bd.executor === 'external'" class="tip executor-warn">
            改为外部后，平台不再自动产出拆解报告。没有 Agent 在线认领时，素材会一直排队；
            想恢复自动出报告，把执行器切回「平台自己跑」。
          </p>
          <template v-if="bd.executor === 'external'">
            <el-form-item label="认领租约（分钟）">
              <el-input-number v-model="bd.claim_lease_min" :min="1" :max="1440" :precision="0" />
              <span class="detail">Agent 认领后多久没回传算失联，可被其他 Agent 接管（默认 30）</span>
            </el-form-item>
            <el-form-item label="最多认领次数">
              <el-input-number v-model="bd.max_attempts" :min="1" :max="20" :precision="0" />
              <span class="detail">含接管。用尽后见下面的兜底开关，不允许一直静默排队</span>
            </el-form-item>
            <el-form-item label="无人回传时平台兜底">
              <el-switch v-model="bd.internal_fallback" />
              <span class="detail">
                {{ bd.internal_fallback
                  ? '打开：次数用尽后平台自己补跑一次，报告里会注明是兜底产出'
                  : '关闭：次数用尽后落成一条可见的失败记录，平台不会自己掏钱跑' }}
              </span>
            </el-form-item>
          </template>

          <el-divider content-position="left">爆款阀门（都是"越大越像爆款"）</el-divider>
          <el-form-item label="相对自身中位数倍数">
            <el-input-number v-model="bd.viral_multiple" :min="0.1" :step="0.5" :precision="1" />
            <span class="detail">播放 ≥ 该账号近期播放中位数 × 倍数（默认 3；作者无数据时不参与）</span>
          </el-form-item>
          <el-form-item label="播放量门槛">
            <el-input-number v-model="bd.min_play" :min="0" :precision="0" />
            <span class="detail">0 = 不限</span>
          </el-form-item>
          <el-form-item label="点赞量门槛">
            <el-input-number v-model="bd.min_like" :min="0" :precision="0" />
            <span class="detail">0 = 不限</span>
          </el-form-item>
          <el-form-item label="互动率门槛">
            <el-input-number v-model="bd.min_engagement_rate" :min="0" :max="1" :step="0.01" :precision="2" />
            <span class="detail">(点赞+评论+收藏+分享)/播放 ≥ 该值；0 = 不限</span>
          </el-form-item>
          <el-form-item label="发布时间窗口（天）">
            <el-input-number v-model="bd.publish_within_days" :min="0" :precision="0" />
            <span class="detail">只判定最近 N 天内发布的作品；0 = 不限</span>
          </el-form-item>
          <el-form-item label="命中规则">
            <el-radio-group v-model="bd.match_rule">
              <el-radio-button value="any">任一命中</el-radio-button>
              <el-radio-button value="all">全部满足</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="转录字符上限 transcript_limit">
            <el-input-number v-model="bd.transcript_limit" :min="1000" :max="200000" :step="2000" :precision="0" />
            <span class="detail">
              送入拆解的转录最多多少字符（默认 24000，约 20 分钟口播）。超出的部分会被截断，
              并在提示词里明确告知模型「已截断」，避免它把缺失当成视频结尾。
            </span>
          </el-form-item>
          <el-form-item label="单次超时秒数 timeout_sec">
            <el-input-number v-model="bd.timeout_sec" :min="30" :max="1800" :step="30" :precision="0" />
            <span class="detail">
              拆解一次最多等多久（默认 300 秒）。带推理的模型实测要 80~240 秒，
              设得太短会把「正在思考」当成失败并切备用模型，白花一次调用。
            </span>
          </el-form-item>
          <el-divider content-position="left">命中后自动执行</el-divider>
          <el-form-item label="自动转录">
            <el-switch v-model="bd.auto_transcribe" />
            <span class="detail">理解 / 拆解都依赖转录，命中时通常需要</span>
          </el-form-item>
          <el-form-item label="自动视频理解">
            <el-switch v-model="bd.auto_understand" />
          </el-form-item>
          <el-form-item label="自动爆款拆解">
            <el-switch v-model="bd.auto_breakdown" />
            <span class="detail">
              {{ bd.executor === 'external'
                ? '命中阀门后自动排进外部队列（不消耗平台模型额度）'
                : '命中阀门后自动出拆解报告（消耗 LLM 额度）' }}
            </span>
          </el-form-item>
          <el-divider content-position="left">爆点判据（按影响力排序）</el-divider>
          <p class="tip" style="margin-bottom: 10px">
            模型按这张表逐条对照找爆点，并在报告里回报命中了哪几条、在哪一段、依据是什么。
            顺序即优先级：越靠前越被当作爆点主因。
          </p>
          <div class="criteria-list">
            <div v-for="(c, idx) in hookCriteria" :key="idx" class="criteria-row">
              <span class="criteria-rank">{{ idx + 1 }}</span>
              <div class="criteria-fields">
                <div class="criteria-top">
                  <el-input v-model="c.name" placeholder="判据名称（如 钩子时刻）" class="criteria-name" />
                  <el-input v-model="c.id" placeholder="id（英文，报告里引用它）" class="criteria-id" />
                </div>
                <el-input v-model="c.ask" placeholder="判定说明：什么样算命中" />
              </div>
              <div class="criteria-acts">
                <button class="ui-btn sm ghost" type="button" :disabled="idx === 0" @click="moveCriterion(idx, -1)">上移</button>
                <button class="ui-btn sm ghost" type="button" :disabled="idx === hookCriteria.length - 1" @click="moveCriterion(idx, 1)">下移</button>
                <button class="ui-btn sm ghost danger" type="button" :disabled="hookCriteria.length <= 1" @click="removeCriterion(idx)">删除</button>
              </div>
            </div>
            <p v-if="!hookCriteria.length" class="criteria-empty">至少保留一条判据。</p>
          </div>
          <div class="ui-bar" style="margin: 10px 0 18px">
            <button class="ui-btn sm" type="button" @click="addCriterion">+ 新增判据</button>
            <button class="ui-btn sm ghost" type="button" @click="resetCriteria">恢复默认 8 条</button>
            <span class="detail">判据 id 必须唯一；改 id 会让历史报告里的 hook_matches 对不上，尽量只改名称与说明。</span>
          </div>

          <el-divider content-position="left">拆解报告提示词</el-divider>
          <el-form-item label="拆解提示词">
            <el-input v-model="bd.prompt" type="textarea" :rows="12" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="saveBreakdownSettings">保存</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- 同步频率 -->
      <el-tab-pane label="同步频率" name="sync">
        <p class="tip">同步节奏与批量。同步时间 / 每次数量都是「随机区间」，模拟人工、避开固定节奏被风控（照抄抖小云）。</p>
        <el-form label-width="220px" style="max-width: 720px">
          <el-form-item label="同步间隔下限（分钟）">
            <el-input-number v-model="sync.interval_min_minutes" :min="1" :precision="0" />
            <span class="detail">订阅未单独设置间隔时，用这个全局随机区间</span>
          </el-form-item>
          <el-form-item label="同步间隔上限（分钟）">
            <el-input-number v-model="sync.interval_max_minutes" :min="1" :precision="0" />
            <span class="detail">每次到期后在该区间内随机取一个间隔</span>
          </el-form-item>
          <el-form-item label="每次同步数量下限">
            <el-input-number v-model="sync.batch_min" :min="1" :max="30" :precision="0" />
          </el-form-item>
          <el-form-item label="每次同步数量上限">
            <el-input-number v-model="sync.batch_max" :min="1" :max="30" :precision="0" />
            <span class="detail">单次拉取/下载的条数范围，上限 30（对齐抖小云 BatchCount）</span>
          </el-form-item>
          <el-form-item label="下载间隔下限（秒）">
            <el-input-number v-model="sync.download_gap_min_seconds" :min="0" :step="0.5" :precision="1" />
          </el-form-item>
          <el-form-item label="下载间隔上限（秒）">
            <el-input-number v-model="sync.download_gap_max_seconds" :min="0" :step="0.5" :precision="1" />
            <span class="detail">相邻两次下载之间的随机等待，避开密集直链请求</span>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="saveSyncSettings">保存</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- 诊断 -->
      <el-tab-pane label="依赖诊断" name="diag">
        <el-button @click="loadDiag">重新检测</el-button>
        <el-table :data="checks" v-loading="loadingDiag" stripe style="margin-top: 12px">
          <el-table-column label="检查项" width="160">
            <template #default="{ row }">{{ row.name }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.ok ? 'success' : (row.required === false ? 'warning' : 'danger')" size="small">
                {{ row.ok ? 'OK' : (row.required === false ? '可选' : '异常') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="280">
            <template #default="{ row }">
              <div>{{ row.detail }}</div>
              <div v-if="!row.ok && row.fix" class="hint">{{ row.fix }}</div>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- 管理员 -->
      <!-- 版本与升级 -->
      <el-tab-pane label="版本与升级" name="version">
        <el-descriptions :column="2" border style="max-width: 720px">
          <el-descriptions-item label="应用版本">{{ ver.app_version || '—' }}</el-descriptions-item>
          <el-descriptions-item label="提交">{{ ver.git_commit || '—' }}</el-descriptions-item>
          <el-descriptions-item label="版本来源">
            {{ ver.version_source === 'release_image' ? '发行镜像' : '源码运行' }}
          </el-descriptions-item>
          <el-descriptions-item label="数据库版本">{{ ver.db_revision || '—' }}</el-descriptions-item>
          <el-descriptions-item label="内置迁移 head">{{ ver.db_head || '—' }}</el-descriptions-item>
          <el-descriptions-item label="数据库同步">
            <el-tag :type="ver.db_in_sync ? 'success' : 'warning'" size="small" effect="dark">
              {{ ver.db_in_sync ? '已同步' : '未同步' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="构建时间">{{ ver.build_time || '—' }}</el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="update.available && update.url"
          type="warning"
          :closable="false"
          style="margin-top: 12px; max-width: 720px"
          title="发现新版本"
          :description="`最新 ${update.latest}，当前 ${update.current}。查看发布说明：${update.url}`"
          show-icon
        />

        <el-alert
          v-if="update.available && !update.expected_commit"
          type="warning"
          :closable="false"
          style="margin-top: 12px; max-width: 720px"
          title="无法确认发行提交"
          description="已发现新版本，但未能从 Release 解析对应 commit；升级将只校验版本字符串。建议稍后重试检查更新。"
          show-icon
        />

        <el-alert
          v-if="update.checkedAt && update.ok === false"
          type="error"
          :closable="false"
          style="margin-top: 12px; max-width: 720px"
          title="检查更新失败"
          :description="update.message || '无法获取最新版本，请稍后重试'"
          show-icon
        />

        <div style="margin-top: 20px; max-width: 720px">
          <el-alert
            v-if="!upgradeStatus.enabled"
            type="info"
            :closable="false"
            show-icon
            title="网页一键升级未启用"
            description="管理员未配置升级边车。请按 docs/upgrade-compatibility.md 用命令行升级（先 ./scripts/backup.sh，再 ./scripts/upgrade.sh <版本>）。"
          />
          <template v-else>
            <el-alert
              type="warning"
              :closable="false"
              show-icon
              title="网页升级会自动备份，失败自动回滚"
              description="升级流程：备份 → 拉取镜像 → 重建服务 → 健康校验；任一步失败会自动回滚到当前版本。过程中服务会短暂不可用。"
            />
            <div style="margin-top: 12px">
              <el-button
                v-if="upgradeTarget"
                type="primary"
                :loading="upgrading"
                :disabled="upgradeRunning"
                @click="confirmUpgrade"
              >升级到 {{ upgradeTarget }}</el-button>
              <el-button v-else type="primary" :loading="checkingUpdate" @click="checkUpdate">检查更新</el-button>
              <el-tag
                v-if="update.latest"
                :type="update.available ? 'warning' : 'success'"
                size="small"
                style="margin-left: 12px"
              >{{ update.available ? `有新版本 ${update.latest}` : '已是最新版本' }}</el-tag>
              <el-tag
                v-if="upgradeJob?.expected_commit"
                :type="upgradeJob.commit_verified ? 'success' : 'warning'"
                size="small"
                style="margin-left: 8px"
              >{{ upgradeJob.commit_verified ? '提交已核验' : '仅核验版本' }}</el-tag>
              <span v-if="upgradeTarget" class="upgrade-hint">当前 {{ update.current || ver.app_version }}</span>
            </div>

            <!-- 升级进度 -->
            <div v-if="upgradeJob" style="margin-top: 16px">
              <el-tag
                :type="jobTagType(upgradeJob.status)"
                size="small"
                effect="dark"
              >{{ jobLabel(upgradeJob.status) }}</el-tag>
              <span style="margin-left: 8px; color: var(--el-text-color-secondary)">
                {{ upgradeJob.from_version || '当前' }} → {{ upgradeJob.version }}
              </span>
              <el-steps direction="vertical" :active="jobActive(upgradeJob)" style="margin-top: 12px">
                <el-step
                  v-for="s in upgradeJob.steps"
                  :key="s.step + s.at"
                  :title="stepLabel(s.step)"
                  :status="s.status === 'ok' ? 'success' : s.status === 'error' ? 'error' : 'process'"
                  :description="s.detail"
                />
              </el-steps>
              <el-alert
                v-if="upgradeJob.error"
                :type="upgradeJob.status === 'rolled_back' ? 'warning' : 'error'"
                :closable="false"
                show-icon
                :title="upgradeJob.error"
                style="margin-top: 12px"
              />
              <div v-if="upgradeJob.backup_path" style="margin-top: 8px; color: var(--el-text-color-secondary); font-size: 13px">
                备份：{{ upgradeJob.backup_path }}
              </div>
            </div>
          </template>
        </div>
      </el-tab-pane>

      <!-- 管理员 -->
      <el-tab-pane label="管理员" name="admin">
        <el-form label-width="120px" style="max-width: 480px">
          <el-form-item label="原密码">
            <el-input v-model="pwd.old_password" type="password" show-password />
          </el-form-item>
          <el-form-item label="新密码">
            <el-input v-model="pwd.new_password" type="password" show-password placeholder="至少 6 位" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="changePwd">修改密码</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>
    </el-tabs>

    <!-- 删除存储根确认 -->
    <div v-if="rootTarget" class="ui-modal" @click.self="rootTarget = null">
      <div class="ui-modal-box">
        <div class="ui-panel-head"><h2>删除存储根目录</h2></div>
        <div class="ui-modal-body">
          <p>确定删除「{{ rootTarget.name }}」({{ rootTarget.path }})？仍被账号、订阅或素材引用的根目录会被拒绝删除。</p>
        </div>
        <div class="ui-modal-foot">
          <button class="ui-btn" @click="rootTarget = null">取消</button>
          <button class="ui-btn danger" @click="doDelRoot">确认删除</button>
        </div>
      </div>
    </div>

    <!-- 新增存储根弹窗 -->
    <el-dialog v-model="storageDialog" title="新增存储根目录" width="480px">
      <el-form label-width="80px">
        <el-form-item label="名称">
          <el-input v-model="rootForm.name" placeholder="例如：本机素材 / NAS" />
        </el-form-item>
        <el-form-item label="路径">
          <div class="path-row">
            <el-input v-model="rootForm.path" placeholder="/mnt/nas/xsai-content" />
            <button type="button" class="ui-btn sm" @click="openPicker">浏览…</button>
          </div>
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="rootForm.is_default" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="storageDialog = false">取消</el-button>
        <el-button type="primary" @click="createRoot">保存</el-button>
      </template>
    </el-dialog>

    <!-- 目录浏览器：读取 backend/worker 进程可见的目录树（只读白名单），点选回填路径 -->
    <el-dialog v-model="pickerDialog" title="选择目录" width="580px" destroy-on-close append-to-body>
      <div class="picker">
        <div class="picker-roots">
          <span class="picker-label">根目录</span>
          <button
            v-for="r in fsRoots"
            :key="r.path"
            type="button"
            class="ui-btn sm"
            :class="{ on: browseRoot === r.path }"
            :disabled="!r.exists"
            :title="r.path + (r.exists ? '' : '（不可用）')"
            @click="loadDir(r.path)"
          >
            {{ rootLabel(r) }}
          </button>
          <span v-if="!fsRoots.length" class="picker-label">无可用根目录</span>
        </div>
        <div class="picker-crumbs">
          <button type="button" class="ui-btn sm ghost" :disabled="!browseParent" @click="loadDir(browseParent!)">
            ↑ 上一级
          </button>
          <span class="crumbs mono">
            <template v-for="(c, i) in crumbs" :key="c.path">
              <span v-if="i && !crumbs[i - 1].path.endsWith(browseSep)" class="sep">{{ browseSep }}</span>
              <a @click="loadDir(c.path)">{{ c.label }}</a>
            </template>
          </span>
        </div>
        <div class="picker-list" v-loading="loadingDir">
          <div v-if="!loadingDir && !browseEntries.length" class="picker-empty">此目录下没有子目录</div>
          <button
            v-for="e in browseEntries"
            :key="e.path"
            type="button"
            class="picker-item"
            @click="loadDir(e.path)"
          >
            <span class="pick-name">{{ e.name }}</span>
            <span class="grow" />
            <span v-if="!e.writable" class="ui-badge warn">只读</span>
          </button>
        </div>
        <div class="picker-manual">
          <el-input v-model="browseManual" placeholder="也可直接输入路径，如 /mnt/nas/xsai-content" size="small" />
          <button type="button" class="ui-btn sm" @click="loadDir(browseManual)">前往</button>
        </div>
      </div>
      <template #footer>
        <span class="picker-picked mono" :title="browsePath">{{ browsePath || '未选择目录' }}</span>
        <span class="grow" />
        <el-button @click="pickerDialog = false">取消</el-button>
        <el-button type="primary" :disabled="!browsePath" @click="chooseDir">选用此目录</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ElMessage } from 'element-plus/es/components/message/index.mjs'
import { ElMessageBox } from 'element-plus/es/components/message-box/index.mjs'
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiCall } from '../api'
import { formatMonthDayTime } from '../utils/time'

const router = useRouter()
const tab = ref('storage')
// 页签清单：与下面 el-tab-pane 的 name 一一对应
const tabs = [
  { k: 'storage', label: '存储目录' },
  { k: 'llm', label: '模型接入' },
  { k: 'comments', label: '评论抓取' },
  { k: 'ai-schedule', label: 'AI 时段' },
  { k: 'process', label: '视频理解' },
  { k: 'breakdown', label: '爆款拆解' },
  { k: 'sync', label: '同步频率' },
  { k: 'diag', label: '依赖诊断' },
  { k: 'version', label: '版本与升级' },
  { k: 'admin', label: '管理员' },
]
const roots = ref<any[]>([])
const checks = ref<any[]>([])
const loadingRoots = ref(false)
const scanningOrphans = ref(false)
const orphanReport = ref<{ orphan_count: number; orphans: string[] } | null>(null)
const loadingDiag = ref(false)
// ---- 版本与升级 ----
const ver = ref<any>({ app_version: '', git_commit: '', build_time: '', db_revision: '', db_head: '', db_in_sync: false })
const update = ref<any>({ current: '', latest: '', available: false, url: '', ok: null, message: '', checkedAt: 0 })
const upgradeStatus = ref<{ enabled: boolean; current: string }>({ enabled: false, current: '' })
const checkingUpdate = ref(false)
const upgrading = ref(false)
const upgradeJob = ref<any>(null)
// 升级目标 = 检查更新发现的最新版；用户从不手填版本号。
const upgradeTarget = computed(() => (update.value.available ? update.value.latest : ''))
// 一次升级正在跑（点击后到轮询结束前），期间禁止重复触发。
const upgradeRunning = computed(() => upgrading.value || upgradeJob.value?.status === 'running')
const storageDialog = ref(false)
const rootForm = reactive({ name: '', path: '', is_default: false })

// ---- 目录浏览器（只读白名单，读取 backend/worker 进程可见的目录树）----
const pickerDialog = ref(false)
const fsRoots = ref<any[]>([])
const browseRoot = ref('')
const browsePath = ref('')
const browseParent = ref<string | null>(null)
const browseSep = ref('/')
const browseEntries = ref<any[]>([])
const browseManual = ref('')
const loadingDir = ref(false)

// 面包屑：从根到当前路径逐级切分（Windows 保留盘符前缀）。
const crumbs = computed(() => {
  const p = browsePath.value || ''
  if (!p) return []
  const sep = browseSep.value || '/'
  const out: { label: string; path: string }[] = []
  if (/^[A-Za-z]:[\\/]/.test(p)) {
    const drive = p.slice(0, 2) + sep
    out.push({ label: p.slice(0, 2), path: drive })
    let acc = drive
    for (const seg of p.slice(3).split(/[\\/]+/).filter(Boolean)) {
      acc = acc.endsWith(sep) ? acc + seg : acc + sep + seg
      out.push({ label: seg, path: acc })
    }
    return out
  }
  out.push({ label: sep, path: sep })
  let acc = ''
  for (const seg of p.split(/[\\/]+/).filter(Boolean)) {
    acc = acc + sep + seg
    out.push({ label: seg, path: acc })
  }
  return out
})

function rootLabel(r: any) {
  return r.path === '/' ? '根 /' : r.path
}

async function loadFsRoots() {
  const data = await apiCall('list_fs_roots_api_system_fs_roots_get')
  fsRoots.value = data.roots || []
}

async function loadDir(path: string) {
  loadingDir.value = true
  try {
    const data = await apiCall('list_fs_dir_api_system_fs_list_get', { path })
    browsePath.value = data.path
    browseParent.value = data.parent || null
    browseSep.value = data.sep || '/'
    browseEntries.value = data.entries || []
    browseManual.value = data.path
    // 高亮当前所属根（用于根快捷入口的激活态）
    const sep = data.sep || '/'
    const hit = (data.roots || []).find(
      (r: any) => data.path === r.path || data.path.startsWith(r.path === '/' ? '/' : r.path + sep),
    )
    if (hit) browseRoot.value = hit.path
  } catch (e: any) {
    // 400（越界/不存在）由 api 拦截器统一提示；这里保持当前目录不变
  } finally {
    loadingDir.value = false
  }
}

async function openPicker() {
  pickerDialog.value = true
  browseManual.value = rootForm.path || ''
  await loadFsRoots()
  // 优先打开当前已填路径；不在白名单（例如非本机风格的路径）时回第一个可用根。
  if (rootForm.path) await loadDir(rootForm.path)
  if (!browsePath.value) {
    const first = fsRoots.value.find((r) => r.exists) || fsRoots.value[0]
    await loadDir(first ? first.path : '')
  }
}

function chooseDir() {
  if (!browsePath.value) return
  rootForm.path = browsePath.value
  if (!rootForm.name.trim()) {
    const seg = browsePath.value.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || browsePath.value
    rootForm.name = seg
  }
  pickerDialog.value = false
}

const pwd = reactive({ old_password: '', new_password: '' })
const commentSettings = reactive({
  top_pct: 0.2,
  budget: 10,
  min_comments: 20,
  min_likes: 100,
  weight: 20,
  pages: 2,
  per_page: 20,
  top_n: 20,
  interval: 1,
  with_sub_comments: false,
})

// ---- AI 任务时段 ----
const WEEKDAYS = [
  { value: 0, label: '一' },
  { value: 1, label: '二' },
  { value: 2, label: '三' },
  { value: 3, label: '四' },
  { value: 4, label: '五' },
  { value: 5, label: '六' },
  { value: 6, label: '日' },
]
const aiSchedule = reactive<any>({
  enabled: false,
  timezone: 'Asia/Shanghai',
  windows: [],
  summary: [],
  next_open_local: null,
  is_open: true,
  next_open_at: null,
})
const aiScheduleSaved = ref('')
const aiScheduleDirty = computed(() => JSON.stringify(aiScheduleSnapshot()) !== aiScheduleSaved.value)

function aiScheduleSnapshot() {
  return {
    enabled: !!aiSchedule.enabled,
    timezone: aiSchedule.timezone,
    windows: (aiSchedule.windows || []).map((w: any) => ({
      days: [...(w.days || [])].sort((a: number, b: number) => a - b),
      start: w.start,
      end: w.end,
    })),
  }
}

function addAiWindow() {
  aiSchedule.windows.push({ days: [0, 1, 2, 3, 4], start: '09:00', end: '18:00' })
}

function removeAiWindow(idx: number) {
  aiSchedule.windows.splice(idx, 1)
}

function toggleAiDay(w: any, day: number) {
  const at = w.days.indexOf(day)
  if (at >= 0) w.days.splice(at, 1)
  else w.days.push(day)
}

function fmtAiTime(value: string | null) {
  return value ? formatMonthDayTime(value, '') : ''
}

async function loadAiSchedule() {
  try {
    const data = await apiCall('get_ai_schedule_api_api_system_ai_schedule_get')
    aiSchedule.enabled = !!data.enabled
    aiSchedule.timezone = data.timezone || 'Asia/Shanghai'
    aiSchedule.windows = (data.windows || []).map((w: any) => ({ days: [...(w.days || [])], start: w.start, end: w.end }))
    aiSchedule.summary = data.summary || []
    aiSchedule.next_open_local = data.next_open_local || null
    aiSchedule.is_open = !!data.is_open
    aiSchedule.next_open_at = data.next_open_at || null
    aiScheduleSaved.value = JSON.stringify(aiScheduleSnapshot())
  } catch { /* 设置暂不可用时保留当前表单 */ }
}

async function saveAiSchedule() {
  if (aiSchedule.enabled && !aiSchedule.windows.length) {
    return ElMessage.warning('启用限制前请先添加时段')
  }
  if (aiSchedule.windows.some((w: any) => !w.days.length)) {
    return ElMessage.warning('每个时段至少要选择一天')
  }
  if (aiSchedule.windows.some((w: any) => w.start === w.end)) {
    return ElMessage.warning('时段的开始和结束不能相同')
  }
  try {
    const data = await apiCall('update_ai_schedule_api_api_system_ai_schedule_put', {}, {
      body: aiScheduleSnapshot(),
    })
    aiSchedule.is_open = !!data.schedule?.is_open
    aiSchedule.next_open_at = data.schedule?.next_open_at || null
    aiScheduleSaved.value = JSON.stringify(aiScheduleSnapshot())
    ElMessage.success('已保存')
    await loadAiSchedule()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  }
}
const proc = reactive<any>({
  min_heat: 1000,
  min_likes_floor: 100,
  budget: 20,
  top_pct: 0.2,
  weight_like: 1,
  weight_comment: 2,
  weight_play: 0,
  video_max_mb: 15,
  cover_max_mb: 5,
  chunk_threshold_sec: 600,
  chunk_seconds: 300,
  chunk_overlap_sec: 15,
  signals_enabled: true,
  prefilter_enabled: false,
  prefilter_min_chars: 3000,
  prefilter_timeout_sec: 120,
  low_confidence_threshold: 0.6,
  classify_enabled: true,
  use_cover: false,
  auto_transcribe: false,
  auto_analyze: false,
  taxonomyText: '{}',
  typePromptsText: '{}',
})

const bd = reactive<any>({
  prompt: '', viral_multiple: 3,
  min_play: 0, min_like: 0, min_engagement_rate: 0, publish_within_days: 0,
  match_rule: 'any', transcript_limit: 24000, timeout_sec: 300,
  auto_transcribe: true, auto_understand: false, auto_breakdown: false,
  executor: 'external', internal_fallback: true, claim_lease_min: 30, max_attempts: 3,
  hook_criteria: [] as HookCriterion[],
})

// v-for 直接遍历 reactive<any> 的字段时，索引会被推导成 string | number，
// 而排序/删除都要求 number。用带类型的 computed 收口。
const hookCriteria = computed<HookCriterion[]>(() => bd.hook_criteria as HookCriterion[])

// 内置 8 条与后端 app/core/hook_criteria.py:DEFAULT_HOOK_CRITERIA 保持一致。
// 只在「用户点恢复默认」和首次加载失败时用；正常路径永远读后端返回值。
interface HookCriterion { id: string; name: string; ask: string }

const DEFAULT_HOOK_CRITERIA: HookCriterion[] = [
  { id: 'hook', name: '钩子时刻', ask: '开头就制造好奇或反差的语句，让人立刻想知道后面讲什么' },
  { id: 'emotion_peak', name: '情绪峰值', ask: '真实的惊讶、大笑、愤怒、脆弱或兴奋，未经修饰的反应' },
  { id: 'opinion_bomb', name: '观点炸弹', ask: '强烈、有争议或反直觉的判断，让人想赞同或反驳' },
  { id: 'revelation', name: '反转/揭秘', ask: '出人意料的事实、数据或自白，改变观众原本的认知' },
  { id: 'conflict', name: '冲突/张力', ask: '分歧、反驳，或一个被正面处理的问题' },
  { id: 'quotable', name: '可摘金句', ask: '单独拿出来就能当引语卡的一句话' },
  { id: 'story_peak', name: '故事高潮', ask: '一段叙事的转折点或报酬时刻' },
  { id: 'practical', name: '实用价值', ask: '观众能立刻上手的具体技巧、方法或洞察' },
]

function addCriterion() {
  bd.hook_criteria.push({ id: `criterion_${bd.hook_criteria.length + 1}`, name: '', ask: '' })
}
function removeCriterion(idx: number) {
  if (bd.hook_criteria.length <= 1) return
  bd.hook_criteria.splice(idx, 1)
}
function moveCriterion(idx: number, delta: number) {
  const next = idx + delta
  if (next < 0 || next >= bd.hook_criteria.length) return
  const [item] = bd.hook_criteria.splice(idx, 1)
  bd.hook_criteria.splice(next, 0, item)
}
function resetCriteria() {
  bd.hook_criteria = DEFAULT_HOOK_CRITERIA.map((c) => ({ ...c }))
}

async function loadBreakdownSettings() {
  const data = await apiCall('get_breakdown_settings_api_api_system_breakdown_settings_get')
  bd.prompt = data.prompt ?? ''
  bd.viral_multiple = data.viral_multiple ?? 3
  bd.min_play = data.min_play ?? 0
  bd.min_like = data.min_like ?? 0
  bd.min_engagement_rate = data.min_engagement_rate ?? 0
  bd.publish_within_days = data.publish_within_days ?? 0
  bd.match_rule = data.match_rule ?? 'any'
  bd.transcript_limit = data.transcript_limit ?? 24000
  bd.timeout_sec = data.timeout_sec ?? 300
  bd.auto_transcribe = data.auto_transcribe ?? true
  bd.auto_understand = data.auto_understand ?? false
  bd.auto_breakdown = data.auto_breakdown ?? false
  bd.executor = data.executor === 'internal' ? 'internal' : 'external'
  bd.internal_fallback = data.internal_fallback ?? true
  bd.claim_lease_min = data.claim_lease_min ?? 30
  bd.max_attempts = data.max_attempts ?? 3
  bd.hook_criteria = Array.isArray(data.hook_criteria) && data.hook_criteria.length
    ? data.hook_criteria.map((c: any) => ({ id: c.id ?? '', name: c.name ?? '', ask: c.ask ?? '' }))
    : DEFAULT_HOOK_CRITERIA.map((c) => ({ ...c }))
}

function validateCriteria(): string {
  if (!hookCriteria.value.length) return '至少保留一条爆点判据'
  const seen = new Set<string>()
  for (const [i, c] of hookCriteria.value.entries()) {
    if (!c.id.trim() || !c.name.trim() || !c.ask.trim()) return `第 ${i + 1} 条判据的 id / 名称 / 说明都不能为空`
    if (!/^[A-Za-z0-9_-]+$/.test(c.id.trim())) return `第 ${i + 1} 条判据的 id 只能用英文、数字、下划线或短横线`
    if (seen.has(c.id.trim())) return `判据 id「${c.id}」重复了，每条必须唯一`
    seen.add(c.id.trim())
  }
  return ''
}

async function saveBreakdownSettings() {
  if (!bd.prompt || !bd.prompt.trim()) return ElMessage.warning('提示词不能为空')
  if (!(bd.viral_multiple > 0)) return ElMessage.warning('爆款倍数必须为正数')
  const criteriaError = validateCriteria()
  if (criteriaError) return ElMessage.warning(criteriaError)
  try {
    await apiCall('update_breakdown_settings_api_api_system_breakdown_settings_put', {}, { body: {
      prompt: bd.prompt,
      viral_multiple: bd.viral_multiple,
      min_play: bd.min_play,
      min_like: bd.min_like,
      min_engagement_rate: bd.min_engagement_rate,
      publish_within_days: bd.publish_within_days,
      match_rule: bd.match_rule,
      transcript_limit: bd.transcript_limit,
      timeout_sec: bd.timeout_sec,
      auto_transcribe: bd.auto_transcribe,
      auto_understand: bd.auto_understand,
      auto_breakdown: bd.auto_breakdown,
      executor: bd.executor,
      internal_fallback: bd.internal_fallback,
      claim_lease_min: bd.claim_lease_min,
      max_attempts: bd.max_attempts,
      hook_criteria: (bd.hook_criteria as HookCriterion[]).map((c: HookCriterion) => ({
        id: c.id.trim(), name: c.name.trim(), ask: c.ask.trim(),
      })),
    } })
    ElMessage.success('已保存')
    await loadBreakdownSettings()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  }
}

// 同步频率（照抄抖小云：随机时间 + 每次数量范围）
const sync = reactive<any>({
  interval_min_minutes: 300, interval_max_minutes: 360,
  batch_min: 10, batch_max: 18,
  download_gap_min_seconds: 2, download_gap_max_seconds: 10,
})

async function loadSyncSettings() {
  const data = await apiCall('get_sync_settings_api_api_system_sync_settings_get')
  Object.assign(sync, data)
}

async function saveSyncSettings() {
  if (sync.interval_min_minutes > sync.interval_max_minutes) {
    return ElMessage.warning('同步间隔下限不能大于上限')
  }
  if (sync.batch_min > sync.batch_max) {
    return ElMessage.warning('每次数量下限不能大于上限')
  }
  if (sync.download_gap_min_seconds > sync.download_gap_max_seconds) {
    return ElMessage.warning('下载间隔下限不能大于上限')
  }
  try {
    await apiCall('update_sync_settings_api_api_system_sync_settings_put', {}, { body: { ...sync } })
    ElMessage.success('已保存')
    await loadSyncSettings()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  }
}

async function loadRoots() {
  loadingRoots.value = true
  try {
    roots.value = await apiCall('list_storage_roots_api_system_storage_roots_get')
  } finally {
    loadingRoots.value = false
  }
}

async function scanOrphans() {
  scanningOrphans.value = true
  try {
    const data = await apiCall('storage_orphans_api_system_storage_roots_orphans_get')
    orphanReport.value = data
    ElMessage.success(data.orphan_count ? `发现 ${data.orphan_count} 个孤儿文件` : '没有发现孤儿文件')
  } finally {
    scanningOrphans.value = false
  }
}

async function createRoot() {
  if (!rootForm.name.trim() || !rootForm.path.trim()) return ElMessage.warning('名称和路径必填')
  await apiCall('create_storage_root_api_system_storage_roots_post', {}, { body: rootForm })
  storageDialog.value = false
  rootForm.name = ''
  rootForm.path = ''
  rootForm.is_default = false
  await loadRoots()
}

const rootTarget = ref<any>(null)

function delRoot(row: any) {
  rootTarget.value = row
}

async function doDelRoot() {
  if (!rootTarget.value) return
  try {
    await apiCall('delete_storage_root_api_system_storage_roots__root_id__delete', {
      root_id: rootTarget.value.id,
    })
    ElMessage.success('已删除存储根')
    rootTarget.value = null
    await loadRoots()
  } catch { /* axios 已提示引用数量/原因 */ }
}

async function loadDiag() {
  loadingDiag.value = true
  try {
    checks.value = (await apiCall('diagnostics_api_system_diagnostics_get')).checks
  } finally {
    loadingDiag.value = false
  }
}

async function loadVersion(): Promise<boolean> {
  try {
    ver.value = await apiCall('get_version_api_system_version_get')
    upgradeStatus.value = await apiCall('update_status_api_system_update_status_get')
    return true
  } catch (e) {
    /* 版本信息获取失败不阻断设置页其它功能 */
    return false
  }
}

async function checkUpdate() {
  checkingUpdate.value = true
  try {
    const data = await apiCall('check_update_api_system_update_check_get')
    if (data.ok === false) {
      // Keep the failure in the page instead of only showing a transient toast.
      // A rate-limited GitHub quota otherwise looks like the button did nothing.
      update.value = { ...data, checkedAt: Date.now() }
      ElMessage.warning(data.message || '检查更新失败')
      return
    }
    update.value = { ...data, checkedAt: Date.now() }
    if (data.available) ElMessage.success(`发现新版本 ${data.latest}`)
    else ElMessage.success('已是最新版本')
  } finally {
    checkingUpdate.value = false
  }
}

function jobLabel(status: string) {
  return (
    {
      running: '升级进行中', success: '升级成功', failed: '升级失败',
      rolled_back: '已自动回滚', partially_rolled_back: '回滚不完整',
    }[status] ||
    status
  )
}
function jobTagType(status: string) {
  return ({
    running: 'warning', success: 'success', failed: 'danger',
    rolled_back: 'warning', partially_rolled_back: 'danger',
  } as any)[status] || 'info'
}
function stepLabel(step: string) {
  return (
    {
      backup: '备份', pull: '拉取镜像', recreate: '重建服务', verify: '健康校验',
      pin: '记录版本', db_restore: '恢复数据库', data_restore: '恢复 data 卷', rollback: '回滚',
    } as any
  )[step] || step
}
function jobActive(job: any) {
  return (job?.steps || []).filter((s: any) => s.status === 'ok').length
}

let pollTimer: number | null = null
function stopPolling() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

// 轮询升级进度。主 API 升级中会短暂不可达，此时保持上次状态、继续轮询。
function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    try {
      const data = await apiCall('upgrade_status_api_system_upgrade_status_get')
      if (data?.job) upgradeJob.value = data.job
      const status = upgradeJob.value?.status
      if (status && status !== 'running') {
        stopPolling()
        if (status === 'success') ElMessage.success('升级成功，服务已更新')
        else if (status === 'rolled_back') ElMessage.warning('升级未成功，已自动回滚到原版本')
        else if (status === 'partially_rolled_back') ElMessage.error('回滚不完整：数据库与旧版本已恢复，data 卷仍需按备份人工恢复')
        else ElMessage.error('升级失败，请查看进度详情')
        // 升级结束后刷新版本信息
        await loadVersion()
      }
    } catch {
      /* 服务重启中，忽略，等待下次轮询 */
    }
  }, 2500)
}

async function confirmUpgrade() {
  const target = upgradeTarget.value
  if (!target) return ElMessage.warning('没有可升级的新版本，请先点「检查更新」')
  try {
    await ElMessageBox.confirm(
      `将升级到 ${target}${update.value.expected_commit ? `（提交 ${update.value.expected_commit.slice(0, 12)}）` : ''}。系统会先自动备份，然后拉取镜像并重建服务，期间服务短暂不可用；若新版本启动失败或提交不一致会自动回滚。是否继续？`,
      '确认升级',
      { type: 'warning', confirmButtonText: '开始升级', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  upgrading.value = true
  try {
    await apiCall('trigger_upgrade_api_system_upgrade_post', {}, { body: {
      version: target,
      expected_commit: update.value.expected_commit || '',
    } })
    ElMessage.info('已开始升级，下方会显示进度')
    startPolling()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '升级失败')
  } finally {
    upgrading.value = false
  }
}

async function changePwd() {
  if (pwd.new_password.length < 6) return ElMessage.warning('新密码至少 6 位')
  try {
    await apiCall('change_password_api_auth_change_password_post', {}, { body: pwd })
    // The server invalidates every old session. Drop the local token before
    // navigating so the login page cannot mount with a dead credential.
    localStorage.removeItem('token')
    pwd.old_password = ''
    pwd.new_password = ''
    ElMessage.success('密码已修改，请重新登录')
    router.push('/login')
  } catch { /* axios 已提示 */ }
}

async function loadCommentSettings() {
  Object.assign(commentSettings, await apiCall('get_comment_settings_api_api_system_comment_settings_get'))
}

async function saveCommentSettings() {
  await apiCall('update_comment_settings_api_api_system_comment_settings_put', {}, { body: commentSettings })
  ElMessage.success('已保存')
}

async function loadProcessSettings() {
  const data = await apiCall('get_process_settings_api_api_system_process_settings_get')
  Object.assign(proc, data, {
    taxonomyText: JSON.stringify(data.taxonomy ?? {}, null, 2),
    typePromptsText: JSON.stringify(data.type_prompts ?? {}, null, 2),
  })
}

async function saveProcessSettings() {
  let taxonomy: any
  let type_prompts: any
  try {
    taxonomy = JSON.parse(proc.taxonomyText)
    type_prompts = JSON.parse(proc.typePromptsText)
  } catch (e: any) {
    return ElMessage.error('JSON 解析失败：' + e.message)
  }
  await apiCall('update_process_settings_api_api_system_process_settings_put', {}, { body: {
    min_heat: proc.min_heat,
    min_likes_floor: proc.min_likes_floor,
    budget: proc.budget,
    top_pct: proc.top_pct,
    weight_like: proc.weight_like,
    weight_comment: proc.weight_comment,
    weight_play: proc.weight_play,
    video_max_mb: proc.video_max_mb,
    cover_max_mb: proc.cover_max_mb,
    chunk_threshold_sec: proc.chunk_threshold_sec,
    chunk_seconds: proc.chunk_seconds,
    chunk_overlap_sec: proc.chunk_overlap_sec,
    signals_enabled: proc.signals_enabled,
    prefilter_enabled: proc.prefilter_enabled,
    prefilter_min_chars: proc.prefilter_min_chars,
    prefilter_timeout_sec: proc.prefilter_timeout_sec,
    low_confidence_threshold: proc.low_confidence_threshold,
    classify_enabled: proc.classify_enabled,
    use_cover: proc.use_cover,
    auto_transcribe: proc.auto_transcribe,
    auto_analyze: proc.auto_analyze,
    taxonomy,
    type_prompts,
  } })
  ElMessage.success('已保存')
  await loadProcessSettings()
}

onMounted(async () => {
  loadRoots()
  loadDiag()
  loadCommentSettings()
  loadAiSchedule()
  loadProcessSettings()
  loadBreakdownSettings()
  loadSyncSettings()
  const versionLoaded = await loadVersion()
  // The updater lives in a sidecar and keeps the last job.  Recover it on a
  // page reload instead of making a completed upgrade look like it vanished.
  if (versionLoaded && upgradeStatus.value.enabled) startPolling()
})

onUnmounted(stopPolling)
</script>

<style scoped>
.bar { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; margin-bottom: var(--sp-3); }
.bar .grow { margin-left: auto; }
.path-row { display: flex; gap: 8px; }
.path-row :deep(.el-input) { flex: 1; }
.upgrade-hint { margin-left: 12px; color: var(--muted); font-size: var(--fs-sm); }
.mono { font-family: var(--font-mono); font-size: var(--fs-sm); }
.ellip { max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.muted { color: var(--muted); }

/* 目录浏览器 */
.picker { display: flex; flex-direction: column; gap: 10px; }
.picker-roots { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.picker-roots .ui-btn.on { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.picker-label { color: var(--muted); font-size: var(--fs-sm); }
.picker-crumbs { display: flex; align-items: center; gap: 8px; min-width: 0; }
.picker-crumbs .crumbs { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.picker-crumbs .crumbs a { cursor: pointer; color: var(--ink-2); }
.picker-crumbs .crumbs a:hover { color: var(--accent); text-decoration: underline; }
.picker-crumbs .sep { margin: 0 2px; }
.picker-list { border: 1px solid var(--line-soft); border-radius: var(--radius); max-height: 300px; min-height: 120px; overflow-y: auto; }
.picker-item {
  width: 100%; display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; background: transparent; border: 0; border-bottom: 1px solid var(--line-soft);
  font: inherit; color: var(--ink-2); text-align: left; cursor: pointer;
}
.picker-item:last-child { border-bottom: 0; }
.picker-item:hover { background: color-mix(in oklab, var(--accent) 6%, transparent); }
.picker-item .grow { margin-left: auto; }
.picker-item .pick-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.picker-empty { padding: 28px 12px; text-align: center; color: var(--muted); }
.picker-manual { display: flex; gap: 8px; }
.picker-manual :deep(.el-input) { flex: 1; }
.picker-picked { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px; }

/* 隐藏 el-tabs 自带的导航条，只保留内容切换能力 */
.tabs-body :deep(.el-tabs__header) { display: none; }

/* 删除确认弹层 */
.ui-modal {
  position: fixed; inset: 0; z-index: 200;
  background: color-mix(in oklab, var(--ink) 45%, transparent);
  display: grid; place-items: center; padding: var(--sp-5);
}
.ui-modal-box { background: var(--surface); border: 2px solid var(--ink); box-shadow: var(--shadow-2); width: min(520px, 100%); }
.ui-modal-body { padding: var(--sp-5); color: var(--ink-2); }
.ui-modal-body p { margin: 0; word-break: break-all; }
.ui-modal-foot {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: var(--sp-4) var(--sp-5); border-top: 1px solid var(--line-soft);
}
.tip {
  color: var(--muted);
  font-size: var(--fs-sm);
  margin: 4px 0 12px;
  line-height: 1.6;
}
/* 执行器切到外部是「平台会停止自动出报告」这种量级的改变，值得一条显眼提示，
   而不是混在说明文字里被扫过去。 */
.executor-warn {
  padding: 10px 14px;
  border: 1px solid var(--accent-line);
  background: var(--accent-soft);
  color: var(--ink-2);
  font-size: var(--fs-sm);
  line-height: 1.7;
}
.detail {
  color: #909399;
  font-size: 12px;
  margin-left: 8px;
}

.orphan-list {
  list-style: none; margin: 0; padding: 0; max-height: 280px; overflow: auto;
  border: 1px solid var(--line-soft); background: var(--surface-2);
}
.orphan-list li { padding: 7px 10px; border-bottom: 1px solid var(--line-soft); min-width: 0; }
.orphan-list li:last-child { border-bottom: 0; }
.orphan-list code { overflow-wrap: anywhere; }

/* AI 时段 */
.ai-window-head { display: flex; align-items: center; gap: 10px; margin: 4px 0 10px; }
.ai-window-head .grow { flex: 1; }
.ai-window-list { display: flex; flex-direction: column; gap: 8px; }
.ai-window-row {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 10px 12px; border: 1px solid var(--line-soft); background: var(--surface-2);
}
.ai-window-row .grow { flex: 1; }
.ai-window-days { display: flex; gap: 4px; }
.ai-day {
  width: 30px; height: 28px; padding: 0; cursor: pointer;
  border: 1px solid var(--line); background: var(--surface); color: var(--muted);
  font: inherit; font-size: var(--fs-sm);
}
.ai-day.on { border-color: var(--ink); background: var(--accent); color: var(--accent-ink); font-weight: 700; }
.ai-window-sep { color: var(--muted); font-size: var(--fs-sm); }
.ai-window-note { margin: 12px 0 14px; }
.ai-window-empty { padding: 24px 12px; border: 1px dashed var(--line); }

/* 视频理解里的分组块：硬边 + 顶栏标签，不用卡片堆叠 */
.proc-group {
  margin: 20px 0 4px;
  border: 1px solid var(--line);
  background: var(--surface-2);
  padding: 0 14px 6px;
}
.proc-group-head {
  display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  margin: 0 -14px 8px; padding: 8px 14px;
  border-bottom: 1px solid var(--line);
  background: var(--surface-3);
}
.proc-group-title { font-weight: 700; font-size: var(--fs-md); color: var(--ink); }
.proc-group-note { color: var(--muted); font-size: var(--fs-sm); }
.proc-group :deep(.el-form-item:last-child) { margin-bottom: 8px; }

/* 爆点判据编辑器：序号 + 名称/id + 说明 + 排序，不用卡片 */
.criteria-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 4px; }
.criteria-row {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 10px 12px; border: 1px solid var(--line-soft); background: var(--surface-2);
}
.criteria-rank {
  flex: none; width: 22px; height: 22px; display: grid; place-items: center;
  border: 1px solid var(--ink); background: var(--surface);
  font-size: var(--fs-xs); font-weight: 700; font-variant-numeric: tabular-nums;
}
.criteria-fields { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.criteria-top { display: flex; gap: 6px; flex-wrap: wrap; }
.criteria-name { flex: 1 1 190px; }
.criteria-id { flex: 1 1 150px; }
.criteria-acts { flex: none; display: flex; gap: 4px; flex-wrap: wrap; }
.criteria-empty { color: var(--muted); font-size: var(--fs-sm); margin: 6px 0; }

@media (max-width: 720px) {
  .ai-window-row { align-items: flex-start; }
  .ai-window-days { flex-wrap: wrap; }
  .criteria-row { flex-wrap: wrap; }
  .criteria-acts { margin-left: auto; }
}
</style>
