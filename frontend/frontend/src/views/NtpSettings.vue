<template>
  <div class="min-h-screen bg-app text-ink-strong animate-in">
    <!-- ===================== 页头 ===================== -->
    <header class="border-b border-line bg-surface/50 px-6 py-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-center gap-3">
          <h1 class="text-2xl font-bold text-ink-strong">时钟同步</h1>
          <!-- 运行态徽标：先判「未开启」，再看 running（关掉开关后监督线程仍存活） -->
          <span :class="headerBadgeClass">
            <span :class="headerDotClass" class="status-dot"></span>
            {{ headerBadgeText }}
          </span>
        </div>
        <div class="flex items-center gap-2">
          <button class="btn btn-ghost btn-sm" :disabled="loading" @click="loadAll">
            <ArrowPathIcon class="h-4 w-4" :class="{ 'animate-spin': loading }" />
            刷新
          </button>
        </div>
      </div>
      <p class="mt-1.5 text-xs text-ink-muted">
        平台作为内网时间源：上行向外部时间源取时用于比对平台时钟偏差，下行应答网络设备的 NTP 请求。
        两条链路互相独立，默认全部关闭，需要时手动开启。
      </p>
    </header>

    <main class="space-y-5 px-6 py-6">
      <!-- 非管理员提示（开关与同步按钮均为后端 admin_only） -->
      <div v-if="!isAdmin && meLoaded" class="card border-l-4 border-l-warning px-4 py-3">
        <p class="text-sm text-ink">当前账号为只读角色，仅可查看状态与偏差；开关、参数保存与手动同步需管理员权限。</p>
      </div>

      <!-- 部署级总闸关闭：后端 PUT /settings 会返 409，提前说清而不是让人点了才报错 -->
      <div v-if="status && status.server_configured === false" class="card border-l-4 border-l-danger px-4 py-3">
        <p class="text-sm text-danger font-medium">部署级总闸已关闭</p>
        <p class="mt-1 text-xs text-ink-muted">
          部署配置中 <code class="font-mono">NTP_SERVER_ENABLED=false</code>，界面开关无法启用授时服务。
          需先修改后端 <code class="font-mono">.env</code> 并重启服务。
        </p>
      </div>

      <!-- ===================== 偏差总览 ===================== -->
      <!-- 「平台当前时间」与偏差合并在同一张卡里（华哥 2026-09-24 定）：
           两者本就是同一件事的两个侧面，拆成两张卡既重复又白占一屏高度。
           时间按**本机（浏览器）时区**显示，与页面其它时间戳（fmtTime）同一口径 ——
           否则会出现"顶部 01:35、下方采样时间 09:31"这种自相矛盾的画面。 -->
      <section class="grid grid-cols-1 gap-5 lg:grid-cols-3">
        <!-- 偏差主卡（含平台当前时间） -->
        <div class="card card-lift p-5 lg:col-span-2">
          <div class="mb-4 flex items-center justify-between">
            <h2 class="card-title">平台时钟偏差</h2>
            <span v-if="offset && offset.available" class="text-xs text-ink-faint">
              采样于 {{ fmtTime(offset.sampled_at) }}
            </span>
          </div>

          <!-- 平台当前时间 -->
          <div class="flex flex-wrap items-end justify-between gap-x-6 gap-y-3 border-b border-line pb-4">
            <div class="flex items-center gap-4">
              <div class="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-brand-500/10">
                <ClockIcon class="h-6 w-6 text-brand-500" />
              </div>
              <div>
                <p class="form-label">平台当前时间</p>
                <p class="font-mono text-4xl font-bold leading-none tabular-nums tracking-tight text-ink-strong">
                  {{ clockText }}
                </p>
                <p class="mt-2 text-xs text-ink-muted">
                  {{ clockDateText }}
                  <span v-if="clockTimezoneText">· 本机时区 {{ clockTimezoneText }}</span>
                </p>
              </div>
            </div>

            <!-- 宿主时区与本机不一致时才出现：两个时区并存时最容易让人怀疑时钟坏了，
                 把差异写在脸上比让人猜好。 -->
            <p v-if="hostTzMismatch" class="max-w-xs text-xs leading-relaxed text-ink-faint">
              平台宿主时区为 <span class="font-mono text-ink-muted">{{ hostTzText }}</span>，
              与本机时区（<span class="font-mono text-ink-muted">{{ clockTimezoneText }}</span>）不同 ——
              此处按<strong class="font-medium text-ink-muted">本机时区</strong>显示。
            </p>
          </div>

          <!-- 无样本 -->
          <div v-if="!offset || !offset.available" class="py-6 text-center">
            <p class="text-sm text-ink-muted">{{ (offset && offset.message) || '尚无偏差样本' }}</p>
            <p class="mt-1 text-xs text-ink-faint">
              偏差来自「上行取时」，请先开启上行取时开关，或点击下方「立即采样」。
            </p>
          </div>

          <!-- 有样本 -->
          <div v-else class="mt-4 flex flex-wrap items-end gap-x-8 gap-y-4">
            <div>
              <p class="form-label">偏差值</p>
              <p class="mt-1 font-mono text-4xl font-bold tabular-nums" :class="offsetValueClass">
                {{ offsetParts.value }}<span class="ml-1 text-lg font-normal text-ink-muted">{{ offsetParts.unit }}</span>
              </p>
            </div>
            <!-- 方向：offset 语义是「上游 - 平台」，>0 上游快 = 平台慢 -->
            <div>
              <p class="form-label">时钟状态</p>
              <p class="mt-1 flex items-center gap-2 text-lg font-medium" :class="directionClass">
                <component :is="directionIcon" class="h-5 w-5" />
                {{ directionText }}
              </p>
            </div>
            <div>
              <p class="form-label">阈值</p>
              <p class="mt-1 font-mono text-lg tabular-nums text-ink">{{ fmtSeconds(offset.threshold_seconds) }} 秒</p>
            </div>
            <div>
              <p class="form-label">往返时延</p>
              <p class="mt-1 font-mono text-lg tabular-nums text-ink">{{ fmtSeconds(offset.round_trip_seconds, 3) }} 秒</p>
            </div>
            <div>
              <p class="form-label">上游时间源</p>
              <p class="mt-1 font-mono text-sm text-ink">{{ offset.server || '-' }}</p>
            </div>
          </div>

          <!-- 超阈值告警条 -->
          <div v-if="offset && offset.available && offset.exceeded" class="mt-4 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2">
            <p class="text-sm font-medium text-danger">
              偏差已超过告警阈值，平台时钟与上游时间源不一致，可能影响设备日志时间与告警判定。
            </p>
          </div>
        </div>

        <!-- 快捷操作 -->
        <div class="card card-lift flex flex-col p-5">
          <h2 class="card-title mb-4">时间校准</h2>
          <p class="text-xs text-ink-muted">
            平台不会自动调整自身时钟。确认偏差合理后，可手动把平台时间同步到上游时间源。
          </p>
          <dl class="mt-4 space-y-2 text-xs">
            <div class="flex justify-between">
              <dt class="text-ink-faint">手动同步上限</dt>
              <dd class="font-mono tabular-nums text-ink">{{ fmtSeconds(status && status.manual_sync_max_seconds) }} 秒</dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-ink-faint">偏差采样</dt>
              <dd class="text-ink">{{ upstreamEnabled ? '自动（约每分钟）' : '已停止' }}</dd>
            </div>
          </dl>
          <!-- 超过 manual_sync_max_seconds 后端返 422 并带可读 detail，这里提前拦住 -->
          <div v-if="manualSyncBlocked" class="mt-3 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2">
            <p class="text-xs text-warning">
              偏差超过手动同步上限（{{ fmtSeconds(status && status.manual_sync_max_seconds) }} 秒），
              直接跳变风险过高。请先人工核对平台时间是否正确。
            </p>
          </div>
          <div class="mt-auto space-y-2 pt-4">
            <button
              class="btn btn-primary w-full"
              :disabled="!isAdmin || !(offset && offset.available) || manualSyncBlocked || manualSyncing"
              @click="askManualSync"
            >
              <BoltIcon class="h-4 w-4" />
              {{ manualSyncing ? '同步中…' : '手动同步平台时间' }}
            </button>
            <button
              class="btn btn-outline w-full"
              :disabled="!isAdmin || sampling"
              @click="doSampleNow"
            >
              <ArrowPathIcon class="h-4 w-4" :class="{ 'animate-spin': sampling }" />
              {{ sampling ? '采样中…' : '立即采样一次' }}
            </button>
          </div>

          <!-- 同步结果：这里**持久显示**，不走 4 秒就消失的轻提示 ——
               失败时的诊断是"哪种校时方式卡在哪一条、为什么"，需要能读完、
               能照着上面的处置做（例如给服务加 CAP_SYS_TIME）。 -->
          <div
            v-if="syncResult"
            class="mt-3 rounded-lg border px-3 py-2"
            :class="syncResult.ok ? 'border-success/40 bg-success/10' : 'border-danger/40 bg-danger/10'"
          >
            <div class="flex items-start justify-between gap-2">
              <p class="text-xs font-medium" :class="syncResult.ok ? 'text-success' : 'text-danger'">
                {{ syncResult.title }}
              </p>
              <button class="shrink-0 text-xs text-ink-faint hover:text-ink" @click="syncResult = null">
                关闭
              </button>
            </div>
            <p class="mt-1 whitespace-pre-line break-words text-xs leading-relaxed text-ink-muted">
              {{ syncResult.detail }}
            </p>
          </div>
        </div>
      </section>

      <!-- ===================== 两个开关 ===================== -->
      <section class="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <!-- 下行授时 -->
        <div class="card card-lift p-5">
          <div class="flex items-start justify-between gap-4">
            <div>
              <h2 class="card-title">下行授时（NTP 服务端）</h2>
              <p class="mt-1 text-xs text-ink-muted">
                监听 UDP 端口，应答网络设备的 NTP 请求，让设备与本平台对时。
              </p>
            </div>
            <button
              class="relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50"
              :class="serverEnabled ? 'bg-brand-500' : 'bg-hover'"
              role="switch"
              :aria-checked="String(serverEnabled)"
              :disabled="!isAdmin || togglingServer"
              :title="isAdmin ? '' : '需管理员权限'"
              @click="toggleServer"
            >
              <span
                class="inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform"
                :class="serverEnabled ? 'translate-x-[22px]' : 'translate-x-0.5'"
              ></span>
            </button>
          </div>

          <dl class="mt-4 space-y-2 text-xs">
            <div class="flex justify-between">
              <dt class="text-ink-faint">运行状态</dt>
              <dd class="flex items-center gap-1.5">
                <span class="status-dot" :class="serverDotClass"></span>
                <span :class="serverTextClass">{{ serverStateText }}</span>
              </dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-ink-faint">监听地址</dt>
              <dd class="font-mono text-ink">
                {{ status && status.listen_host ? status.listen_host : '0.0.0.0' }}:<span class="tabular-nums">{{ (status && status.bound_port) || (status && status.port) || 123 }}</span>
              </dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-ink-faint">请求 / 应答<span class="ml-1 opacity-80">（本次启动以来）</span></dt>
              <dd class="font-mono tabular-nums text-ink">
                {{ fmtNum(status && status.requests) }} / {{ fmtNum(status && status.responses) }}
              </dd>
            </div>
          </dl>

          <!-- 下行授时的两个视图，回答三个不同的问题（单值「最近请求」一个都答不了）：
               ① 「本次启动以来」台账 → 那些客户端真的来取过时（拿设备表一对，
                  没出现的直接列出来 = 反向诊断入口：设备侧没配 / ACL 不通 / 地址填错）；
               ② 「近期请求」列表 → 每台设备的**实际轮询间隔**（相邻同源的时间差，
                  由后端算好 interval_seconds，页面只显示）。
               两组数据都不受对方容量影响：窗口会被突发设备吃满（实测见过 1 秒一发的），
               若"哪些设备来过"只看窗口，一台 64 秒前才来过的设备会被读成"从没来过"。 -->
          <div class="mt-4">
            <h3 class="text-xs font-medium text-ink-muted">
              本次启动以来<span class="ml-1 font-normal text-ink-faint">（请求 / 应答同口径，重启平台后归零）</span>
            </h3>

            <!-- 这两行是**入口**而不是"标签墙"：数字直接给结论，点开给名单（设备名 + IP）。
                 原先把设备名平铺成一排徽章，一眼只能看出"有几台"，真要照着名单去设备侧
                 核对时还得逐个悬停读 title —— 而"拿名单去查设备"恰恰是这张卡最常用的动作。
                 另外徽章超过 12 个就被截成"+N 台"，恰好把要找的那台藏在省略号里。 -->
            <div class="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
              <button
                type="button"
                class="flex items-center gap-2 rounded-lg border border-line bg-surface-2/60 px-3 py-2 text-left text-xs transition-colors hover:bg-hover"
                title="本次启动以来向本平台发起过 NTP 请求的客户端 —— 点开查看设备名与 IP"
                @click="openClientDialog('online')"
              >
                <span class="h-2 w-2 shrink-0 rounded-full bg-success"></span>
                <span class="text-ink-muted">客户端在线</span>
                <span class="ml-auto font-mono text-lg font-bold tabular-nums text-ink-strong">{{ onlineClients.length }}</span>
                <ChevronRightIcon class="h-3.5 w-3.5 shrink-0 text-ink-faint" />
              </button>

              <button
                type="button"
                class="flex items-center gap-2 rounded-lg border bg-surface-2/60 px-3 py-2 text-left text-xs transition-colors hover:bg-hover"
                :class="unsyncedDevices.length ? 'border-warning/40' : 'border-line'"
                title="设备表里有、但本次启动以来一次都没来取过时的设备 —— 设备侧没配 / 链路被 ACL 拦 / 服务端地址填错，都在这份名单里"
                @click="openClientDialog('unsynced')"
              >
                <span
                  class="h-2 w-2 shrink-0 rounded-full"
                  :class="unsyncedDevices.length ? 'bg-warning' : 'bg-ink-faint/50'"
                ></span>
                <span class="text-ink-muted">客户端未同步</span>
                <span
                  class="ml-auto font-mono text-lg font-bold tabular-nums"
                  :class="unsyncedDevices.length ? 'text-warning' : 'text-ink-strong'"
                >{{ unsyncedDevices.length }}</span>
                <ChevronRightIcon class="h-3.5 w-3.5 shrink-0 text-ink-faint" />
              </button>
            </div>

            <p v-if="onlineUnmanaged.length" class="mt-1.5 text-[11px] text-warning">
              有 {{ onlineUnmanaged.length }} 个来源不在设备表中（{{ onlineUnmanaged.map((c) => c.ip).join('、') }}）——
              点开「客户端在线」查看。
            </p>
          </div>

          <div class="mt-4">
            <div class="flex items-baseline justify-between gap-2">
              <h3 class="text-xs font-medium text-ink-muted">近期请求</h3>
              <span class="text-[11px] text-ink-faint">
                <template v-if="recentRequests.length">最近 {{ recentRequests.length }} 条</template>
                <template v-else>窗口内暂无</template>
              </span>
            </div>

            <p
              v-if="!recentRequests.length"
              class="mt-2 rounded-lg border border-line/60 px-3 py-3 text-center text-xs text-ink-faint"
            >
              尚无设备来取时。若设备侧已指向本平台，请确认网络可达、链路未被 ACL 拦截。
            </p>

            <div v-else class="mt-2 max-h-48 overflow-auto rounded-lg border border-line/60">
              <table class="w-full table-fixed border-collapse text-left text-xs">
                <thead class="sticky top-0 z-10 bg-surface-2 text-[11px] font-normal text-ink-faint">
                  <tr>
                    <th class="w-[76px] px-2 py-1.5 font-normal">时间</th>
                    <th class="px-2 py-1.5 font-normal">来源设备</th>
                    <th class="w-[72px] px-2 py-1.5 text-right font-normal">间隔</th>
                    <th class="w-[52px] px-2 py-1.5 text-right font-normal">应答</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(r, i) in recentRequests" :key="`${r.at}-${i}`" class="border-t border-line/40">
                    <td class="px-2 py-1 font-mono tabular-nums text-ink-muted" :title="fmtTime(r.at)">
                      {{ fmtHms(r.at) }}
                    </td>
                    <td class="truncate px-2 py-1">
                      <template v-if="deviceNameByIp[r.client_ip]">
                        <span class="text-ink">{{ deviceNameByIp[r.client_ip] }}</span>
                        <span class="ml-1 font-mono text-[11px] text-ink-faint">{{ r.client_ip }}</span>
                      </template>
                      <span
                        v-else
                        class="font-mono text-ink-muted"
                        title="设备表中没有这个地址（可能是循环地址、未纳管设备或上游转发）"
                      >{{ r.client_ip || '-' }}</span>
                    </td>
                    <td
                      class="px-2 py-1 text-right font-mono tabular-nums text-ink-muted"
                      title="与上一次同源请求的时间差（NTP 轮询间隔）"
                    >{{ fmtInterval(r.interval_seconds) }}</td>
                    <td class="px-2 py-1 text-right">
                      <span v-if="r.answered" class="text-success">成功</span>
                      <span v-else class="text-warning" :title="r.reason || ''">未应答</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- 绑定失败：端口被占用是最常见的坑（Windows W32Time / Linux chronyd） -->
          <div v-if="status && status.bind_error" class="mt-4 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2">
            <p class="text-xs font-medium text-danger">端口绑定失败：{{ status.bind_error }}</p>
            <p v-if="status.bind_occupier" class="mt-1 text-xs text-ink-muted">
              疑似占用：<span class="font-mono">{{ status.bind_occupier }}</span>
            </p>
            <ul v-if="status.bind_hints && status.bind_hints.length" class="mt-1.5 list-disc space-y-0.5 pl-4 text-xs text-ink-muted">
              <li v-for="(h, i) in status.bind_hints" :key="i">{{ h }}</li>
            </ul>
          </div>

          <div v-if="status && status.last_error" class="mt-3 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2">
            <p class="text-xs text-warning">最近错误：{{ status.last_error }}</p>
          </div>
        </div>

        <!-- 上行取时 -->
        <div class="card card-lift p-5">
          <div class="flex items-start justify-between gap-4">
            <div>
              <h2 class="card-title">上行取时（NTP 客户端）</h2>
              <p class="mt-1 text-xs text-ink-muted">
                按周期向外部时间源取时，用于计算平台时钟偏差并绘制曲线（不会自动改平台时间）。
              </p>
            </div>
            <button
              class="relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50"
              :class="upstreamEnabled ? 'bg-brand-500' : 'bg-hover'"
              role="switch"
              :aria-checked="String(upstreamEnabled)"
              :disabled="!isAdmin || togglingUpstream"
              :title="isAdmin ? '' : '需管理员权限'"
              @click="toggleUpstream"
            >
              <span
                class="inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform"
                :class="upstreamEnabled ? 'translate-x-[22px]' : 'translate-x-0.5'"
              ></span>
            </button>
          </div>

          <dl class="mt-4 space-y-2 text-xs">
            <div class="flex justify-between">
              <dt class="text-ink-faint">运行状态</dt>
              <dd class="flex items-center gap-1.5">
                <span class="status-dot" :class="upstreamDotClass"></span>
                <span :class="upstreamTextClass">{{ upstreamStateText }}</span>
              </dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-ink-faint">最近成功取时</dt>
              <dd class="text-ink">
                <span v-if="status && status.last_offset_at">{{ fmtTime(status.last_offset_at) }}</span>
                <span v-else class="text-ink-faint">-</span>
              </dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-ink-faint">最近往返时延</dt>
              <dd class="font-mono tabular-nums text-ink">{{ fmtSeconds(status && status.last_round_trip, 3) }} 秒</dd>
            </div>
          </dl>

          <!-- 时间源可编辑 -->
          <div class="mt-4 space-y-2">
            <label class="form-label" for="ntp-upstream">上游时间源</label>
            <div class="flex gap-2">
              <input
                id="ntp-upstream"
                v-model.trim="upstreamServerInput"
                class="input flex-1 font-mono"
                :disabled="!isAdmin"
                placeholder="ntp.aliyun.com"
                @keyup.enter="saveUpstream"
              />
              <button
                class="btn btn-outline"
                :disabled="!isAdmin || savingUpstream || upstreamServerInput === (status && status.upstream_server)"
                @click="saveUpstream"
              >
                保存
              </button>
            </div>
            <p class="text-xs text-ink-faint">
              默认 <code class="font-mono">ntp.aliyun.com</code>（阿里云）。可填域名或 IP，留空则沿用当前值。
            </p>
          </div>

          <!-- 阈值可编辑（后端限制 0 < x ≤ 3600） -->
          <div class="mt-3 space-y-2">
            <label class="form-label" for="ntp-threshold">偏差告警阈值（秒）</label>
            <div class="flex gap-2">
              <input
                id="ntp-threshold"
                v-model.number="thresholdInput"
                type="number"
                min="1"
                max="3600"
                step="1"
                class="input w-32 font-mono tabular-nums"
                :disabled="!isAdmin"
                @keyup.enter="saveThreshold"
              />
              <button
                class="btn btn-outline"
                :disabled="!isAdmin || savingThreshold || thresholdInput === (offset && offset.threshold_seconds)"
                @click="saveThreshold"
              >
                保存
              </button>
            </div>
            <p class="text-xs text-ink-faint">超过该值的偏差会触发「时钟一致性」告警，并在上方偏差卡标红。</p>
          </div>

          <div v-if="status && status.upstream_error" class="mt-3 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2">
            <p class="text-xs text-warning">最近取时失败：{{ status.upstream_error }}</p>
          </div>
        </div>
      </section>

      <!-- ===================== 偏差曲线 ===================== -->
      <section class="card card-lift p-5">
        <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h2 class="card-title">偏差历史曲线</h2>
          <div class="flex items-center gap-2">
            <select v-model.number="historyHours" class="select" @change="loadHistory">
              <option :value="6">最近 6 小时</option>
              <option :value="24">最近 24 小时</option>
              <option :value="72">最近 3 天</option>
              <option :value="168">最近 7 天</option>
              <option :value="720">最近 30 天</option>
            </select>
          </div>
        </div>

        <div v-if="historyStats.count" class="mb-3 flex flex-wrap gap-x-6 gap-y-1 text-xs">
          <span class="text-ink-muted">样本 <span class="font-mono tabular-nums text-ink">{{ fmtNum(historyStats.count) }}</span></span>
          <span class="text-ink-muted">成功 <span class="font-mono tabular-nums text-success">{{ fmtNum(historyStats.success_count) }}</span></span>
          <span class="text-ink-muted">失败 <span class="font-mono tabular-nums" :class="historyStats.failed_count ? 'text-danger' : 'text-ink-faint'">{{ fmtNum(historyStats.failed_count) }}</span></span>
          <span class="text-ink-muted">最大偏差 <span class="font-mono tabular-nums text-ink">{{ fmtOffset(historyStats.max_abs_seconds) }}</span></span>
          <span v-if="!chartMilliScale" class="text-ink-muted">阈值线 <span class="font-mono tabular-nums text-warning">±{{ fmtSeconds(historyStats.threshold_seconds, 0) }} 秒</span></span>
          <span v-else class="text-ink-muted">
            阈值 <span class="font-mono tabular-nums text-warning">±{{ fmtSeconds(historyStats.threshold_seconds, 0) }} 秒</span>
            <span class="text-ink-faint">（偏差量级较小，图上未绘制阈值线）</span>
          </span>
        </div>

        <div v-show="historyStats.count" ref="chartEl" class="h-72 w-full"></div>
        <p v-if="!historyStats.count" class="py-12 text-center text-sm text-ink-muted">
          该时间段内没有偏差样本。开启「上行取时」后，平台会按周期自动采样。
        </p>
      </section>

      <!-- ===================== 设备下发 ===================== -->
      <section class="card card-lift p-5">
        <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="card-title">设备 NTP 配置下发</h2>
            <p class="mt-1 text-xs text-ink-muted">
              把本平台作为时间源写入网络设备配置（真实改动设备，属高危操作，操作记入审计）。
            </p>
          </div>
          <button class="btn btn-ghost btn-sm" :disabled="loadingDevices" @click="loadCandidates">
            <ArrowPathIcon class="h-4 w-4" :class="{ 'animate-spin': loadingDevices }" />
            刷新设备
          </button>
        </div>

        <!-- 参数区 -->
        <div class="grid grid-cols-1 gap-4 rounded-lg border border-line bg-surface-2/50 p-4 md:grid-cols-3">
          <div>
            <label class="form-label" for="ntp-addr">下发地址（平台时间源地址）</label>
            <select id="ntp-addr" v-model="addressInput" class="select mt-1 w-full font-mono">
              <option value="">{{ candidates.length ? '请选择…' : '暂无候选地址' }}</option>
              <option v-for="c in candidates" :key="c" :value="c">
                {{ c }}{{ c === recommendedAddress ? '（推荐）' : '' }}
              </option>
            </select>
            <p class="mt-1 text-xs text-ink-faint">
              设备需能路由到该地址。推荐值为后端 <code class="font-mono">NTP_ADVERTISE_ADDRESS</code> 配置项。
            </p>
          </div>
          <div>
            <label class="form-label" for="ntp-mode">时钟协议</label>
            <select id="ntp-mode" v-model="clockMode" class="select mt-1 w-full">
              <option value="system">system（普通设备，clock protocol ntp）</option>
              <option value="mdc">mdc（H3C MDC 设备，clock protocol ntp mdc 1）</option>
            </select>
            <p class="mt-1 text-xs text-ink-faint">
              MDC 设备需单独指定（仅 H3C 有此形态）；不确定时保持 system，可按命令预览结果判断。
            </p>
          </div>
          <div>
            <label class="form-label">保存配置</label>
            <label class="mt-1.5 flex items-center gap-2 text-sm text-ink">
              <input v-model="saveConfig" type="checkbox" class="checkbox" />
              下发后保存配置（H3C `save force` / 华为 `save`，重启不丢失）
            </label>
          </div>
        </div>

        <!-- 设备列表 + 右侧操作面板 -->
        <div class="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_18rem]">
          <!-- 左：设备列表（内层缩进沿用原样） -->
          <div class="min-w-0">
          <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-3 text-xs text-ink-muted">
              <button class="btn btn-ghost btn-sm" @click="selectAll">
                {{ allSelected ? '取消全选' : '全选' }}
              </button>
              <button
                class="btn btn-ghost btn-sm"
                :disabled="!unsyncedDevices.length"
                title="只勾选本次启动以来还没来取过时的设备 —— 已同步的不必重复下发"
                @click="selectUnsynced"
              >选未同步</button>
              <span>已选 <span class="font-mono tabular-nums text-ink">{{ selectedIds.length }}</span> / {{ devices.length }}</span>
              <span v-if="selectedSyncedCount" class="text-success">其中 {{ selectedSyncedCount }} 台已同步</span>
              <span v-if="selectedMdCount" class="text-warning">其中 {{ selectedMdCount }} 台疑似 MDC</span>
            </div>
            <input v-model.trim="deviceFilter" class="input w-56" placeholder="搜索名称 / IP" />
          </div>

          <div v-if="loadingDevices" class="py-8 text-center text-sm text-ink-muted">加载中…</div>
          <p v-else-if="!devices.length" class="py-8 text-center text-sm text-ink-muted">没有可下发的设备</p>
          <div v-else class="max-h-80 overflow-y-auto rounded-lg border border-line">
            <table class="table">
              <thead class="sticky top-0 bg-surface-2">
                <tr>
                  <th class="w-10"></th>
                  <th>设备</th>
                  <th class="w-36">IP</th>
                  <th class="w-28">对时状态</th>
                  <th class="w-40">最近下发</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="d in filteredDevices" :key="d.id" class="hover:bg-hover">
                  <td>
                    <input v-model="selectedIds" type="checkbox" class="checkbox" :value="d.id" />
                  </td>
                  <td>
                    <span class="text-ink">{{ d.name }}</span>
                    <span v-if="isMdc(d)" class="badge badge-warning ml-1.5">MDC</span>
                  </td>
                  <td class="font-mono text-xs">{{ d.ip }}</td>
                  <td>
                    <!-- 「已同步」判据 = 设备**真的回来取过时**，而不是"我们下发成功过"。
                         下发记录只能证明命令发出去了；设备按周期来取时，才证明它确实在跟平台对时。
                         这类设备不必重复下发（页面另有「选未同步」直接跳过它们）。 -->
                    <span
                      v-if="syncInfo(d)"
                      class="badge badge-success"
                      :title="syncTooltip(d)"
                    >已同步</span>
                    <span
                      v-else
                      class="text-xs text-ink-faint"
                      title="本次启动以来未见到该设备来平台取时（可能是设备侧没配，或中间 ACL 不通）"
                    >未同步</span>
                  </td>
                  <td>
                    <div class="flex flex-wrap items-center gap-x-1.5 gap-y-1">
                      <span
                        v-if="deviceStatusMap[d.id]"
                        :class="statusBadgeClass(deviceStatusMap[d.id].status)"
                        class="badge"
                        :title="statusTitle(d)"
                      >
                        {{ statusLabel(deviceStatusMap[d.id].status) }}
                      </span>
                      <span v-else class="text-xs text-ink-faint">未下发</span>
                      <button
                        v-if="deviceFailure(d)"
                        class="text-xs text-danger underline decoration-dotted underline-offset-2 hover:opacity-80"
                        @click="openDeviceFailure(d)"
                      >查看原因</button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          </div>

          <!-- 右：操作面板（选中设备后即可「立即下发」） -->
          <aside class="flex flex-col gap-3 self-start rounded-lg border border-line bg-surface-2/50 p-4 xl:sticky xl:top-4">
            <div>
              <p class="form-label">已选设备</p>
              <p class="mt-1 font-mono text-2xl font-bold tabular-nums text-ink-strong">
                {{ selectedIds.length }}<span class="ml-1 text-sm font-normal text-ink-muted">/ {{ devices.length }} 台</span>
              </p>
              <p v-if="selectedMdCount" class="mt-1 text-xs text-warning">其中 {{ selectedMdCount }} 台疑似 MDC</p>
            </div>

            <dl class="space-y-1.5 border-t border-line pt-3 text-xs">
              <div class="flex justify-between gap-2">
                <dt class="text-ink-faint">下发地址</dt>
                <dd class="truncate font-mono text-ink">{{ addressInput || '-' }}</dd>
              </div>
              <div class="flex justify-between gap-2">
                <dt class="text-ink-faint">时钟协议</dt>
                <dd class="font-mono text-ink">{{ clockMode }}</dd>
              </div>
              <div class="flex justify-between gap-2">
                <dt class="text-ink-faint">保存配置</dt>
                <dd class="text-ink">{{ saveConfig ? '是' : '否' }}</dd>
              </div>
              <div class="flex justify-between gap-2">
                <dt class="text-ink-faint">命令模板</dt>
                <dd class="text-right text-ink">{{ templateSummary }}</dd>
              </div>
            </dl>

            <button class="btn btn-primary w-full" :disabled="!canApply" @click="startApply">
              <PaperAirplaneIcon class="h-4 w-4" />
              {{ loadingPreview ? '生成命令…' : '立即下发' }}
            </button>
            <button class="btn btn-outline w-full" @click="openTemplateDialog">
              <PencilSquareIcon class="h-4 w-4" />
              下发模板
            </button>
            <button
              v-if="hasAnyRecord"
              class="btn btn-danger w-full"
              :disabled="!isAdmin || !selectedIds.length"
              @click="askRollback"
            >
              <ArrowUturnLeftIcon class="h-4 w-4" />
              回滚配置
            </button>

            <p class="border-t border-line pt-3 text-xs text-ink-faint">
              下发会真实改动设备配置并记入审计；按每批 {{ APPLY_CHUNK }} 台执行，可随时在下方结果区查看逐台状态。
            </p>
          </aside>
        </div>

        <!-- 下发进度 -->
        <div v-if="applying" class="mt-4 rounded-lg border border-line bg-surface-2/50 p-4">
          <div class="mb-2 flex items-center justify-between text-xs text-ink-muted">
            <span>{{ applyPhase }}</span>
            <span class="font-mono tabular-nums">{{ applyDone }} / {{ applyTotal }}</span>
          </div>
          <div class="h-2 w-full overflow-hidden rounded-full bg-hover">
            <div class="h-full bg-brand-500 transition-all" :style="{ width: applyPercent + '%' }"></div>
          </div>
        </div>

        <!-- 下发结果（三态） -->
        <div v-if="results.length" class="mt-4">
          <div class="mb-2 flex flex-wrap items-center gap-4 text-xs">
            <h3 class="text-sm font-semibold text-ink-strong">下发结果</h3>
            <span class="text-success">成功 <span class="font-mono tabular-nums">{{ summary.applied }}</span></span>
            <span class="text-warning">待确认 <span class="font-mono tabular-nums">{{ summary.unverified }}</span></span>
            <span class="text-danger">失败 <span class="font-mono tabular-nums">{{ summary.failed }}</span></span>
            <button class="btn btn-ghost btn-sm" @click="results = []">清空</button>
          </div>
          <div class="max-h-96 overflow-y-auto rounded-lg border border-line">
            <table class="table">
              <thead class="sticky top-0 bg-surface-2">
                <tr>
                  <th>设备</th>
                  <th class="w-24">状态</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in results" :key="i" class="hover:bg-hover">
                  <td class="text-ink">{{ r.device_name || r.device_id }}</td>
                  <td>
                    <span class="badge" :class="statusBadgeClass(resultState(r))">{{ resultLabel(r) }}</span>
                  </td>
                  <td class="text-xs text-ink-muted">
                    <p class="line-clamp-2 whitespace-pre-line">{{ resultBrief(r) }}</p>
                    <button
                      class="mt-1 underline decoration-dotted underline-offset-2 hover:opacity-80"
                      :class="resultState(r) === 'failed' ? 'text-danger' : 'text-brand-500'"
                      @click="openResultDetail(r)"
                    >查看详情</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </main>

    <!-- ===================== 下发模板（可编辑） ===================== -->
    <div
      v-if="templateDialogOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="closeTemplateDialog"
    >
      <div class="card flex max-h-[90vh] w-full max-w-4xl animate-in flex-col p-5">
        <div class="flex items-start justify-between gap-4">
          <div>
            <h3 class="text-lg font-bold text-ink-strong">下发模板</h3>
            <p class="mt-1 text-xs text-ink-muted">
              命令集随型号/版本有差异时在这里改。改动**直接用于本次下发**；
              点「保存为默认模板」才会对以后所有下发生效。
            </p>
          </div>
          <button class="btn btn-ghost btn-sm" @click="closeTemplateDialog">关闭</button>
        </div>

        <!-- 厂商 Tab -->
        <div class="mt-4 flex gap-1 border-b border-line">
          <button
            v-for="t in templateTabs"
            :key="t.key"
            class="border-b-2 px-3 py-2 text-sm transition-colors"
            :class="templateTab === t.key
              ? 'border-brand-500 font-medium text-brand-500'
              : 'border-transparent text-ink-muted hover:text-ink'"
            @click="templateTab = t.key"
          >
            {{ t.label }}
          </button>
        </div>

        <div class="mt-4 flex-1 space-y-4 overflow-y-auto pr-1">
          <div v-for="block in currentTabBlocks" :key="block.key">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <label class="form-label" :for="'tpl-' + block.key">{{ block.label }}</label>
              <div class="flex items-center gap-2">
                <span class="badge" :class="savedTemplateMeta(block.key).customized ? 'badge-warning' : 'badge-neutral'">
                  {{ savedTemplateMeta(block.key).customized ? '已自定义' : '默认' }}
                </span>
                <button
                  class="btn btn-ghost btn-sm"
                  :disabled="!isAdmin || savingTemplate"
                  @click="resetTemplate(block.key)"
                >
                  恢复默认
                </button>
              </div>
            </div>
            <textarea
              :id="'tpl-' + block.key"
              v-model="templateDraft[block.key]"
              rows="8"
              spellcheck="false"
              class="input mt-1 w-full font-mono text-xs leading-relaxed"
              :disabled="!isAdmin"
            ></textarea>
            <p v-if="savedTemplateMeta(block.key).updated_at" class="mt-1 text-xs text-ink-faint">
              上次修改 {{ fmtTime(savedTemplateMeta(block.key).updated_at) }}
              <span v-if="savedTemplateMeta(block.key).operator"> · {{ savedTemplateMeta(block.key).operator }}</span>
            </p>
          </div>

          <div class="rounded-lg border border-line bg-surface-2/50 p-3 text-xs text-ink-muted">
            <p class="font-medium text-ink">可用占位符</p>
            <ul class="mt-1.5 space-y-1">
              <li v-for="ph in templatePlaceholders" :key="ph.token">
                <code class="font-mono text-ink">{{ ph.token }}</code>
                <span v-if="ph.required" class="badge badge-danger ml-1.5">必填</span>
                <span class="ml-1">— {{ ph.desc }}</span>
              </li>
            </ul>
            <p class="mt-2">{{ templateSaveNote }}</p>
            <p class="mt-1">
              上限 {{ templateLimits.max_lines }} 行、每行 {{ templateLimits.max_line_len }} 字符。
              模板含非法内容时后端会拒绝保存/下发（避免把坏命令投到设备上）。
            </p>
          </div>
        </div>

        <div class="mt-4 flex flex-wrap justify-end gap-2 border-t border-line pt-4">
          <button class="btn btn-ghost" @click="closeTemplateDialog">取消</button>
          <button
            class="btn btn-outline"
            :disabled="!isAdmin || savingTemplate"
            @click="saveTemplates"
          >
            {{ savingTemplate ? '保存中…' : '保存为默认模板' }}
          </button>
          <button class="btn btn-primary" @click="closeTemplateDialog">用于本次下发</button>
        </div>
      </div>
    </div>

    <!-- ===================== 二次确认弹窗 ===================== -->
    <div v-if="confirmDialog.open" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" @click.self="confirmDialog.open = false">
      <div class="card w-full max-w-2xl animate-in p-5">
        <h3 class="text-lg font-bold" :class="confirmDialog.danger ? 'text-danger' : 'text-ink-strong'">
          {{ confirmDialog.title }}
        </h3>
        <p class="mt-2 text-sm text-ink-muted">{{ confirmDialog.message }}</p>

        <div v-if="confirmDialog.commands" class="mt-3">
          <p class="form-label">将要执行的命令</p>
          <pre class="mt-1 max-h-56 overflow-auto rounded-md border border-line bg-app p-3 font-mono text-xs text-ink">{{ confirmDialog.commands }}</pre>
        </div>

        <div v-if="confirmDialog.note" class="mt-3 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2">
          <p class="text-xs text-warning">{{ confirmDialog.note }}</p>
        </div>

        <div class="mt-5 flex justify-end gap-2">
          <button class="btn btn-ghost" :disabled="confirmDialog.busy" @click="confirmDialog.open = false">取消</button>
          <button
            class="btn"
            :class="confirmDialog.danger ? 'btn-danger' : 'btn-primary'"
            :disabled="confirmDialog.busy"
            @click="confirmDialog.onConfirm"
          >
            {{ confirmDialog.busy ? '执行中…' : confirmDialog.confirmText || '确认' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ===================== 失败原因 / 下发详情 ===================== -->
    <!-- 为什么单开弹窗而不是塞进表格：失败诊断动辄 3~5 行，还带设备报错、执行的命令、
         下发后回读，表格里只能截断。而"这台为什么失败、下次要不要改模板/改地址"
         恰恰是再次下发之前必须看全的东西。 -->
    <div
      v-if="detailDialog.open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="detailDialog.open = false"
    >
      <div class="card flex max-h-[88vh] w-full max-w-3xl animate-in flex-col p-5">
        <div class="flex items-start justify-between gap-4">
          <div class="min-w-0">
            <h3 class="text-lg font-bold text-ink-strong">{{ detailDialog.title }}</h3>
            <p class="mt-1 text-xs text-ink-muted">{{ detailDialog.subtitle }}</p>
          </div>
          <button class="btn btn-ghost btn-sm" @click="detailDialog.open = false">关闭</button>
        </div>

        <div class="mt-4 flex-1 space-y-3 overflow-y-auto pr-1">
          <!-- 「失败但已同步」这个矛盾最容易被误读成"设备有问题"，其实多半是回读判定的偏差：
               设备都在取时了，说明配置确实生效了。既然页面同时握着两条证据，就有义务说清楚。 -->
          <div v-if="detailDialog.hint" class="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2">
            <p class="text-xs text-warning">{{ detailDialog.hint }}</p>
          </div>

          <div>
            <p class="form-label">{{ detailDialog.reasonLabel }}</p>
            <pre class="mt-1 whitespace-pre-wrap break-words rounded-md border border-line bg-app p-3 text-xs text-ink">{{ detailDialog.reason || '（无）' }}</pre>
          </div>

          <div v-if="detailDialog.warnings.length">
            <p class="form-label">设备返回的报错 / 告警（{{ detailDialog.warnings.length }} 条）</p>
            <pre class="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border border-warning/40 bg-warning/10 p-3 font-mono text-xs text-ink">{{ detailDialog.warnings.join('\n') }}</pre>
          </div>

          <div v-if="detailDialog.commands">
            <p class="form-label">执行的命令</p>
            <pre class="mt-1 max-h-56 overflow-auto rounded-md border border-line bg-app p-3 font-mono text-xs text-ink">{{ detailDialog.commands }}</pre>
          </div>

          <div v-if="detailDialog.readback">
            <p class="form-label">下发后回读（判定依据）</p>
            <pre class="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-app p-3 font-mono text-xs text-ink-faint">{{ detailDialog.readback }}</pre>
          </div>

          <dl
            v-if="detailDialog.meta.length"
            class="grid grid-cols-1 gap-x-4 gap-y-1.5 border-t border-line pt-3 text-xs sm:grid-cols-2"
          >
            <div v-for="m in detailDialog.meta" :key="m.k" class="flex justify-between gap-2">
              <dt class="shrink-0 text-ink-faint">{{ m.k }}</dt>
              <dd class="truncate text-ink" :title="m.v">{{ m.v }}</dd>
            </div>
          </dl>
        </div>

        <div class="mt-4 flex justify-end border-t border-line pt-4">
          <button class="btn btn-outline" @click="detailDialog.open = false">关闭</button>
        </div>
      </div>
    </div>

    <!-- ===================== 客户端名单（在线 / 未同步） ===================== -->
    <!-- 为什么单开弹窗而不是把名单铺在卡片上：名单长度=设备数，几十台上百台时
         铺在卡片里会把这页撑爆、还把「近期请求」挤到屏幕外。而这份名单的用途
         只有一个 —— 拿着它去设备侧核对（所以每行都带可复制的 IP）。 -->
    <div
      v-if="clientDialog.open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="clientDialog.open = false"
    >
      <div class="card flex max-h-[88vh] w-full max-w-2xl animate-in flex-col p-5">
        <div class="flex items-start justify-between gap-4">
          <div class="min-w-0">
            <h3 class="text-lg font-bold text-ink-strong">{{ clientDialogTitle }}</h3>
            <p class="mt-1 text-xs text-ink-muted">{{ clientDialogSubtitle }}</p>
          </div>
          <button class="btn btn-ghost btn-sm" @click="clientDialog.open = false">关闭</button>
        </div>

        <div v-if="clientDialog.mode === 'unsynced' && clientRows.length" class="mt-3 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2">
          <p class="text-xs text-warning">
            这些设备本次启动以来从未向平台发起过 NTP 请求。排查方向（按常见度）：
            ① 设备侧 NTP 未配置或未指向本平台；② 中间 ACL 未放通 UDP 123；
            ③ 设备时区/服务被 <span class="font-mono">undo ntp-service enable</span> 关闭；
            ④ 设备显示时间比北京时间差 8 小时 → 多为<b>时区仍是 UTC</b>（H3C 默认），
            下发模板里已含 <span class="font-mono">clock timezone Beijing add 08:00:00</span>。
            注意「最近下发」里显示<b>已生效</b>的设备属于"配了但没跑"，光看下发记录看不出来。
          </p>
        </div>

        <div class="mt-4 flex-1 overflow-auto rounded-lg border border-line">
          <table class="table">
            <thead class="sticky top-0 bg-surface-2">
              <tr v-if="clientDialog.mode === 'online'">
                <th>设备名称</th>
                <th class="w-36">IP</th>
                <th class="w-24">取时次数</th>
                <th class="w-28">最近取时</th>
                <th class="w-24" title="与同源上一条请求的时间差。设备侧突发时这个值会很小（<1s），那是同一次突发内的间隔、不是轮询周期">最近间隔</th>
              </tr>
              <tr v-else>
                <th>设备名称</th>
                <th class="w-36">IP</th>
                <th class="w-28">最近下发</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in clientRows" :key="r.key" class="hover:bg-hover">
                <td>
                  <template v-if="r.name">
                    <span class="text-ink">{{ r.name }}</span>
                    <span v-if="r.mdc" class="badge badge-warning ml-1.5">MDC</span>
                  </template>
                  <span v-else class="badge badge-neutral">未纳管</span>
                </td>
                <td class="font-mono text-xs">{{ r.ip }}</td>
                <template v-if="clientDialog.mode === 'online'">
                  <td class="font-mono tabular-nums">
                    {{ r.count }}
                    <span v-if="r.answered < r.count" class="ml-1 text-warning" :title="`其中 ${r.count - r.answered} 次未被应答`">!</span>
                  </td>
                  <td :title="fmtTime(r.last_at)">{{ fmtHms(r.last_at) }}</td>
                  <td
                    class="font-mono tabular-nums text-ink-muted"
                    title="与同源上一条请求的时间差；'<1s' 多半是同一次突发内的间隔，轮询周期请看上方「近期请求」"
                  >{{ fmtInterval(r.interval_seconds) }}</td>
                </template>
                <template v-else>
                  <td>
                    <span
                      v-if="r.status"
                      :class="statusBadgeClass(r.status)"
                      class="badge"
                    >{{ statusLabel(r.status) }}</span>
                    <span v-else class="text-xs text-ink-faint">未下发</span>
                  </td>
                  <td class="text-xs text-ink-muted">{{ r.note }}</td>
                </template>
              </tr>
              <tr v-if="!clientRows.length">
                <td :colspan="clientDialog.mode === 'online' ? 5 : 4" class="py-8 text-center text-sm text-ink-muted">
                  {{ clientDialog.mode === 'online'
                    ? '本次启动以来还没有客户端来取时。若设备侧已指向本平台，请确认网络可达、链路未被 ACL 拦截。'
                    : '所有设备本次启动以来都已在与平台取时（客户端全部在线）。' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="mt-4 flex items-center justify-between gap-3 border-t border-line pt-4">
          <button class="btn btn-outline btn-sm" :disabled="!clientRows.length" @click="copyClientList">
            <ClipboardDocumentIcon class="h-4 w-4" />
            复制名单（名称 + IP）
          </button>
          <button class="btn btn-outline" @click="clientDialog.open = false">关闭</button>
        </div>
      </div>
    </div>

    <!-- 轻提示 -->
    <div v-if="flashMsg" class="fixed bottom-6 left-1/2 z-50 w-[min(46rem,92vw)] -translate-x-1/2">
      <div class="whitespace-pre-line break-words rounded-lg border px-4 py-2 text-sm shadow-lg" :class="flashOk ? 'border-success/40 bg-success/15 text-success' : 'border-danger/40 bg-danger/15 text-danger'">
        {{ flashMsg }}
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import * as echarts from 'echarts'
import {
  ArrowPathIcon,
  ArrowUturnLeftIcon,
  ArrowUpIcon,
  ArrowDownIcon,
  CheckCircleIcon,
  ChevronRightIcon,
  ClipboardDocumentIcon,
  BoltIcon,
  ClockIcon,
  PencilSquareIcon,
  PaperAirplaneIcon,
} from '@heroicons/vue/24/outline'
import { chartTheme, onThemeChange } from '../utils/chartTheme.js'
import {
  getNtpStatus,
  updateNtpSettings,
  getNtpOffset,
  getNtpOffsetHistory,
  manualNtpSync,
  getNtpCandidates,
  previewNtp,
  applyNtp,
  rollbackNtp,
  getNtpDeviceStatus,
  getNtpTemplates,
  saveNtpTemplate,
  getDevices,
  getMe,
} from '../api'

// 每批下发的设备数：与 loghost 保持一致（单台要连 SSH 下发 + 前后取快照）
const APPLY_CHUNK = 10

// 星期（按本机时区的墙上时间取，与 fmtTime 同一口径）
const WEEK_CN = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

// 命令模板的四个键（与后端 vendor × action 一一对应）
const TEMPLATE_KEYS = ['h3c_apply', 'h3c_rollback', 'huawei_apply', 'huawei_rollback']
function emptyTemplateMap() {
  return { h3c_apply: '', h3c_rollback: '', huawei_apply: '', huawei_rollback: '' }
}
const TEMPLATE_TABS = [
  { key: 'h3c', label: 'H3C Comware' },
  { key: 'huawei', label: '华为 VRP' },
]

// ---------- 基础状态 ----------
const loading = ref(false)
const loadingDevices = ref(false)
const isAdmin = ref(false)
const meLoaded = ref(false)

const status = ref(null)
const offset = ref(null)
const historyStats = ref({})
// 曲线当前是否处于毫秒量级：决定 Y 轴单位，也决定要不要画阈值线
// （阈值是秒级；毫秒尺度下它远在视野外，画了也看不见 —— 与其留个"看不到的图例"不如说清楚）
const chartMilliScale = ref(false)
// 候选地址是纯字符串数组（后端 local_address_candidates），推荐值单列一个字段
const candidates = ref([])
const recommendedAddress = ref('')
const devices = ref([])
const deviceStatusMap = ref({})

const upstreamServerInput = ref('')
const thresholdInput = ref(60)
const addressInput = ref('')
const clockMode = ref('system')
const saveConfig = ref(true)
const deviceFilter = ref('')
const selectedIds = ref([])

// 平台实时时钟：epoch 是后端给的**绝对时刻**（宿主时钟），本地按秒自增，
// 每次状态轮询回来重新锚定（防本机时钟漂移累积）。
// 显示口径 = **本机（浏览器）时区**，与 fmtTime() 一致：同一屏不能出现两个时区的时间。
// 宿主时区仍取回来，只用于"两者不一致时"给出说明，不参与本卡的时间渲染。
const clockRef = reactive({ epoch: 0, baseMs: 0, hostTzMin: 0, hostTzName: '' })
const clockText = ref('--:--:--')
const clockDateText = ref('')
const clockLocalTzMin = ref(-new Date().getTimezoneOffset())

// 下发模板：draft 就是「本次下发会用的模板」，saved 是服务器当前值（用于判断改动）
const templateDraft = reactive(emptyTemplateMap())
const templateSaved = reactive(emptyTemplateMap())
const templateDefaults = reactive(emptyTemplateMap())
const templateMeta = reactive({
  h3c_apply: {}, h3c_rollback: {}, huawei_apply: {}, huawei_rollback: {},
})
const templatePlaceholders = ref([])
const templateLimits = ref({ max_lines: 40, max_line_len: 200 })
const templateSaveNote = ref('')
const templatesLoaded = ref(false)
const templateDialogOpen = ref(false)
const templateTab = ref('h3c')
const savingTemplate = ref(false)
const loadingPreview = ref(false)

const results = ref([])
const applying = ref(false)
const applyDone = ref(0)
const applyTotal = ref(0)
const applyPhase = ref('')

const togglingServer = ref(false)
const togglingUpstream = ref(false)
const savingUpstream = ref(false)
const savingThreshold = ref(false)
const manualSyncing = ref(false)
const sampling = ref(false)
// 手动同步的结果（成功/失败都留在卡片里，直到用户关掉）。
// 用持久面板而不是轻提示：失败诊断有 3~5 行，4 秒的 toast 根本读不完。
const syncResult = ref(null)

const historyHours = ref(24)
const chartEl = ref(null)
let chart = null
let offTheme = null
let timer = null
let historyTimer = null
let clockTimer = null

const flashMsg = ref('')
const flashOk = ref(true)
let flashTimer = null
function flash(text, ok = true) {
  flashMsg.value = text
  flashOk.value = ok
  clearTimeout(flashTimer)
  flashTimer = setTimeout(() => { flashMsg.value = '' }, 4000)
}

const confirmDialog = reactive({
  open: false,
  title: '',
  message: '',
  commands: '',
  note: '',
  confirmText: '确认',
  danger: false,
  busy: false,
  onConfirm: () => {},
})

// 失败原因 / 下发详情弹窗。与上面「二次确认」分开：确认弹窗是"将要做什么"（危险动作前），
// 这个是"已经发生了什么"（事后取证），混淆会让危险的红色按钮出现在只读场景里。
const detailDialog = reactive({
  open: false,
  title: '',
  subtitle: '',
  hint: '',
  reasonLabel: '失败原因',
  reason: '',
  warnings: [],
  commands: '',
  readback: '',
  meta: [],
})

// ---------- 工具 ----------
function fmtNum(n) {
  if (n === null || n === undefined) return '-'
  return Number(n).toLocaleString('zh-CN')
}
function fmtSeconds(v, digits = 3) {
  if (v === null || v === undefined) return '-'
  return Number(v).toFixed(digits)
}
// 带符号展示原始偏差（0 不显示正号）
function signed(v) {
  if (v === null || v === undefined) return '-'
  const n = Number(v)
  if (!Number.isFinite(n)) return '-'
  if (Math.abs(n) < 0.0005) return '0.000'
  return (n > 0 ? '+' : '-') + Math.abs(n).toFixed(3)
}
function fmtTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return '-'
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
// 近期请求表用的紧凑时刻（HH:MM:SS）。表格列窄，且这些请求都在最近几分钟内，
// 带日期反而把列撑爆；完整时刻放在 title 里，需要精确到日时再悬停看。
function fmtHms(ts) {
  if (!ts) return '-'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return '-'
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
// 轮询间隔：小间隔用秒（NTP minpoll=6 → 64s 起步，秒级最直观），
// 大间隔用 分+秒（maxpoll=10 → 1024s，写成 "17m04s" 才好和理论值对照）。
function fmtInterval(v) {
  if (v === null || v === undefined) return '-'
  const n = Number(v)
  if (!Number.isFinite(n) || n < 0) return '-'
  // 亚秒间隔：设备侧的突发（同一周期内连发几个包）会让相邻两条只差零点几秒。
  // 这时显示 "0s" 是把信息抹掉了（读起来像"没有间隔"），所以单独标成 <1s ——
  // 也让人一眼看出"这条不是轮询周期，是突发"。
  if (n < 1) return '<1s'
  if (n < 120) return `${Math.round(n)}s`
  const m = Math.floor(n / 60)
  const s = Math.round(n - m * 60)
  // 四舍五入可能凑出 60s，进位到分钟，避免出现 "5m60s"
  return s === 60 ? `${m + 1}m00s` : `${m}m${String(s).padStart(2, '0')}s`
}

const STATUS_BADGE = {
  applied: 'badge-success',
  unverified: 'badge-warning',
  failed: 'badge-danger',
  pending: 'badge-neutral',
}
const STATUS_LABEL = {
  applied: '已生效',
  unverified: '待确认',
  failed: '失败',
  pending: '进行中',
}
function statusBadgeClass(s) {
  return STATUS_BADGE[s] || 'badge-neutral'
}
function statusLabel(s) {
  return STATUS_LABEL[s] || s || '-'
}
// 单条结果的三态：显式 state 优先，其次由 ok 推导（与 loghost 同口径）
function resultState(r) {
  return r?.state || (r?.ok ? 'applied' : 'failed')
}
function resultLabel(r) {
  if (resultState(r) === 'unverified') return '待确认'
  return r?.ok ? '已生效' : '失败'
}

// ---------- 派生状态 ----------
// 开关的「已开启」判定：先看用户开关，再看部署级总闸
const serverEnabled = computed(() => !!(status.value && status.value.server_enabled))
const upstreamEnabled = computed(() => !!(status.value && status.value.upstream_enabled))

// 授权：配置存在但未开 → 「已配置，未开启」；开了但没跑起来 → 「已开启，但未运行」
const serverStateText = computed(() => {
  const s = status.value
  if (!s) return '未知'
  if (!s.server_enabled) return s.server_configured ? '已配置 · 当前关闭' : '已关闭'
  if (s.server_running && s.bound) return '运行中'
  if (s.server_running) return '已开启 · 端口未绑定'
  return '已开启 · 未运行'
})
const serverDotClass = computed(() => {
  const s = status.value
  if (!s || !s.server_enabled) return 'status-dot-offline'
  if (s.server_running && s.bound) return 'status-dot-online'
  return 'status-dot-warning'
})
const serverTextClass = computed(() => {
  const s = status.value
  if (!s || !s.server_enabled) return 'text-ink-muted'
  if (s.server_running && s.bound && !s.bind_error) return 'text-success'
  return 'text-warning'
})

const upstreamStateText = computed(() => {
  const s = status.value
  if (!s) return '未知'
  if (!s.upstream_enabled) return s.upstream_configured ? '已配置 · 当前关闭' : '已关闭'
  if (s.upstream_running) return '运行中'
  return '已开启 · 未运行'
})
const upstreamDotClass = computed(() => {
  const s = status.value
  if (!s || !s.upstream_enabled) return 'status-dot-offline'
  return s.upstream_running ? 'status-dot-online' : 'status-dot-warning'
})
const upstreamTextClass = computed(() => {
  const s = status.value
  if (!s || !s.upstream_enabled) return 'text-ink-muted'
  return s.upstream_running ? 'text-success' : 'text-warning'
})

// 页头徽标：只看平台整体（两条链路任一在跑即视为运行中）
const headerBadgeText = computed(() => {
  const s = status.value
  if (!s) return '加载中'
  if (!s.server_enabled && !s.upstream_enabled) return '未开启'
  if (s.server_enabled && s.server_running && s.bound) return '授时运行中'
  if (s.upstream_enabled && s.upstream_running) return '取时运行中'
  if (s.server_enabled || s.upstream_enabled) return '已开启 · 未就绪'
  return '未开启'
})
const headerBadgeClass = computed(() => {
  const s = status.value
  if (!s) return 'badge badge-neutral'
  if (!s.server_enabled && !s.upstream_enabled) return 'badge badge-neutral'
  if ((s.server_enabled && s.server_running && s.bound) || (s.upstream_enabled && s.upstream_running)) {
    return 'badge badge-success'
  }
  return 'badge badge-warning'
})
const headerDotClass = computed(() => {
  const s = status.value
  if (!s || (!s.server_enabled && !s.upstream_enabled)) return 'status-dot-offline'
  if ((s.server_enabled && s.server_running && s.bound) || (s.upstream_enabled && s.upstream_running)) {
    return 'status-dot-online'
  }
  return 'status-dot-warning'
})

// 偏差方向：offset = 上游 - 平台。>0 → 平台慢，需向后拨；<0 → 平台快
const directionText = computed(() => {
  const d = offset.value && offset.value.direction
  if (d === 'slow') return '平台偏慢'
  if (d === 'fast') return '平台偏快'
  if (d === 'synced') return '已对齐'
  return '未知'
})
const directionClass = computed(() => {
  const d = offset.value && offset.value.direction
  if (d === 'synced') return 'text-success'
  return 'text-warning'
})
const directionIcon = computed(() => {
  const d = offset.value && offset.value.direction
  if (d === 'fast') return ArrowUpIcon
  if (d === 'slow') return ArrowDownIcon
  return CheckCircleIcon
})
const offsetValueClass = computed(() => {
  if (!offset.value || !offset.value.available) return 'text-ink'
  return offset.value.exceeded ? 'text-danger' : 'text-ink-strong'
})

// 偏差值与单位拆开渲染：偏差通常是毫秒级，显示「+3.0 毫秒」比「+0.003 秒」直观得多
const offsetParts = computed(() => {
  const o = offset.value
  if (!o || !o.available || o.offset_seconds === null || o.offset_seconds === undefined) {
    return { value: '-', unit: '' }
  }
  const n = Number(o.offset_seconds)
  if (!Number.isFinite(n)) return { value: '-', unit: '' }
  if (Math.abs(n) < 0.0005) return { value: '0', unit: '毫秒' }
  const sign = n > 0 ? '+' : '-'
  const a = Math.abs(n)
  // 1 秒以内一律按毫秒：平台偏差常态就是毫秒级（实测 2~20ms），
  // 分界卡在 0.01 会让单位在临界点上来回横跳，同一个页面看着像在抖。
  if (a < 1) return { value: `${sign}${(a * 1000).toFixed(1)}`, unit: '毫秒' }
  return { value: `${sign}${a.toFixed(2)}`, unit: '秒' }
})

// 手动同步上限：超过则按钮置灰（后端也会返 422）
const manualSyncBlocked = computed(() => {
  const s = status.value
  const o = offset.value
  if (!s || !o || !o.available) return false
  const max = s.manual_sync_max_seconds
  if (max === null || max === undefined) return false
  return Number(o.abs_seconds) > Number(max)
})

// ---------- 平台实时时钟 ----------
// 分钟偏移 → `UTC+08:00`（传分钟而不是小时：半小时时区如 UTC+05:30 会算歪）
function fmtTzOffset(minutes) {
  const m = Number(minutes) || 0
  const sign = m >= 0 ? '+' : '-'
  const a = Math.abs(m)
  return `UTC${sign}${String(Math.floor(a / 60)).padStart(2, '0')}:${String(a % 60).padStart(2, '0')}`
}

const clockTimezoneText = computed(() => fmtTzOffset(clockLocalTzMin.value))
const hostTzText = computed(() => fmtTzOffset(clockRef.hostTzMin))
// 宿主与本机时区不同 → 卡片右上角给一句说明（.108 就是宿主 UTC、本机 +8 的情形）
const hostTzMismatch = computed(
  () => Number(clockRef.hostTzMin) !== Number(clockLocalTzMin.value),
)

// 后端 /ntp/status 每次轮询都会带回宿主时间，用它重新锚定（防本地时钟漂移累积）
function applyServerTime(s) {
  if (!s || s.server_time_epoch === null || s.server_time_epoch === undefined) return
  clockRef.epoch = Number(s.server_time_epoch)
  clockRef.baseMs = Date.now()
  clockRef.hostTzMin = Number(s.server_timezone_offset_minutes || 0)
  clockRef.hostTzName = s.server_timezone_name || ''
  // 本机偏移可能随夏令时变化，随轮询一起刷新
  clockLocalTzMin.value = -new Date().getTimezoneOffset()
  tickClock()
}

// 时间轴 = 服务器 epoch + 本地流逝秒数，再按**本机时区**渲染成墙上时间。
// 这里刻意用 getHours()/getDate() 而不是 getUTC*：让浏览器时区参与换算，
// 得到的口径就和页面其它时间戳（fmtTime）完全一致。
function tickClock() {
  if (!clockRef.epoch) {
    clockText.value = '--:--:--'
    clockDateText.value = ''
    return
  }
  const hostMs = (clockRef.epoch + (Date.now() - clockRef.baseMs) / 1000) * 1000
  const d = new Date(hostMs)
  const p = (n) => String(n).padStart(2, '0')
  clockText.value = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  clockDateText.value =
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${WEEK_CN[d.getDay()]}`
}

// ---------- 设备列表 ----------
// DeviceResponse 不含 is_mdc 字段，按后端口径（型号/名称含 MDC）在前端推导，仅作提示
function isMdc(d) {
  const text = `${d.model || ''} ${d.name || ''}`.toUpperCase()
  return text.includes('MDC')
}
const selectedMdCount = computed(() => {
  const set = new Set(selectedIds.value)
  return devices.value.filter((d) => set.has(d.id) && isMdc(d)).length
})
const filteredDevices = computed(() => {
  const kw = deviceFilter.value.toLowerCase()
  if (!kw) return devices.value
  return devices.value.filter(
    (d) => (d.name || '').toLowerCase().includes(kw) || (d.ip || '').includes(kw),
  )
})
const allSelected = computed(
  () => filteredDevices.value.length > 0 && filteredDevices.value.every((d) => selectedIds.value.includes(d.id)),
)
const hasAnyRecord = computed(() => Object.keys(deviceStatusMap.value).length > 0)

// ---------- 失败原因（可查看） ----------
// 后端留的是**最近一条失败记录**，而不是"最新一条记录"：失败→重试成功之后，
// 成功记录不该把失败原因顶掉 —— "上次为什么失败"正是决定要不要再发、模板要不要改的依据。
function deviceFailure(d) {
  const st = deviceStatusMap.value[d.id]
  if (!st || st.status !== 'failed') return null
  return st.last_failed || {
    message: st.message,
    at: null,
    address: st.server_address,
    clock_mode: st.clock_mode,
    operator: st.operator,
    commands: null,
  }
}
function statusTitle(d) {
  const st = deviceStatusMap.value[d.id]
  if (!st) return ''
  const bits = []
  if (st.message) bits.push(st.message)
  if (st.applied_at) bits.push(`生效时间 ${fmtTime(st.applied_at)}`)
  if (st.operator) bits.push(`操作人 ${st.operator}`)
  return bits.join('\n')
}
function truncateText(text, max = 4000) {
  const s = text === null || text === undefined ? '' : String(text)
  if (s.length <= max) return s
  return `${s.slice(0, max)}\n…（已截断，完整内容共 ${s.length} 字符）`
}
// 结果表「说明」列的摘要：error 是后端的判定文案（自带诊断与建议）
function resultBrief(r) {
  const warn = Array.isArray(r?.warnings) ? r.warnings.join('; ') : ''
  return r?.error || r?.message || warn || '-'
}

function closeDetail() {
  detailDialog.open = false
}

// 从设备列表打开：数据来自**下发记录**，所以刷新页面后仍可查（结果区会清空，它不会）
function openDeviceFailure(d) {
  const f = deviceFailure(d)
  if (!f) return
  const meta = []
  if (d.ip) meta.push({ k: '设备 IP', v: d.ip })
  if (f.address) meta.push({ k: '下发地址', v: f.address })
  if (f.clock_mode) meta.push({ k: '时钟协议', v: f.clock_mode })
  if (f.at) meta.push({ k: '失败时间', v: fmtTime(f.at) })
  if (f.operator) meta.push({ k: '操作人', v: f.operator })
  // 该设备此刻在不在取时，决定这条「失败」该怎么读。
  // 注意不能一律归因于"回读误判"：现场就有一台失败原因是 No route to host（当时根本连不上），
  // 后来网络恢复、它自己就来取时了 —— 这类"当时不通、现已恢复"和"一直通只是回读读不到"
  // 是两种完全不同的成因，含糊其辞会把人引向错误的排查方向。
  const s = syncInfo(d)
  const msg = f.message || ''
  const connLike = /OSError|No route|refused|unreachable|timed?\s?out/i.test(msg)
  detailDialog.open = true
  detailDialog.title = `${d.name || d.ip || d.id} 下发失败`
  detailDialog.subtitle = '取自设备最近一条失败记录（此后重试成功也仍保留）'
  detailDialog.hint = s
    ? `该设备本次启动以来已在与平台取时（${s.count} 次，最近 ${fmtTime(s.last_at)}）——`
      + '它现在确实在对时，无需重发。'
      + (connLike
        ? '下方原因是连接类异常，说明当时连不上、现已恢复（网络/ACL 或设备侧状态变化）。'
        : '下方原因更像回读判定的偏差（此前那次的快照读法与该型号不匹配），请对照回读结果判断。')
    : ''
  detailDialog.reasonLabel = '失败原因（含下一步建议）'
  detailDialog.reason = f.message
    || '（该记录未写入原因，请查看下方执行的命令与设备返回）'
  detailDialog.warnings = []
  detailDialog.commands = f.commands || ''
  detailDialog.readback = ''
  detailDialog.meta = meta
}

// 从结果表打开：数据来自**本次下发的原始返回**（含设备逐条报错与回读）
function openResultDetail(r) {
  const st = resultState(r)
  const meta = []
  if (r.ip) meta.push({ k: '设备 IP', v: r.ip })
  if (r.commands) {
    meta.push({ k: '命令条数', v: String(String(r.commands).split('\n').filter(Boolean).length) })
  }
  detailDialog.open = true
  detailDialog.title = `${r.device_name || r.ip || r.device_id} · ${resultLabel(r)}`
  detailDialog.subtitle = st === 'failed'
    ? '下发未生效。下面是判定依据与设备原始返回 —— 重发前请先按建议核对'
    : st === 'unverified'
      ? '命令已下发且无报错，但回读不可判定（可能已生效）。请登录设备人工确认后再决定是否重发'
      : '命令已下发，回读确认目标 NTP 服务器存在'
  // 与设备列表同一套反证：设备正在取时 → 它现在确实在对时，不必重发
  const dev = devices.value.find((x) => x.id === r.device_id)
  const s = dev ? syncInfo(dev) : null
  detailDialog.hint = s && st !== 'applied'
    ? `该设备本次启动以来已在与平台取时（${s.count} 次，最近 ${fmtTime(s.last_at)}）——`
      + '它现在确实在对时，无需重发（本次结果多半是回读判定或链路一时不通所致）。'
    : ''
  detailDialog.reasonLabel = st === 'failed' ? '失败原因（含下一步建议）' : '判定说明'
  detailDialog.reason = r.error || r.message
    || (st === 'applied' ? '回读确认「目标 NTP 服务器」已在设备配置中。' : '（无）')
  detailDialog.warnings = Array.isArray(r.warnings) ? r.warnings : []
  detailDialog.commands = r.commands || ''
  detailDialog.readback = truncateText(r.after)
  detailDialog.meta = meta
}

// ---------- 近期请求（下行授时） ----------
// 「近期请求」列表（后端环形缓冲，最新在前）
const recentRequests = computed(
  () => (status.value && status.value.recent_requests) || [],
)
// IP → 设备名（用设备表反查；查不到就退回显示 IP，不臆造名字）
const deviceNameByIp = computed(() => {
  const m = {}
  for (const d of devices.value) {
    const ip = d && d.ip ? String(d.ip).trim() : ''
    if (ip) m[ip] = d.name || ''
  }
  return m
})

// 本次启动以来的来源台账。
// 为什么还需要它：上面那张表只有 20 条，而设备轮询节奏能差出量级 ——
// 实测有过一批设备处于启动突发（1 秒一发），20 条只够覆盖约 20 秒，
// 一台 64 秒前才来过的设备会被整个挤出窗口、在页面上表现成"从没来过"。
// 而这正是反向诊断最怕的误判（把"来过但被窗口吃掉"读成"没来"，直奔错误方向）。
// 台账不受窗口影响，"不在这张表里"才是可信的"它没来过"。
const recentSources = computed(
  () => (status.value && status.value.recent_sources) || [],
)

// ---------- 客户端在线（本次启动以来的来源台账） ----------
// 带设备名反查。**未纳管的来源也要列**（`managed: false`）：设备表里查不到名字
// 不等于"不是客户端"——多一个陌生 IP 在取时，往往比少一台设备更值得看一眼
// （地址填错、串了别的网段、甚至非本系统的设备指向了这里）。
// 排序：已纳管在前（按名称）——"查设备"总是先查纳管列表，异常项放最后不干扰主视线。
const onlineClients = computed(() => {
  const list = recentSources.value.map((s) => {
    const ip = String(s.client_ip || '').trim()
    const name = deviceNameByIp.value[ip] || ''
    return {
      key: ip,
      ip,
      name,
      managed: !!name,
      count: s.count,
      answered: s.answered,
      first_at: s.first_at,
      last_at: s.last_at,
      interval_seconds: s.interval_seconds,
    }
  })
  list.sort((a, b) => {
    if (a.managed !== b.managed) return a.managed ? -1 : 1
    return a.managed ? a.name.localeCompare(b.name, 'zh') : a.ip.localeCompare(b.ip)
  })
  return list
})
const onlineUnmanaged = computed(() => onlineClients.value.filter((c) => !c.managed))

// 未同步名单：带「最近下发」状态。
// 这一列是为了让「配了但没跑」露出来 —— 下发记录「已生效」+ 这里「未同步」，
// 说明命令确实进了配置、但设备并没在跟平台对时（服务没起 / ACL 不通 / 地址填错）。
// （匹配口径与设备列表的「已同步」列共用 `unsyncedDevices`，只有一份真源。）
const unsyncedClients = computed(() =>
  unsyncedDevices.value
    .map((d) => ({
      key: `d${d.id}`,
      ip: d.ip,
      name: d.name || '',
      mdc: isMdc(d),
      status: (deviceStatusMap.value[d.id] || {}).status || null,
      note: '本次启动以来未向平台发起过 NTP 请求',
    }))
    .sort((a, b) => String(a.name).localeCompare(String(b.name), 'zh')),
)

const clientDialog = reactive({ open: false, mode: 'online' })
const clientRows = computed(() =>
  clientDialog.mode === 'online' ? onlineClients.value : unsyncedClients.value,
)
const clientDialogTitle = computed(() =>
  clientDialog.mode === 'online' ? '客户端在线' : '客户端未同步',
)
const clientDialogSubtitle = computed(() => {
  if (clientDialog.mode === 'online') {
    const managed = onlineClients.value.length - onlineUnmanaged.value.length
    const parts = [`本次启动以来共 ${onlineClients.value.length} 个来源取时`,
      `其中已纳管 ${managed} 台`]
    if (onlineUnmanaged.value.length) parts.push(`未纳管 ${onlineUnmanaged.value.length} 个`)
    return `${parts.join(' · ')}（重启平台后归零）`
  }
  return `设备表共 ${devices.value.length} 台，其中 ${unsyncedClients.value.length} 台本次启动以来尚未取时`
})
function openClientDialog(mode) {
  clientDialog.mode = mode
  clientDialog.open = true
}
// 复制"名称 + IP"清单，方便直接拿去设备侧核对。
// 不直接用 navigator.clipboard：它要求安全上下文，用 http:// 访问平台时是 undefined，
// 会静默失败 —— 所以留一条 textarea + execCommand 的老路（本地部署常见 http 访问）。
async function copyClientList() {
  const rows = clientRows.value
  if (!rows.length) return
  const text = rows.map((r) => (r.name ? `${r.name}\t${r.ip}` : r.ip)).join('\n')
  let ok = false
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      ok = true
    }
  } catch {
    ok = false
  }
  if (!ok) {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'
      ta.style.top = '-1000px'
      document.body.appendChild(ta)
      ta.select()
      ok = document.execCommand('copy')
      document.body.removeChild(ta)
    } catch {
      ok = false
    }
  }
  flash(ok ? `已复制 ${rows.length} 条（名称 + IP）` : '复制失败，请手动选中表格内容复制', ok)
}

// ---------- 对时状态（设备列表的「已同步」） ----------
// 判据 = 设备**真的回来取过时**（来源台账里有它的 IP），而不是"我们下发成功过"。
// 这不是咬文嚼字：下发成功只说明命令发出、且回读到了配置；若设备侧 NTP 服务没起来、
// 或中间 ACL 不放通 UDP 123，它照样不会来取时 —— 那种设备在「最近下发」里是「已生效」，
// 在「对时状态」里才露馅。两列并排看，正好区分「配了」和「在跑」。
// 同一张表里还能一眼看出：谁在按周期取时、间隔是多少、谁压根没出现（反向诊断入口）。
const syncInfoByIp = computed(() => {
  const m = {}
  for (const s of recentSources.value) {
    if (s && s.client_ip) m[String(s.client_ip).trim()] = s
  }
  return m
})
function syncInfo(d) {
  if (!d || !d.ip) return null
  return syncInfoByIp.value[String(d.ip).trim()] || null
}
function syncTooltip(d) {
  const s = syncInfo(d)
  if (!s) return ''
  const parts = [`本次启动以来来取时 ${s.count} 次`, `最近 ${fmtTime(s.last_at)}`]
  if (s.interval_seconds !== null && s.interval_seconds !== undefined) {
    parts.push(`间隔 ${fmtInterval(s.interval_seconds)}`)
  }
  if (s.answered < s.count) parts.push(`其中 ${s.count - s.answered} 次未应答`)
  return parts.join(' · ')
}
// 本次启动以来一次都没来取过时的设备（设备侧没配 / ACL 不通，两类都在这）。
// 这是「客户端未同步」与设备列表「未同步」共同的唯一判据 —— 别在别处再算一遍。
const unsyncedDevices = computed(() => devices.value.filter((d) => !syncInfo(d)))
const selectedSyncedCount = computed(() => {
  const set = new Set(selectedIds.value)
  return devices.value.filter((d) => set.has(d.id) && syncInfo(d)).length
})
const selectedSyncedNames = computed(() => {
  const set = new Set(selectedIds.value)
  return devices.value.filter((d) => set.has(d.id) && syncInfo(d)).map((d) => d.name || d.ip)
})

const summary = computed(() => {
  const c = { applied: 0, unverified: 0, failed: 0 }
  for (const r of results.value) {
    const st = resultState(r)
    if (st === 'applied') c.applied += 1
    else if (st === 'unverified') c.unverified += 1
    else c.failed += 1
  }
  return c
})
const applyPercent = computed(() =>
  applyTotal.value ? Math.round((applyDone.value / applyTotal.value) * 100) : 0,
)

// 能否「立即下发」：管理员 + 选了设备 + 有下发地址
const canApply = computed(() => (
  isAdmin.value && selectedIds.value.length > 0 && !!addressInput.value && !applying.value
))

// 右侧面板的模板摘要：让人在点下发之前就知道"这次用的是默认还是改过的命令"
const templateSummary = computed(() => {
  if (!templatesLoaded.value) return '加载中…'
  const h3c = !!(templateMeta.h3c_apply && templateMeta.h3c_apply.customized)
  const hw = !!(templateMeta.huawei_apply && templateMeta.huawei_apply.customized)
  return `H3C ${h3c ? '自定义' : '默认'} / 华为 ${hw ? '自定义' : '默认'}`
})

const templateTabs = computed(() => TEMPLATE_TABS)
const currentTabBlocks = computed(() => {
  const v = templateTab.value
  return [
    { key: `${v}_apply`, label: '下发命令模板' },
    { key: `${v}_rollback`, label: '回滚命令模板' },
  ]
})
function savedTemplateMeta(key) {
  return templateMeta[key] || {}
}

function selectAll() {
  if (allSelected.value) {
    const visible = new Set(filteredDevices.value.map((d) => d.id))
    selectedIds.value = selectedIds.value.filter((id) => !visible.has(id))
  } else {
    const set = new Set(selectedIds.value)
    for (const d of filteredDevices.value) set.add(d.id)
    selectedIds.value = [...set]
  }
}

// 「选未同步」：排除已同步的（本次启动以来已在取时），只把真正还没对上的选出来。
// 刻意做成「替换选择」而不是「追加」：这个按钮的用途就是"这次只发给还没对上的那些"，
// 追加会让上一轮选的已同步设备残留下来，恰好是它要避免的事。
function selectUnsynced() {
  const targets = filteredDevices.value.filter((d) => !syncInfo(d))
  if (!targets.length) {
    flash('当前范围内的设备都已在取时，无需重复下发', true)
    return
  }
  const skipped = filteredDevices.value.length - targets.length
  selectedIds.value = targets.map((d) => d.id)
  flash(
    skipped
      ? `已选 ${targets.length} 台未同步设备（跳过 ${skipped} 台已同步）`
      : `已选 ${targets.length} 台未同步设备`,
    true,
  )
}

// ---------- 数据加载 ----------
async function loadMe() {
  try {
    const me = await getMe()
    isAdmin.value = me && me.role === 'admin'
  } catch (e) {
    isAdmin.value = false
  } finally {
    meLoaded.value = true
  }
}

async function loadStatus() {
  try {
    const s = await getNtpStatus()
    status.value = s
    // 每次拿到状态都重新锚定实时时钟（抵消本地时钟漂移）
    applyServerTime(s)
    // 首次填充输入框，避免覆盖用户正在编辑的内容
    if (upstreamServerInput.value === '') upstreamServerInput.value = s.upstream_server || ''
    if (s.offset_threshold_seconds !== undefined && s.offset_threshold_seconds !== null) {
      thresholdInput.value = s.offset_threshold_seconds
    }
  } catch (e) {
    /* 错误已由拦截器提示 */
  }
}

async function loadOffset() {
  try {
    const o = await getNtpOffset()
    offset.value = o
    if (o && o.available && o.threshold_seconds !== undefined && o.threshold_seconds !== null) {
      thresholdInput.value = o.threshold_seconds
    }
  } catch (e) {
    /* ignore */
  }
}

async function loadHistory() {
  try {
    const data = await getNtpOffsetHistory(historyHours.value)
    historyStats.value = data || {}
    await nextTick()
    renderChart(data && data.points ? data.points : [])
  } catch (e) {
    historyStats.value = {}
  }
}

async function loadCandidates() {
  loadingDevices.value = true
  try {
    // 设备列表 page_size 后端限制 ≤300，多页拉取保证下拉完整
    const [c, st] = await Promise.all([getNtpCandidates(), getNtpDeviceStatus([])])
    candidates.value = ((c && c.candidates) || []).filter((x) => typeof x === 'string')
    recommendedAddress.value = (c && c.configured) || ''
    devices.value = await fetchAllDevices()
    // 默认选中推荐地址
    if (!addressInput.value) {
      if (recommendedAddress.value) addressInput.value = recommendedAddress.value
      else if (candidates.value.length) addressInput.value = candidates.value[0]
    }
    const map = {}
    for (const item of (st && st.items) || []) {
      map[item.device_id] = item
    }
    deviceStatusMap.value = map
  } catch (e) {
    /* ignore */
  } finally {
    loadingDevices.value = false
  }
}

// 分页拉取全部设备（后端 page_size 上限 300，加 page_size=300 上限保护）
async function fetchAllDevices() {
  const PAGE = 300
  const all = []
  for (let page = 1; page <= 20; page += 1) {
    const res = await getDevices({ page, page_size: PAGE })
    const items = (res && res.items) || []
    all.push(...items)
    const total = (res && res.total) || all.length
    if (all.length >= total || items.length < PAGE) break
  }
  return all
}

// 设备最近下发状态：在已有设备列表中按当前可见项刷新
async function refreshDeviceStatus() {
  const ids = devices.value.map((d) => d.id)
  if (!ids.length) return
  try {
    const st = await getNtpDeviceStatus(ids)
    const map = {}
    for (const item of (st && st.items) || []) map[item.device_id] = item
    deviceStatusMap.value = map
  } catch (e) {
    /* ignore */
  }
}

async function loadAll() {
  loading.value = true
  try {
    await Promise.all([loadStatus(), loadOffset(), loadHistory()])
  } finally {
    loading.value = false
  }
}

// ---------- 开关 ----------
async function toggleServer() {
  if (!isAdmin.value || togglingServer.value) return
  togglingServer.value = true
  const target = !serverEnabled.value
  try {
    const res = await updateNtpSettings({ server_enabled: target })
    if (res && res.status) status.value = { ...status.value, ...res.status }
    else await loadStatus()
    flash(target ? '下行授时已开启' : '下行授时已关闭', true)
    await loadOffset()
  } catch (e) {
    const detail = e?.response?.data?.detail
    flash(detail || (target ? '开启失败' : '关闭失败'), false)
    await loadStatus()
  } finally {
    togglingServer.value = false
  }
}

async function toggleUpstream() {
  if (!isAdmin.value || togglingUpstream.value) return
  togglingUpstream.value = true
  const target = !upstreamEnabled.value
  try {
    const res = await updateNtpSettings({ upstream_enabled: target })
    if (res && res.status) status.value = { ...status.value, ...res.status }
    else await loadStatus()
    flash(target ? '上行取时已开启' : '上行取时已关闭', true)
  } catch (e) {
    const detail = e?.response?.data?.detail
    flash(detail || (target ? '开启失败' : '关闭失败'), false)
    await loadStatus()
  } finally {
    togglingUpstream.value = false
  }
}

async function saveUpstream() {
  if (!isAdmin.value || savingUpstream.value) return
  const val = (upstreamServerInput.value || '').trim()
  if (!val) {
    flash('时间源不能为空', false)
    return
  }
  savingUpstream.value = true
  try {
    const res = await updateNtpSettings({ upstream_server: val })
    if (res && res.status) status.value = { ...status.value, ...res.status }
    else await loadStatus()
    flash('上游时间源已保存', true)
  } catch (e) {
    flash(e?.response?.data?.detail || '保存失败', false)
    await loadStatus()
  } finally {
    savingUpstream.value = false
  }
}

async function saveThreshold() {
  if (!isAdmin.value || savingThreshold.value) return
  const v = Number(thresholdInput.value)
  if (!Number.isFinite(v) || v <= 0 || v > 3600) {
    flash('阈值需为 1~3600 之间的秒数', false)
    return
  }
  savingThreshold.value = true
  try {
    const res = await updateNtpSettings({ offset_threshold_seconds: v })
    if (res && res.status) status.value = { ...status.value, ...res.status }
    else await loadStatus()
    flash('偏差阈值已保存', true)
    await Promise.all([loadOffset(), loadHistory()])
  } catch (e) {
    flash(e?.response?.data?.detail || '保存失败', false)
    await loadStatus()
  } finally {
    savingThreshold.value = false
  }
}

// ---------- 手动同步 / 立即采样 ----------
function askManualSync() {
  if (!isAdmin.value) return
  const o = offset.value || {}
  confirmDialog.open = true
  confirmDialog.title = '手动同步平台时间'
  confirmDialog.message =
    `将把平台时间调整到上游时间源（${o.server || '当前时间源'}）。` +
    `当前偏差 ${signed(o.offset_seconds)} 秒（${directionText.value}）。同步会立即跳变系统时钟，` +
    '可能影响正在进行的采集与告警判定，请确认偏差在合理范围内。'
  confirmDialog.confirmText = '确认同步'
  confirmDialog.danger = true
  confirmDialog.commands = ''
  confirmDialog.note = '平台平时不会自动调整时钟，这是唯一会改动平台自身时间的操作。'
  confirmDialog.onConfirm = doManualSync
}

async function doManualSync() {
  confirmDialog.busy = true
  manualSyncing.value = true
  syncResult.value = null
  try {
    const res = await manualNtpSync()
    confirmDialog.open = false
    // 成功后展示后端原话（含它自己的复核残差），不在这里另编一句 ——
    // "改过时钟"和"时钟已对齐"是两回事，数字必须来自真实复核。
    syncResult.value = {
      ok: true,
      title: `同步完成：校正 ${fmtOffset(res && res.offset_seconds)}`,
      detail: (res && res.message) || '宿主时钟已校正。',
    }
    flash('已校正平台时钟', true)
    await loadAll()
  } catch (e) {
    // 后端对同步失败返 422，detail 是可读的诊断（逐条策略 + 处置办法）
    confirmDialog.open = false
    const detail = e?.response?.data?.detail
    syncResult.value = {
      ok: false,
      title: '同步失败：平台时钟未改动',
      detail: typeof detail === 'string' && detail
        ? detail
        : '未能取得失败原因，请查看后端日志。',
    }
    flash('同步失败，见「时间校准」卡片内的说明', false)
  } finally {
    confirmDialog.busy = false
    manualSyncing.value = false
  }
}

async function doSampleNow() {
  if (!isAdmin.value || sampling.value) return
  sampling.value = true
  try {
    await loadOffset()
    flash('已刷新偏差采样', true)
    await loadHistory()
  } finally {
    sampling.value = false
  }
}

// ---------- 命令模板 ----------
async function loadTemplates() {
  try {
    const res = await getNtpTemplates()
    for (const item of (res && res.items) || []) {
      applyTemplateItem(item, `${item.vendor}_${item.action}`)
    }
    templatePlaceholders.value = (res && res.placeholders) || []
    if (res && res.limits) templateLimits.value = res.limits
    templateSaveNote.value = (res && res.save_note) || ''
    templatesLoaded.value = true
  } catch (e) {
    /* 错误已由拦截器提示 */
  }
}

function applyTemplateItem(item, key) {
  if (!item || !TEMPLATE_KEYS.includes(key)) return
  const body = item.body || ''
  templateDraft[key] = body
  templateSaved[key] = body
  if (item.default_body !== undefined) templateDefaults[key] = item.default_body || ''
  templateMeta[key] = {
    customized: !!item.customized,
    operator: item.operator || '',
    updated_at: item.updated_at || '',
  }
}

// 本次下发的模板覆盖：把草稿原样带上，保证"看到的命令就是要发出去的命令"。
// 后端优先级是「本次覆盖 > 已保存 > 内置默认」，所以草稿等于默认值时带上也无害。
function templateOverrides() {
  if (!templatesLoaded.value) return {}
  const out = {}
  if (templateDraft.h3c_apply) out.h3c_commands = templateDraft.h3c_apply
  if (templateDraft.huawei_apply) out.huawei_commands = templateDraft.huawei_apply
  return out
}

function openTemplateDialog() {
  templateDialogOpen.value = true
}
function closeTemplateDialog() {
  templateDialogOpen.value = false
}

// 恢复默认 = 立刻落库（不是只改本地草稿）—— "恢复了却没保存"更让人困惑
async function resetTemplate(key) {
  if (!isAdmin.value || savingTemplate.value) return
  const [vendor, action] = key.split('_')
  savingTemplate.value = true
  try {
    const res = await saveNtpTemplate({ vendor, action, body: '' })
    applyTemplateItem(res && res.item, key)
    flash(
      `${vendor === 'h3c' ? 'H3C' : '华为'} ${action === 'apply' ? '下发' : '回滚'}模板已恢复默认`,
      true,
    )
  } catch (e) {
    flash(e?.response?.data?.detail || '恢复默认失败', false)
  } finally {
    savingTemplate.value = false
  }
}

// 只保存有改动的（避免"点一下保存就刷 4 条审计"）
async function saveTemplates() {
  if (!isAdmin.value || savingTemplate.value) return
  savingTemplate.value = true
  let changed = 0
  try {
    for (const key of TEMPLATE_KEYS) {
      if ((templateDraft[key] || '') === (templateSaved[key] || '')) continue
      const [vendor, action] = key.split('_')
      const res = await saveNtpTemplate({ vendor, action, body: templateDraft[key] })
      applyTemplateItem(res && res.item, key)
      changed += 1
    }
    flash(changed ? `已保存 ${changed} 套模板` : '模板没有改动', true)
  } catch (e) {
    flash(e?.response?.data?.detail || '保存模板失败', false)
    await loadTemplates()
  } finally {
    savingTemplate.value = false
  }
}

// ---------- 下发 / 回滚 ----------
// 「立即下发」：先向服务端要一遍命令（**不连设备**），把将要执行的命令摆进确认弹窗，
// 再执行 —— 预览与执行共用同一套模板解析，所以弹窗里看到的与实际发出的必然一致。
async function startApply() {
  if (!isAdmin.value) return
  if (!selectedIds.value.length) {
    flash('请先全选或勾选要下发的设备', false)
    return
  }
  if (!addressInput.value) {
    flash('请先选择下发地址', false)
    return
  }
  loadingPreview.value = true
  try {
    const p = await previewNtp({
      device_ids: [...selectedIds.value],
      address: addressInput.value,
      clock_mode: clockMode.value,
      save: saveConfig.value,
      ...templateOverrides(),
    })
    askApplyWithConfirm(p)
  } catch (e) {
    flash(e?.response?.data?.detail || '生成下发命令失败', false)
  } finally {
    loadingPreview.value = false
  }
}

// 预览结果按厂商拼成可读文本（看到什么就会发什么）
function applyCommandsText(p) {
  const parts = [`【H3C Comware】\n${(p.commands || []).join('\n')}`]
  if (p.has_huawei) parts.push(`【华为 VRP】\n${(p.huawei_commands || []).join('\n')}`)
  return parts.join('\n\n')
}

function askApplyWithConfirm(p) {
  if (!p) return
  confirmDialog.open = true
  confirmDialog.title = `确认向 ${selectedIds.value.length} 台设备下发 NTP 配置？`
  confirmDialog.message =
    `下发地址 ${p.address}，时钟协议 ${p.clock_mode}` +
    `${p.save ? '，并保存配置（H3C save force / 华为 save）' : '，不保存配置'}。` +
    '这会真实改动设备配置，请确认地址与设备范围无误。'
  confirmDialog.confirmText = '确认下发'
  confirmDialog.danger = true
  confirmDialog.commands = applyCommandsText(p)
  const notes = []
  // 已同步 = 本次启动以来该设备已在跟平台取时（来源台账里有它的 IP）。
  // 再发一次通常是无用功；更坏的是它会把"真正需要下发的那些"淹没在一片成功里，
  // 让人误以为"全发好了" —— 所以这里明说，并给出设备名，让人可以退回去改选择。
  if (selectedSyncedCount.value) {
    const names = selectedSyncedNames.value
    const shown = names.slice(0, 6).join('、')
    notes.push(
      `其中 ${selectedSyncedCount.value} 台已同步（${shown}`
      + `${names.length > 6 ? ` 等 ${names.length} 台` : ''}）：本次启动以来已在与平台对时，`
      + '通常无需重复下发。如需强制覆盖设备侧的手工改动，继续即可。',
    )
  }
  if (p.mdc_devices && p.mdc_devices.length) {
    notes.push(`注意：${p.mdc_devices.length} 台疑似 MDC 设备，当前协议为 ${p.clock_mode}。`)
  }
  if (p.using_custom_apply && (p.using_custom_apply.h3c || p.using_custom_apply.huawei)) {
    notes.push('本次使用了自定义命令模板（非内置定稿），请再核对上方命令。')
  }
  if (!notes.length) {
    notes.push(`下发按每批 ${APPLY_CHUNK} 台进行，可随时在结果区查看逐台状态。`)
  }
  confirmDialog.note = notes.join(' ')
  // 参数与模板在弹窗打开这一刻定下来，执行时不再重读表单 —— 否则用户打开弹窗后
  // 又去改了「保存配置」或模板，界面显示的是一套、实际执行的是另一套。
  const overrides = templateOverrides()
  confirmDialog.onConfirm = () =>
    doApply({
      device_ids: [...selectedIds.value],
      address: p.address,
      clock_mode: p.clock_mode,
      save: p.save,
      ...overrides,
    })
}

async function doApply(payload) {
  confirmDialog.busy = true
  applying.value = true
  results.value = []
  const ids = [...payload.device_ids]
  applyTotal.value = ids.length
  applyDone.value = 0
  const collected = []
  try {
    for (let i = 0; i < ids.length; i += APPLY_CHUNK) {
      const chunk = ids.slice(i, i + APPLY_CHUNK)
      applyPhase.value = `下发中（第 ${Math.floor(i / APPLY_CHUNK) + 1} / ${Math.ceil(ids.length / APPLY_CHUNK)} 批）`
      try {
        const res = await applyNtp({ ...payload, device_ids: chunk, confirm: true })
        collected.push(...((res && res.results) || []))
      } catch (e) {
        // 整批失败：为该批每台补一条失败记录，避免进度与结果不一致
        const detail = e?.response?.data?.detail || e.message || '请求失败'
        for (const id of chunk) collected.push({ device_id: id, ok: false, state: 'failed', error: detail })
      }
      applyDone.value = Math.min(i + APPLY_CHUNK, ids.length)
      results.value = [...collected]
    }
    const c = { applied: 0, unverified: 0, failed: 0 }
    for (const r of collected) {
      const st = resultState(r)
      if (st === 'applied') c.applied += 1
      else if (st === 'unverified') c.unverified += 1
      else c.failed += 1
    }
    flash(`下发完成：成功 ${c.applied} / 待确认 ${c.unverified} / 失败 ${c.failed}`, c.failed === 0)
    confirmDialog.open = false
    await refreshDeviceStatus()
  } finally {
    confirmDialog.busy = false
    applying.value = false
    applyPhase.value = ''
  }
}

function askRollback() {
  if (!isAdmin.value) return
  confirmDialog.open = true
  confirmDialog.title = `确认回滚 ${selectedIds.value.length} 台设备的 NTP 配置？`
  confirmDialog.message = '将执行回滚命令，移除本平台的 NTP 服务器配置。设备将不再与本平台对时。'
  confirmDialog.confirmText = '确认回滚'
  confirmDialog.danger = true
  confirmDialog.commands = ''
  confirmDialog.note = '未指定地址时，回滚会取各设备最近一条下发记录里的地址。'
  // 同样在弹窗前把参数定下来
  confirmDialog.onConfirm = () =>
    doRollback({
      device_ids: [...selectedIds.value],
      address: addressInput.value || undefined,
      clock_mode: clockMode.value,
      save: saveConfig.value,
    })
}

async function doRollback(payload) {
  confirmDialog.busy = true
  applying.value = true
  results.value = []
  const ids = [...payload.device_ids]
  applyTotal.value = ids.length
  applyDone.value = 0
  const collected = []
  try {
    for (let i = 0; i < ids.length; i += APPLY_CHUNK) {
      const chunk = ids.slice(i, i + APPLY_CHUNK)
      applyPhase.value = `回滚中（第 ${Math.floor(i / APPLY_CHUNK) + 1} / ${Math.ceil(ids.length / APPLY_CHUNK)} 批）`
      try {
        const res = await rollbackNtp({ ...payload, device_ids: chunk })
        collected.push(...((res && res.results) || []))
      } catch (e) {
        const detail = e?.response?.data?.detail || e.message || '请求失败'
        for (const id of chunk) collected.push({ device_id: id, ok: false, state: 'failed', error: detail })
      }
      applyDone.value = Math.min(i + APPLY_CHUNK, ids.length)
      results.value = [...collected]
    }
    flash('回滚完成', true)
    confirmDialog.open = false
    await refreshDeviceStatus()
  } finally {
    confirmDialog.busy = false
    applying.value = false
    applyPhase.value = ''
  }
}

// 偏差格式化：1 秒以内用 ms，否则用 s（图表轴与 tooltip 共用，避免两处口径不一）
function fmtOffset(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '-'
  const a = Math.abs(n)
  if (a === 0) return '0'
  // 分界与 offsetParts / milliScale 保持一致：1 秒以内统一 ms
  if (a < 1) return `${(n * 1000).toFixed(1)} ms`
  return `${n.toFixed(2)} s`
}

// ---------- 图表 ----------
function renderChart(points) {
  if (!chartEl.value) return
  if (!chart) chart = echarts.init(chartEl.value)
  const t = chartTheme()
  const thr = Number(historyStats.value.threshold_seconds || 0)
  // 失败样本不进折线（值为 null 断线），单独用散点标在 0 位上
  const bad = points.filter((p) => p.success === false)
  // 量级判据：全部样本都在 1 秒以内时，轴名与刻度统一按 ms 展示
  const vals = points.filter((p) => p.success !== false).map((p) => Math.abs(Number(p.offset_seconds) || 0))
  const milliScale = vals.length > 0 && Math.max(...vals) < 1
  chartMilliScale.value = milliScale
  chart.setOption(
    {
      backgroundColor: 'transparent',
      grid: { left: 56, right: 20, top: 28, bottom: 40 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: t.tooltipBg,
        borderColor: t.tooltipBorder,
        textStyle: { color: t.tooltipText, fontSize: 12 },
        valueFormatter: (v) => (v === null || v === undefined ? '-' : fmtOffset(v)),
      },
      xAxis: {
        type: 'category',
        data: points.map((p) => fmtTime(p.at)),
        axisLine: { lineStyle: { color: t.axisLine } },
        axisLabel: { color: t.sub, fontSize: 11, hideOverlap: true },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        name: milliScale ? '偏差（毫秒）' : '偏差（秒）',
        nameTextStyle: { color: t.sub, fontSize: 11 },
        axisLine: { show: false },
        // 偏差常在毫秒级（±0.01s）：固定 1 位小数会把所有刻度都显示成 0.0，
        // 轴完全读不出来。按量级自适应精度，并在过小时改用毫秒为单位。
        axisLabel: {
          color: t.sub,
          fontSize: 11,
          formatter: (v) => {
            const n = Number(v)
            if (!Number.isFinite(n)) return ''
            if (n === 0) return '0'
            return milliScale ? (n * 1000).toFixed(1) : n.toFixed(3)
          },
        },
        splitLine: { lineStyle: { color: t.split } },
      },
      series: [
        {
          name: '偏差',
          type: 'line',
          data: points.map((p) => (p.success === false ? null : p.offset_seconds)),
          smooth: true,
          showSymbol: false,
          lineStyle: { color: t.brand, width: 1.8 },
          areaStyle: { color: t.brand, opacity: 0.1 },
          // 阈值线：超阈值区间用红色高亮（后端告警口径）。
          // 毫秒量级时刻度跨度只有 ±10ms，秒级阈值线会落在视野之外 —— 不画，
          // 统计行会改成文字说明，避免"图例说有阈值线、图上却找不到"。
          markLine: thr && !milliScale
            ? {
                silent: true,
                symbol: 'none',
                label: { color: t.sub, fontSize: 10, formatter: '阈值' },
                lineStyle: { color: '#f59e0b', type: 'dashed', width: 1 },
                data: [{ yAxis: thr }, { yAxis: -thr }],
              }
            : undefined,
        },
        {
          name: '取时失败',
          type: 'scatter',
          data: bad.map((p) => [fmtTime(p.at), 0]),
          symbolSize: 6,
          itemStyle: { color: '#ef4444' },
        },
      ],
    },
    true,
  )
}

function resizeChart() {
  if (chart) chart.resize()
}

function disposeChart() {
  if (chart) {
    chart.dispose()
    chart = null
  }
}

// ---------- 生命周期 ----------
onMounted(async () => {
  await loadMe()
  await loadAll()
  await loadCandidates()
  await loadTemplates()
  window.addEventListener('resize', resizeChart)
  // 实时时钟：每秒走一格，每 15s 随 /ntp/status 轮询重新锚定
  clockTimer = setInterval(tickClock, 1000)
  offTheme = onThemeChange(() => {
    const pts = historyStats.value && historyStats.value.points ? historyStats.value.points : null
    if (pts) renderChart(pts)
    else loadHistory()
  })
  // 状态 15s 轮询；偏差历史 60s（曲线慢变量，无需频繁拉）
  timer = setInterval(async () => {
    if (document.hidden) return
    await loadStatus()
    await loadOffset()
  }, 15000)
  historyTimer = setInterval(() => {
    if (document.hidden) return
    loadHistory()
  }, 60000)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeChart)
  if (offTheme) offTheme()
  if (timer) clearInterval(timer)
  if (historyTimer) clearInterval(historyTimer)
  if (clockTimer) clearInterval(clockTimer)
  clearTimeout(flashTimer)
  disposeChart()
})
</script>
