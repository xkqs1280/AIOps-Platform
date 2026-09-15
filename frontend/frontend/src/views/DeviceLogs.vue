<template>
  <div class="min-h-screen bg-app text-ink-strong p-6 animate-in">
    <!-- ================= Header ================= -->
    <div class="flex items-start justify-between gap-4 mb-5">
      <div>
        <h2 class="text-xl font-bold tracking-wide">设备日志中心</h2>
        <p class="text-xs text-ink-faint mt-1">
          设备 syslog 统一接收与留存 · 平台留存 {{ retentionDays }} 天
        </p>
      </div>
      <div class="flex items-center gap-2.5 shrink-0">
        <!-- 接收器状态：设备在发、平台静默收不到是最难排查的故障，所以常驻显示 -->
        <div
          class="flex items-center gap-2 px-3 py-2 rounded-lg border text-xs"
          :class="receiverBoxClass"
          :title="receiverTitle"
        >
          <span class="status-dot" :class="receiverDotClass"></span>
          <span class="font-medium">{{ receiverText }}</span>
          <span v-if="receiver" class="font-mono text-ink-faint">
            {{ receiver.host }}:{{ receiver.bound_port || receiver.port }}/udp
          </span>
        </div>
        <button class="btn btn-ghost btn-sm" :disabled="loading" @click="refreshAll">
          <ArrowPathIcon class="w-4 h-4" :class="loading ? 'animate-spin' : ''" />
          刷新
        </button>
        <button class="btn btn-outline btn-sm" :disabled="total === 0" @click="doExport">
          <ArrowDownTrayIcon class="w-4 h-4" />
          导出 CSV
        </button>
      </div>
    </div>

    <!-- 接收器绑定失败：排查建议由后端按平台下发（Windows 上还会查出占用进程），
         不再在前端硬编码 Linux 的 CAP_NET_BIND_SERVICE ——那套提示在 Windows 上是错的 -->
    <div v-if="receiver && receiver.bind_error" class="mb-5 card border-danger/40 bg-danger/5 p-4">
      <div class="flex items-start gap-3">
        <ExclamationTriangleIcon class="w-5 h-5 text-danger shrink-0 mt-0.5" />
        <div class="text-sm">
          <p class="font-semibold text-danger">syslog 接收器绑定 {{ receiver.host }}:{{ receiver.port }} 失败</p>
          <p class="text-ink-muted mt-1 font-mono text-xs break-all">{{ receiver.bind_error }}</p>
          <p v-if="receiver.bind_occupier?.length" class="text-danger mt-1 text-xs">
            当前占用：{{ receiver.bind_occupier.join('、') }}
          </p>
          <ul class="text-ink-muted mt-2 space-y-0.5 list-disc list-inside text-xs">
            <li v-for="(hint, i) in bindHints" :key="i">{{ hint }}</li>
          </ul>
        </div>
      </div>
    </div>

    <!-- ================= 统计卡片 ================= -->
    <div class="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4 mb-5">
      <div class="card card-lift p-4">
        <div class="flex items-center justify-between">
          <span class="card-title">{{ statsRangeLabel }}</span>
          <DocumentTextIcon class="w-4 h-4 text-cyan-400" />
        </div>
        <div class="text-2xl font-bold mt-2 tabular-nums">{{ fmtNum(stats?.total) }}</div>
        <div class="text-[11px] text-ink-faint mt-1">
          累计接收 {{ fmtNum(receiver?.received) }} 条
        </div>
      </div>

      <div class="card card-lift p-4">
        <div class="flex items-center justify-between">
          <span class="card-title">错误及以上</span>
          <ExclamationTriangleIcon class="w-4 h-4 text-danger" />
        </div>
        <div class="text-2xl font-bold mt-2 tabular-nums" :class="errorCount > 0 ? 'text-danger' : ''">
          {{ fmtNum(errorCount) }}
        </div>
        <div class="text-[11px] text-ink-faint mt-1">级别 0~3（emergency/critical/error）</div>
      </div>

      <button class="card card-lift p-4 text-left" @click="focusMissingDevice">
        <div class="flex items-center justify-between">
          <span class="card-title">未纳管设备日志</span>
          <QuestionMarkCircleIcon class="w-4 h-4 text-warning" />
        </div>
        <div class="text-2xl font-bold mt-2 tabular-nums" :class="missingTotal > 0 ? 'text-warning' : ''">
          {{ fmtNum(missingTotal) }}
        </div>
        <div class="text-[11px] text-ink-faint mt-1">来源 IP 不在设备表 → 排查漏管设备</div>
      </button>

      <!-- 日志主机：已把日志发到本平台的设备数。点开看是哪几台 —— 这张表才是
           「配置到底生效没有」的真凭据（命令下发成功 ≠ 日志真的发过来了）。 -->
      <button class="card card-lift p-4 text-left" @click="openLoghostDetail">
        <div class="flex items-center justify-between">
          <span class="card-title">日志主机</span>
          <ServerStackIcon class="w-4 h-4 text-violet-400" />
        </div>
        <div class="text-2xl font-bold mt-2 tabular-nums" :class="lhManaged.length ? 'text-success' : ''">
          {{ fmtNum(lhManaged.length) }}
        </div>
        <div class="text-[11px] text-ink-faint mt-1 truncate">
          <template v-if="lhManaged.length">
            已收到日志 {{ lhHostSummary.receiving }}
            <template v-if="lhHostSummary.applied_silent"> · 已下发暂无日志 {{ lhHostSummary.applied_silent }}</template>
          </template>
          <template v-else>尚无设备把日志发到本平台</template>
        </div>
      </button>

      <div class="card card-lift p-4">
        <div class="flex items-center justify-between">
          <span class="card-title">入库 / 排队</span>
          <CircleStackIcon class="w-4 h-4 text-success" />
        </div>
        <div class="text-2xl font-bold mt-2 tabular-nums">
          {{ fmtNum(receiver?.stored) }}
          <span class="text-base font-normal text-ink-faint">/ {{ fmtNum(receiver?.queue_size) }}</span>
        </div>
        <div class="text-[11px] mt-1" :class="droppedTotal > 0 ? 'text-warning' : 'text-ink-faint'">
          <template v-if="droppedTotal > 0">
            丢弃 溢出 {{ fmtNum(receiver?.dropped_overflow) }} · 限流 {{ fmtNum(receiver?.dropped_rate) }}
          </template>
          <template v-else>无丢弃 · 安全类分流 {{ fmtNum(receiver?.security_routed) }} 条</template>
        </div>
      </div>
    </div>

    <!-- ================= Tabs ================= -->
    <div class="flex gap-2.5 mb-5">
      <button
        v-for="t in tabs"
        :key="t.key"
        class="btn btn-sm"
        :class="activeTab === t.key ? 'btn-primary' : 'btn-ghost'"
        @click="switchTab(t.key)"
      >
        {{ t.label }}
      </button>
    </div>

    <!-- ================================================================
         Tab 1：日志查询
         ================================================================ -->
    <div v-if="activeTab === 'logs'">
      <!-- 筛选栏 -->
      <div class="card p-4 mb-4">
        <div class="flex flex-wrap items-end gap-3">
          <div class="w-32">
            <label class="form-label">时间范围</label>
            <select v-model="f.range" class="select" @change="onRangeChange">
              <option value="1h">近 1 小时</option>
              <option value="24h">近 24 小时</option>
              <option value="7d">近 7 天</option>
              <option value="30d">近 30 天</option>
              <option value="custom">自定义</option>
            </select>
          </div>

          <template v-if="f.range === 'custom'">
            <div class="w-52">
              <label class="form-label">起始</label>
              <input v-model="f.startLocal" type="datetime-local" class="input" @change="reload" />
            </div>
            <div class="w-52">
              <label class="form-label">结束</label>
              <input v-model="f.endLocal" type="datetime-local" class="input" @change="reload" />
            </div>
          </template>

          <div class="w-48">
            <label class="form-label">设备</label>
            <select v-model="f.device_id" class="select" @change="reload">
              <option value="">全部设备</option>
              <option v-for="d in filtersMeta.devices" :key="d.id" :value="d.id">
                {{ d.name }} ({{ d.ip }})
              </option>
            </select>
          </div>

          <div class="w-36">
            <label class="form-label">模块</label>
            <select v-model="f.module" class="select" @change="reload">
              <option value="">全部模块</option>
              <option v-for="m in filtersMeta.modules" :key="m.module" :value="m.module">
                {{ m.module }} ({{ m.count }})
              </option>
            </select>
          </div>

          <div class="w-36">
            <label class="form-label">分类</label>
            <select v-model="f.category" class="select" @change="reload">
              <option value="">全部分类</option>
              <option v-for="c in filtersMeta.categories" :key="c.category" :value="c.category">
                {{ c.label }}
              </option>
            </select>
          </div>

          <div class="w-40">
            <label class="form-label">级别（不高于）</label>
            <select v-model="f.max_severity" class="select" @change="reload">
              <option value="">全部级别</option>
              <option v-for="s in filtersMeta.severities" :key="s.severity" :value="s.severity">
                {{ s.label }}（{{ s.name }}）
              </option>
            </select>
          </div>

          <div class="flex-1 min-w-[220px]">
            <label class="form-label">关键字</label>
            <div class="relative">
              <MagnifyingGlassIcon class="w-4 h-4 text-ink-faint absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                v-model="f.keyword"
                class="input pl-9"
                placeholder="正文 / 模块 / 接口 / 用户 / 设备名 / IP"
                @input="onKeywordInput"
                @keyup.enter="reload"
              />
            </div>
          </div>

          <label class="flex items-center gap-2 pb-2 text-xs text-ink-muted cursor-pointer select-none">
            <input v-model="f.missing_device" type="checkbox" class="checkbox" @change="reload" />
            只看未纳管设备
          </label>

          <button class="btn btn-ghost btn-sm mb-[1px]" @click="resetFilters">重置</button>
        </div>
      </div>

      <!-- 图表 -->
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-4">
        <div class="card p-4 lg:col-span-2">
          <div class="flex items-center justify-between mb-2">
            <span class="card-title">日志量趋势（按小时）</span>
            <span class="text-[11px] text-ink-faint">按平台接收时间</span>
          </div>
          <div ref="hourChartRef" class="h-[190px]"></div>
        </div>
        <div class="card p-4">
          <span class="card-title">分类分布</span>
          <div ref="catChartRef" class="h-[190px] mt-2"></div>
        </div>
      </div>

      <!-- 列表 -->
      <div class="card overflow-hidden">
        <div class="overflow-x-auto">
          <table class="table">
            <thead>
              <tr>
                <th class="w-[150px]">接收时间</th>
                <th class="w-[160px]">设备</th>
                <th class="w-[90px]">模块</th>
                <th class="w-[80px]">级别</th>
                <th class="w-[90px]">分类</th>
                <th class="w-[170px]">接口</th>
                <th class="w-[100px]">用户</th>
                <th>内容</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="log in logs"
                :key="log.id"
                class="cursor-pointer"
                @click="detail = log"
              >
                <td class="whitespace-nowrap font-mono text-xs">{{ fmtTime(log.received_at) }}</td>
                <td>
                  <div class="text-ink truncate max-w-[150px]">{{ log.device_name || '未纳管' }}</div>
                  <div v-if="!log.device_name" class="text-[11px] text-warning font-mono">{{ log.src_ip || '-' }}</div>
                </td>
                <td>
                  <span class="badge badge-neutral font-mono">{{ log.module || '-' }}</span>
                </td>
                <td>
                  <span class="badge" :class="severityBadge(log.severity)">
                    {{ log.severity_label || '-' }}
                  </span>
                </td>
                <td class="whitespace-nowrap">{{ log.category_label || '-' }}</td>
                <td class="font-mono text-xs truncate max-w-[170px]" :title="log.interface">
                  {{ log.interface || '-' }}
                </td>
                <td class="truncate max-w-[100px]" :title="log.username">{{ log.username || '-' }}</td>
                <td>
                  <div class="text-ink truncate max-w-[520px]" :title="log.content">{{ log.content }}</div>
                </td>
              </tr>
              <tr v-if="!loading && logs.length === 0">
                <td colspan="8" class="py-14 text-center text-ink-faint">
                  <template v-if="!receiver?.running">
                    接收器未运行，且当前条件无日志。请确认设备已配置 loghost 指向本平台。
                  </template>
                  <template v-else-if="receiver?.stored === 0">
                    接收正常但尚无日志入库 —— 设备侧可能还没配置日志主机（见「日志主机下发」页）。
                  </template>
                  <template v-else>当前筛选条件下没有日志</template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 分页 -->
        <div v-if="total > 0" class="flex items-center justify-between px-4 py-3 border-t border-line text-sm text-ink-muted">
          <span>共 {{ fmtNum(total) }} 条</span>
          <div class="flex items-center gap-3">
            <select v-model.number="pageSize" class="select w-28" @change="page = 1; loadLogs()">
              <option :value="20">20 条/页</option>
              <option :value="50">50 条/页</option>
              <option :value="100">100 条/页</option>
              <option :value="200">200 条/页</option>
            </select>
            <button class="btn btn-outline btn-sm" :disabled="page <= 1" @click="page--; loadLogs()">上一页</button>
            <span class="tabular-nums">第 {{ page }} / {{ totalPages }} 页</span>
            <button class="btn btn-outline btn-sm" :disabled="page >= totalPages" @click="page++; loadLogs()">下一页</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ================================================================
         Tab 2：日志主机下发（真实改动设备配置）
         ================================================================ -->
    <div v-else>
      <div class="card border-warning/40 bg-warning/5 p-3.5 mb-4 flex items-start gap-2.5">
        <ExclamationTriangleIcon class="w-4 h-4 text-warning shrink-0 mt-0.5" />
        <p class="text-xs text-ink-muted leading-relaxed">
          该操作会<strong class="text-warning">真实修改设备配置</strong>：平台先读取 info-center 配置快照，
          再逐条下发 loghost 命令并回验，全程留痕、可一键回滚。
          不勾选「保存到启动配置」时仅做运行时下发（重启即失效），适合先在少量设备灰度验证。
        </p>
      </div>

      <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <!-- 左：参数与命令预览 -->
        <div class="space-y-4">
          <div class="card p-4">
            <h3 class="card-title mb-3">下发参数</h3>

            <div v-if="lhCandidates.length" class="mb-3">
              <label class="form-label">平台侧地址候选（多网卡时请确认设备能访问哪一个）</label>
              <div class="flex flex-wrap gap-2">
                <button
                  v-for="c in lhCandidates"
                  :key="c"
                  class="btn btn-sm"
                  :class="lhForm.address === c ? 'btn-primary' : 'btn-outline'"
                  @click="lhForm.address = c; clearPreview()"
                >
                  {{ c }}
                </button>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="form-label">日志主机地址 *</label>
                <input v-model.trim="lhForm.address" class="input font-mono" placeholder="192.168.1.10" @input="clearPreview" />
                <p v-if="lhAdvertiseNotSet" class="text-[11px] text-warning mt-1">
                  未在 .env 配置 SYSLOG_ADVERTISE_ADDRESS，请手动指定
                </p>
              </div>
              <div>
                <label class="form-label">端口（留空 = 514）</label>
                <input v-model.trim="lhForm.port" class="input font-mono" placeholder="514" @input="clearPreview" />
              </div>
              <div>
                <label class="form-label">日志级别</label>
                <select v-model="lhForm.level" class="select" @change="clearPreview">
                  <option v-for="lv in levelOptions" :key="lv" :value="lv">{{ lv }}</option>
                </select>
              </div>
              <label class="flex items-end gap-2 pb-2.5 text-xs text-ink-muted cursor-pointer select-none">
                <input v-model="lhForm.save" type="checkbox" class="checkbox" @change="clearPreview" />
                保存到启动配置（H3C 用 save force、华为用 save）
              </label>
            </div>
          </div>

          <div class="card p-4">
            <div class="flex items-center justify-between mb-3">
              <h3 class="card-title">命令预览</h3>
              <div class="flex gap-2">
                <button class="btn btn-ghost btn-sm" :disabled="!canSubmit || lhLoading" @click="doPreview">
                  预览命令
                </button>
                <button class="btn btn-outline btn-sm" :disabled="!lhPreview" @click="copyCommands">
                  <ClipboardDocumentIcon class="w-3.5 h-3.5" />
                  复制（手工下发）
                </button>
              </div>
            </div>
            <div v-if="lhPreview" class="space-y-2">
              <div class="text-[11px] text-ink-faint">
                目标 {{ lhPreview.devices.length }} 台设备 · {{ lhPreview.address }}:{{ lhPreview.port }}
              </div>
              <div v-for="v in lhVariants" :key="v.vendor || 'default'" class="space-y-2">
                <div v-if="lhVariants.length > 1" class="text-[11px] text-ink font-medium">
                  适用：{{ v.vendor }}（{{ v.devices }}）
                </div>
                <pre class="font-mono text-xs bg-surface-2 border border-line rounded-lg p-3 overflow-x-auto text-ink">{{ (v.commands || []).join('\n') }}</pre>
                <div class="text-[11px] text-ink-faint">回滚命令：</div>
                <pre class="font-mono text-xs bg-surface-2 border border-line rounded-lg p-3 overflow-x-auto text-ink-muted">{{ (v.rollback_commands || []).join('\n') }}</pre>
              </div>
            </div>
            <p v-else class="text-xs text-ink-faint py-6 text-center">
              选择设备后点「预览命令」查看将要下发的内容（预览不会连接设备）
            </p>
          </div>

          <div class="card p-4">
            <h3 class="card-title mb-3">执行</h3>
            <div class="flex items-center gap-3">
              <button class="btn btn-primary" :disabled="!canSubmit || lhBusy" @click="askApply">
                <CloudArrowUpIcon class="w-4 h-4" />
                下发到 {{ lhSelected.length }} 台设备
              </button>
              <button class="btn btn-danger" :disabled="lhSelected.length === 0 || lhBusy" @click="askRollback">
                <ArrowUturnLeftIcon class="w-4 h-4" />
                回滚选中设备
              </button>
            </div>

            <!-- 分块进度：单块最多 30 台，避免一个请求挂十几分钟 -->
            <div v-if="lhBusy" class="mt-4">
              <div class="flex items-center justify-between text-xs text-ink-muted mb-1.5">
                <span>{{ lhProgress.label }}（每批最多 30 台，逐批提交）</span>
                <span class="tabular-nums">{{ lhProgress.done }} / {{ lhProgress.total }}</span>
              </div>
              <div class="h-1.5 bg-surface-2 rounded-full overflow-hidden">
                <div class="h-full grad-brand transition-all duration-300"
                     :style="{ width: (lhProgress.total ? (lhProgress.done / lhProgress.total * 100) : 0) + '%' }"></div>
              </div>
            </div>

            <div v-if="lhResults.length" class="mt-4 max-h-64 overflow-y-auto space-y-1.5">
              <div class="flex flex-wrap items-center gap-3 px-1 pb-1 text-[11px]">
                <span class="text-success">已确认 {{ lhSummary.applied }}</span>
                <span v-if="lhSummary.unverified" class="text-warning">待确认 {{ lhSummary.unverified }}</span>
                <span class="text-danger">失败 {{ lhSummary.failed }}</span>
              </div>
              <div
                v-for="(r, i) in lhResults"
                :key="i"
                class="flex items-start gap-2.5 px-3 py-2 rounded-lg border text-xs"
                :class="r.ok ? 'border-success/30 bg-success/5'
                  : (isUnverified(r) ? 'border-warning/30 bg-warning/5' : 'border-danger/30 bg-danger/5')"
              >
                <CheckCircleIcon v-if="r.ok" class="w-4 h-4 text-success shrink-0 mt-0.5" />
                <ExclamationTriangleIcon v-else-if="isUnverified(r)" class="w-4 h-4 text-warning shrink-0 mt-0.5" />
                <XCircleIcon v-else class="w-4 h-4 text-danger shrink-0 mt-0.5" />
                <div class="min-w-0 flex-1">
                  <div class="flex items-center justify-between gap-3">
                    <span class="text-ink truncate">{{ r.device_name }}</span>
                    <span class="font-mono text-ink-faint shrink-0">{{ r.ip }}</span>
                  </div>
                  <div v-if="r.error" class="mt-0.5 break-all"
                       :class="isUnverified(r) ? 'text-warning' : 'text-danger'">{{ r.error }}</div>
                  <div v-else-if="r.warnings?.length" class="text-warning mt-0.5 break-all">{{ r.warnings.join('; ') }}</div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 右：设备选择 + 当前状态 -->
        <div class="space-y-4">
          <div class="card p-4">
            <div class="flex items-center justify-between mb-3">
              <h3 class="card-title">选择设备</h3>
              <div class="flex items-center gap-2">
                <span class="text-[11px] text-ink-faint">已选 {{ lhSelected.length }}</span>
                <button class="btn btn-ghost btn-sm" @click="selectAllFiltered">
                  {{ allFilteredSelected ? '取消全选' : '全选' }}
                </button>
              </div>
            </div>
            <div class="relative mb-2">
              <MagnifyingGlassIcon class="w-4 h-4 text-ink-faint absolute left-3 top-1/2 -translate-y-1/2" />
              <input v-model="lhDeviceSearch" class="input pl-9" placeholder="按名称 / IP 过滤设备" />
            </div>
            <div class="max-h-[420px] overflow-y-auto border border-line rounded-lg divide-y divide-line">
              <label
                v-for="d in lhFilteredDevices"
                :key="d.id"
                class="flex items-center gap-3 px-3 py-2 hover:bg-hover/50 cursor-pointer text-sm"
              >
                <input type="checkbox" class="checkbox" :value="d.id" v-model="lhSelected" @change="clearPreview" />
                <span class="text-ink flex-1 truncate">{{ d.name }}</span>
                <span class="font-mono text-xs text-ink-faint">{{ d.ip }}</span>
                <span class="badge shrink-0" :class="statusBadgeClass(lhDisplayStatus(d.id))"
                      :title="lhStatusHint(d.id)">
                  {{ statusLabel(lhDisplayStatus(d.id)) }}
                </span>
              </label>
              <div v-if="lhFilteredDevices.length === 0" class="px-3 py-10 text-center text-ink-faint text-sm">
                没有匹配的设备
              </div>
            </div>
          </div>

          <div class="card p-4">
            <h3 class="card-title mb-3">日志主机配置状态（记录 + 实际收到日志）</h3>
            <div class="max-h-72 overflow-y-auto">
              <table class="table">
                <thead>
                  <tr>
                    <th>设备</th>
                    <th>日志主机</th>
                    <th>状态</th>
                    <th>最近收到日志</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(s, i) in lhStatus" :key="i">
                    <td>
                      <div class="text-ink truncate max-w-[140px]">{{ s.device_name || '已删除设备' }}</div>
                      <div class="text-[11px] font-mono text-ink-faint">{{ s.device_ip || '-' }}</div>
                    </td>
                    <td class="font-mono text-xs">
                      {{ s.address }}<span v-if="s.port && s.port !== 514">:{{ s.port }}</span>
                    </td>
                    <td>
                      <span class="badge" :class="statusBadgeClass(lhDisplayStatus(s.device_id, s))"
                            :title="lhStatusHint(s.device_id, s)">
                        {{ statusLabel(lhDisplayStatus(s.device_id, s)) }}
                      </span>
                      <div v-if="s.message" class="text-[11px] text-ink-faint mt-0.5 truncate max-w-[180px]" :title="s.message">
                        {{ s.message }}
                      </div>
                    </td>
                    <td class="text-xs whitespace-nowrap">
                      <template v-if="s.last_log_at">
                        <div>{{ fmtTime(s.last_log_at) }}</div>
                        <div class="text-[11px] text-ink-faint">
                          {{ fmtNum(s.log_count) }} 条 / {{ lhStatusInfo.receiving_days }} 天
                        </div>
                      </template>
                      <span v-else class="text-ink-faint">—</span>
                    </td>
                  </tr>
                  <tr v-if="lhStatus.length === 0">
                    <td colspan="4" class="py-8 text-center text-ink-faint">暂无下发记录，也未收到日志</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ================= 日志主机详情（顶部卡片点开） ================= -->
    <div v-if="lhDetailOpen" class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
         @click.self="lhDetailOpen = false">
      <div class="bg-surface border border-line rounded-xl w-full max-w-3xl max-h-[82vh] flex flex-col">
        <div class="flex items-start justify-between gap-4 px-5 py-4 border-b border-line">
          <div>
            <h3 class="text-base font-bold">日志主机 · 已接入设备</h3>
            <p class="text-[11px] text-ink-faint mt-0.5">
              平台地址
              <span class="font-mono">{{ lhStatusInfo.platform_address || '未配置' }}:{{ lhStatusInfo.udp_port || 514 }}</span>
              · 近 {{ lhStatusInfo.receiving_days }} 天统计
            </p>
          </div>
          <button class="btn btn-ghost btn-sm" @click="lhDetailOpen = false">关闭</button>
        </div>

        <div class="px-5 py-3 border-b border-line flex flex-wrap items-center gap-4 text-xs">
          <span class="text-success">已收到日志 {{ lhHostSummary.receiving }}</span>
          <span v-if="lhHostSummary.applied_silent" class="text-cyan-400">
            已下发暂无日志 {{ lhHostSummary.applied_silent }}
          </span>
          <span v-if="lhHostSummary.failed" class="text-danger">失败 {{ lhHostSummary.failed }}</span>
          <span class="text-ink-faint">合计 {{ lhManaged.length }} 台</span>
        </div>

        <div class="overflow-y-auto">
          <table class="table">
            <thead>
              <tr>
                <th>设备名称</th>
                <th>设备 IP</th>
                <th>状态</th>
                <th>最近收到日志</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(s, i) in lhManaged" :key="i">
                <td class="text-ink">{{ s.device_name || '—' }}</td>
                <td class="font-mono text-xs text-ink-muted">{{ s.device_ip || '—' }}</td>
                <td>
                  <span class="badge" :class="statusBadgeClass(lhDisplayStatus(s.device_id, s))">
                    {{ statusLabel(lhDisplayStatus(s.device_id, s)) }}
                  </span>
                  <span v-if="s.source === 'traffic'" class="text-[11px] text-ink-faint ml-1.5">设备侧已配（平台无记录）</span>
                </td>
                <td class="text-xs whitespace-nowrap">
                  <template v-if="s.last_log_at">
                    {{ fmtTime(s.last_log_at) }}
                    <span class="text-ink-faint">（{{ fmtNum(s.log_count) }} 条）</span>
                  </template>
                  <span v-else class="text-ink-faint">—</span>
                </td>
              </tr>
              <tr v-if="lhManaged.length === 0">
                <td colspan="4" class="py-10 text-center text-ink-faint">
                  还没有设备把日志发到本平台。到「日志主机下发」页选择设备并下发后即会出现在这里。
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="px-5 py-3 border-t border-line text-[11px] text-ink-faint">
          「已下发」= 平台确认配置生效，或确实已收到该设备的日志；「仅收到日志」= 设备侧已经配好，但平台没有下发记录（多为手工配置）。
          收到日志是配置真的生效的唯一硬证据 —— 命令下发成功不等于日志会来。
        </div>
      </div>
    </div>

    <!-- ================= 日志详情抽屉 ================= -->
    <div v-if="detail" class="fixed inset-0 bg-black/50 z-50 flex justify-end" @click.self="detail = null">
      <div class="w-full max-w-2xl bg-surface border-l border-line h-full overflow-y-auto animate-fade">
        <div class="flex items-center justify-between px-6 py-4 border-b border-line sticky top-0 bg-surface z-10">
          <div>
            <h3 class="text-base font-bold">日志详情</h3>
            <p class="text-[11px] text-ink-faint font-mono mt-0.5">#{{ detail.id }} · 来源 {{ detail.log_source }}</p>
          </div>
          <button class="btn btn-ghost btn-sm" @click="detail = null">关闭</button>
        </div>

        <div class="p-6 space-y-5">
          <div class="grid grid-cols-2 gap-4 text-sm">
            <div v-for="row in detailRows" :key="row.label">
              <div class="text-[11px] text-ink-faint mb-0.5">{{ row.label }}</div>
              <div class="text-ink break-all" :class="row.mono ? 'font-mono text-xs' : ''">{{ row.value || '-' }}</div>
            </div>
          </div>

          <div>
            <div class="text-[11px] text-ink-faint mb-1">消息正文</div>
            <div class="text-sm text-ink bg-surface-2 border border-line rounded-lg p-3 whitespace-pre-wrap break-all">
              {{ detail.content || '-' }}
            </div>
          </div>

          <div>
            <div class="flex items-center justify-between mb-1">
              <span class="text-[11px] text-ink-faint">原始报文（审计依据，不做任何加工）</span>
              <button class="btn btn-ghost btn-sm" @click="copyText(detail.raw_log)">复制</button>
            </div>
            <pre class="font-mono text-xs bg-surface-2 border border-line rounded-lg p-3 whitespace-pre-wrap break-all text-ink">{{ detail.raw_log || '-' }}</pre>
          </div>

          <div v-if="detail.device_time_raw" class="text-[11px] text-ink-faint leading-relaxed">
            设备侧时间原样为 <code class="font-mono">{{ detail.device_time_raw }}</code>。
            设备时钟可能为 UTC（实测 H3C 出厂 <code class="font-mono">display clock</code> 返回 UTC），
            如需按设备本地时间对齐，请配置 <code class="font-mono">SYSLOG_DEVICE_TZ_OFFSET_HOURS</code>。
          </div>
        </div>
      </div>
    </div>

    <!-- ================= 高危操作二次确认 ================= -->
    <div v-if="confirmDialog" class="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4" @click.self="confirmDialog = null">
      <div class="bg-surface rounded-xl border border-line w-full max-w-lg">
        <div class="px-6 py-4 border-b border-line flex items-center gap-2.5">
          <ExclamationTriangleIcon class="w-5 h-5" :class="confirmDialog.danger ? 'text-danger' : 'text-warning'" />
          <h3 class="text-base font-bold">{{ confirmDialog.title }}</h3>
        </div>
        <div class="px-6 py-4 text-sm text-ink-muted space-y-3">
          <p v-for="(line, i) in confirmDialog.lines" :key="i" class="leading-relaxed">{{ line }}</p>
          <div class="bg-surface-2 border border-line rounded-lg p-3">
            <div class="text-[11px] text-ink-faint mb-1.5">将对 {{ pendingIds.length }} 台设备执行：</div>
            <pre class="font-mono text-xs text-ink overflow-x-auto whitespace-pre-wrap">{{ pendingCommandsText }}</pre>
          </div>
          <p v-if="confirmDialog.danger" class="text-danger font-medium">
            此操作不可撤销（回滚需另行执行），请确认目标设备无误。
          </p>
        </div>
        <div class="px-6 py-4 border-t border-line flex justify-end gap-3">
          <button class="btn btn-ghost" @click="confirmDialog = null">取消</button>
          <button
            class="btn"
            :class="confirmDialog.danger ? 'btn-danger' : 'btn-primary'"
            :disabled="lhBusy"
            @click="confirmDialog.action(); confirmDialog = null"
          >
            确认执行
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import * as echarts from 'echarts'
import {
  MagnifyingGlassIcon,
  ArrowPathIcon,
  ArrowDownTrayIcon,
  ArrowUturnLeftIcon,
  ExclamationTriangleIcon,
  QuestionMarkCircleIcon,
  CircleStackIcon,
  DocumentTextIcon,
  CheckCircleIcon,
  XCircleIcon,
  ClipboardDocumentIcon,
  CloudArrowUpIcon,
  ServerStackIcon,
} from '@heroicons/vue/24/outline'
import { chartTheme, onThemeChange } from '../utils/chartTheme'
import {
  getDeviceLogs,
  getDeviceLogStats,
  getDeviceLogFilters,
  getDeviceLogReceiver,
  exportDeviceLogs,
  getDevices,
  getLoghostCandidates,
  previewLoghost,
  applyLoghost,
  rollbackLoghost,
  getLoghostStatus,
} from '../api/index.js'

// 单批下发的设备数：后端逐台 SSH（并发 4）+ 前后各一次配置快照，约 15s/台，
// 10 台 ≈ 40s/批。批越小，单个请求越短，越不容易在设备慢/连接抖动时被中途
// 取消或断开（2026-09-15 生产 70 台批量下发/回滚报 500 即长请求叠加单台中断）；
// 同时进度反馈更细。总时长基本不变，因为并发上限没变。
const APPLY_CHUNK = 10

const tabs = [
  { key: 'logs', label: '日志查询' },
  { key: 'loghost', label: '日志主机下发' },
]
const activeTab = ref('logs')

// 筛选条件（放在最前：多处 computed 会引用它）
const f = ref({
  range: '24h',
  startLocal: '',
  endLocal: '',
  device_id: '',
  module: '',
  category: '',
  max_severity: '',
  keyword: '',
  missing_device: false,
})

// ---------------------------------------------------------------------------
// 接收器状态
// ---------------------------------------------------------------------------
const receiver = ref(null)

// 留存天数取后端**实际生效**的配置（.env 的 DEVICE_LOGS_DAYS），不硬编码：
// 客户调大后页面必须跟着变，否则又变成"文案承诺与实现不一致"。
const retentionDays = computed(() => receiver.value?.retention_days ?? 180)

// 绑定失败时的排查建议：优先后端按平台下发的 bind_hints；旧后端没有该字段时才回退。
const bindHints = computed(() => {
  const hints = receiver.value?.bind_hints
  if (Array.isArray(hints) && hints.length) return hints
  return [
    '端口可能被占用：可改 .env 的 SYSLOG_UDP_PORT（设备侧需同步指向新端口）',
    '防火墙需放行该 UDP 端口入站',
  ]
})

const receiverText = computed(() => {
  const r = receiver.value
  if (!r) return '状态未知'
  if (!r.enabled) return '已停用'
  if (r.bind_error) return '绑定失败'
  if (r.running) return '接收中'
  if (r.started_at) return '未运行'
  return '未启动'
})

const receiverBoxClass = computed(() => {
  const r = receiver.value
  if (!r) return 'border-line bg-surface-2 text-ink-muted'
  if (!r.enabled) return 'border-line bg-surface-2 text-ink-faint'
  if (r.bind_error) return 'border-danger/40 bg-danger/10 text-danger'
  if (r.running) return 'border-success/40 bg-success/10 text-success'
  return 'border-warning/40 bg-warning/10 text-warning'
})

const receiverDotClass = computed(() => {
  const r = receiver.value
  if (!r || !r.enabled || r.bind_error) return 'status-dot-critical'
  if (r.running) return 'status-dot-online'
  return 'status-dot-warning'
})

const receiverTitle = computed(() => {
  const r = receiver.value
  if (!r) return ''
  return [
    `启用：${r.enabled}`,
    `运行：${r.running}`,
    `绑定：${r.host}:${r.bound_port ?? r.port}`,
    r.bind_error ? `错误：${r.bind_error}` : '',
    r.started_at ? `启动于：${fmtTime(r.started_at)}` : '',
  ].filter(Boolean).join('\n')
})

const droppedTotal = computed(() =>
  (receiver.value?.dropped_overflow || 0) + (receiver.value?.dropped_rate || 0))

// ---------------------------------------------------------------------------
// 统计
// ---------------------------------------------------------------------------
const stats = ref(null)
const missingTotal = ref(0)

// 时间范围下拉的"小时数"真源（rangeToParams 也复用，避免两处各写一份对不上）
const RANGE_HOURS = { '1h': 1, '24h': 24, '7d': 168, '30d': 720 }

const rangeHours = computed(() => RANGE_HOURS[f.value.range] ?? 0)

// 自定义范围下没有"近 N 小时"概念，别显示成"近 0 小时"
const statsRangeLabel = computed(() =>
  f.value.range === 'custom' ? '自定义范围日志量' : `近 ${rangeHours.value} 小时日志量`)

const errorCount = computed(() =>
  (stats.value?.by_severity || [])
    .filter((s) => s.severity !== null && s.severity <= 3)
    .reduce((a, s) => a + s.count, 0))

// ---------------------------------------------------------------------------
// 筛选
// ---------------------------------------------------------------------------
const filtersMeta = ref({ modules: [], categories: [], severities: [], devices: [] })

const logs = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const loading = ref(false)
const detail = ref(null)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

const detailRows = computed(() => {
  const d = detail.value
  if (!d) return []
  return [
    { label: '接收时间（平台）', value: fmtTime(d.received_at), mono: true },
    { label: '设备时间（设备侧）', value: d.device_time_raw || d.device_time, mono: true },
    { label: '设备', value: d.device_name ? `${d.device_name} (${d.device_ip || '-'})` : '未纳管（仅来源 IP）' },
    { label: '来源 IP', value: d.src_ip, mono: true },
    { label: '设备自报主机名', value: d.hostname, mono: true },
    { label: '模块 / 助记符', value: [d.module, d.mnemonic].filter(Boolean).join(' / '), mono: true },
    { label: '级别', value: d.severity !== null && d.severity !== undefined ? `${d.severity_label}（${d.severity_name} / ${d.severity}）` : null },
    { label: '分类', value: d.category_label },
    { label: '接口', value: d.interface, mono: true },
    { label: '操作用户', value: d.username, mono: true },
  ]
})

function onRangeChange() {
  if (f.value.range !== 'custom') {
    f.value.startLocal = ''
    f.value.endLocal = ''
  }
  reload()
}

function rangeToParams() {
  if (f.value.range === 'custom') {
    return {
      start: f.value.startLocal ? new Date(f.value.startLocal).toISOString() : undefined,
      end: f.value.endLocal ? new Date(f.value.endLocal).toISOString() : undefined,
    }
  }
  const hours = RANGE_HOURS[f.value.range]
  if (!hours) return {}
  return { start: new Date(Date.now() - hours * 3600_000).toISOString() }
}

function buildQuery() {
  const q = { ...rangeToParams() }
  if (f.value.device_id !== '') q.device_id = f.value.device_id
  if (f.value.module) q.module = f.value.module
  if (f.value.category) q.category = f.value.category
  if (f.value.max_severity !== '') q.max_severity = f.value.max_severity
  if (f.value.keyword.trim()) q.keyword = f.value.keyword.trim()
  if (f.value.missing_device) q.missing_device = true
  return q
}

let kwTimer = null
function onKeywordInput() {
  clearTimeout(kwTimer)
  kwTimer = setTimeout(reload, 350)
}

function resetFilters() {
  f.value = { range: '24h', startLocal: '', endLocal: '', device_id: '', module: '', category: '', max_severity: '', keyword: '', missing_device: false }
  reload()
}

function focusMissingDevice() {
  activeTab.value = 'logs'
  f.value.missing_device = true
  reload()
}

// ---------------------------------------------------------------------------
// 数据加载
// ---------------------------------------------------------------------------
async function loadLogs() {
  loading.value = true
  try {
    const res = await getDeviceLogs({ ...buildQuery(), page: page.value, page_size: pageSize.value })
    logs.value = res.items || []
    total.value = res.total || 0
  } catch (e) {
    console.error('加载设备日志失败', e)
    logs.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  try {
    // 统计口径与列表保持一致（时间窗 + 设备 + 分类 + 关键字），避免"卡片数字和列表对不上"
    stats.value = await getDeviceLogStats({
      ...rangeToParams(),
      device_id: f.value.device_id || undefined,
      category: f.value.category || undefined,
      keyword: f.value.keyword.trim() || undefined,
    })
    const miss = await getDeviceLogs({ ...rangeToParams(), missing_device: true, page: 1, page_size: 1 })
    missingTotal.value = miss.total || 0
  } catch (e) {
    console.error('加载设备日志统计失败', e)
  }
  renderCharts()
}

async function loadMeta() {
  try {
    filtersMeta.value = await getDeviceLogFilters({ hours: rangeHours.value || 168 })
  } catch (e) {
    console.error('加载筛选项失败', e)
  }
}

async function loadReceiver() {
  try {
    receiver.value = await getDeviceLogReceiver()
  } catch (e) {
    console.error('加载接收器状态失败', e)
  }
}

function reload() {
  page.value = 1
  loadLogs()
  loadStats()
}

async function refreshAll() {
  await Promise.all([loadReceiver(), loadMeta()])
  reload()
}

async function doExport() {
  try {
    const res = await exportDeviceLogs(buildQuery())
    const blob = res instanceof Blob ? res : new Blob([res])
    const url = URL.createObjectURL(blob)
    const d = new Date()
    const pad = (n) => String(n).padStart(2, '0')
    const a = document.createElement('a')
    a.href = url
    a.download = `设备日志_${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}.csv`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    alert('导出失败：' + (e?.message || e))
  }
}

// ---------------------------------------------------------------------------
// 图表
// ---------------------------------------------------------------------------
let hourChart = null
let catChart = null
const hourChartRef = ref(null)
const catChartRef = ref(null)

function renderCharts() {
  const cc = chartTheme()

  if (hourChartRef.value) {
    if (!hourChart) hourChart = echarts.init(hourChartRef.value)
    const rows = stats.value?.by_hour || []
    hourChart.setOption({
      grid: { left: 8, right: 12, top: 16, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        backgroundColor: cc.tooltipBg,
        borderColor: cc.tooltipBorder,
        textStyle: { color: cc.tooltipText, fontSize: 12 },
      },
      xAxis: {
        type: 'category',
        data: rows.map((r) => (r.hour || '').slice(5).replace(' ', '\n')),
        axisLabel: { color: cc.axis, fontSize: 10 },
        axisLine: { lineStyle: { color: cc.axisLine } },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: cc.axis, fontSize: 10 },
        splitLine: { lineStyle: { color: cc.split } },
      },
      series: [{
        type: 'bar',
        data: rows.map((r) => r.count),
        itemStyle: { color: '#06b6d4', borderRadius: [3, 3, 0, 0] },
        barMaxWidth: 26,
      }],
    }, true)
  }

  if (catChartRef.value) {
    if (!catChart) catChart = echarts.init(catChartRef.value)
    const rows = stats.value?.by_category || []
    catChart.setOption({
      tooltip: {
        trigger: 'item',
        backgroundColor: cc.tooltipBg,
        borderColor: cc.tooltipBorder,
        textStyle: { color: cc.tooltipText, fontSize: 12 },
      },
      legend: {
        type: 'scroll', bottom: 0, textStyle: { color: cc.sub, fontSize: 10 },
        itemWidth: 8, itemHeight: 8,
      },
      series: [{
        type: 'pie',
        radius: ['38%', '64%'],
        center: ['50%', '44%'],
        label: { color: cc.sub, fontSize: 10, formatter: '{b} {c}' },
        labelLine: { lineStyle: { color: cc.axisLine } },
        itemStyle: { borderColor: cc.pieBorder, borderWidth: 2 },
        data: rows.map((r) => ({ name: r.label, value: r.count })),
      }],
      color: ['#06b6d4', '#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#ef4444', '#6366f1'],
    }, true)
  }
}

function onResize() {
  hourChart?.resize()
  catChart?.resize()
}

let offTheme = null

// ---------------------------------------------------------------------------
// 展示辅助
// ---------------------------------------------------------------------------
const SEVERITY_BADGE = {
  0: 'badge-danger', 1: 'badge-danger', 2: 'badge-danger', 3: 'badge-danger',
  4: 'badge-warning', 5: 'badge-info', 6: 'badge-neutral', 7: 'badge-neutral',
}
const severityBadge = (s) => SEVERITY_BADGE[s] ?? 'badge-neutral'

function fmtNum(n) {
  if (n === null || n === undefined) return '-'
  return Number(n).toLocaleString('zh-CN')
}

function fmtTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text || '')
  } catch {
    // 非 HTTPS 场景 clipboard API 不可用，退回 execCommand
    const ta = document.createElement('textarea')
    ta.value = text || ''
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
}

// ---------------------------------------------------------------------------
// 日志主机下发
// ---------------------------------------------------------------------------
const levelOptions = [
  'emergencies', 'alerts', 'critical', 'errors',
  'warnings', 'notifications', 'informational', 'debugging',
]

const lhForm = ref({ address: '', port: '', level: 'informational', save: true })
const lhCandidates = ref([])
const lhDevices = ref([])
const lhSelected = ref([])
const lhDeviceSearch = ref('')
const lhPreview = ref(null)
const lhApplying = ref(false)   // 单次下发/回滚进行中
const lhBusy = computed(() => lhApplying.value)
const lhProgress = ref({ done: 0, total: 0, label: '' })
const lhResults = ref([])
const lhStatus = ref([])
// 后端下发的汇总与平台地址（卡片数字、详情弹层表头都用它，不在前端另算一套）
const lhStatusInfo = ref({ summary: {}, platform_address: null, udp_port: null, receiving_days: 7 })
const lhDetailOpen = ref(false)
const lhLoading = ref(false)

const pendingIds = ref([])
const confirmDialog = ref(null)

const lhAdvertiseNotSet = computed(() => !lhForm.value.address)

const lhFilteredDevices = computed(() => {
  const kw = lhDeviceSearch.value.trim().toLowerCase()
  if (!kw) return lhDevices.value
  return lhDevices.value.filter(
    (d) => (d.name || '').toLowerCase().includes(kw) || (d.ip || '').includes(kw))
})

const allFilteredSelected = computed(() =>
  lhFilteredDevices.value.length > 0 &&
  lhFilteredDevices.value.every((d) => lhSelected.value.includes(d.id)))

const canSubmit = computed(() =>
  lhSelected.value.length > 0 && /^(\d{1,3}\.){3}\d{1,3}$/.test(lhForm.value.address || ''))

// 预览命令按厂商分组：华为与 H3C 仅「保存配置」一条不同（华为没有 save force），
// 混选设备时必须分别展示，否则会把 H3C 的命令显示给华为设备。
const lhVariants = computed(() => {
  const p = lhPreview.value
  if (!p) return []
  const groups = { true: [], false: [] }
  for (const d of p.devices || []) {
    groups[d.huawei ? 'true' : 'false'].push(d.name)
  }
  const all = (p.variants && p.variants.length) ? p.variants : [{
    vendor: '', huawei: false, commands: p.commands, rollback_commands: p.rollback_commands,
  }]
  const picked = all.filter((v) => groups[v.huawei ? 'true' : 'false'].length > 0)
  return (picked.length ? picked : all).map((v) => ({
    ...v, devices: groups[v.huawei ? 'true' : 'false'].join('、'),
  }))
})

function variantCommandsText(rollback) {
  return lhVariants.value.map((v) => {
    const cmds = (rollback ? v.rollback_commands : v.commands) || []
    return lhVariants.value.length > 1
      ? `# ${v.vendor}（${v.devices}）\n${cmds.join('\n')}`
      : cmds.join('\n')
  }).join('\n\n')
}

const pendingCommandsText = computed(() => {
  if (confirmDialog.value?.commands?.length) return confirmDialog.value.commands.join('\n')
  if (!lhPreview.value) return '（未预览，将使用默认命令）'
  return variantCommandsText(!!confirmDialog.value?.rollback)
})

function clearPreview() {
  lhPreview.value = null
}

function selectAllFiltered() {
  if (allFilteredSelected.value) {
    const ids = new Set(lhFilteredDevices.value.map((d) => d.id))
    lhSelected.value = lhSelected.value.filter((id) => !ids.has(id))
  } else {
    const set = new Set(lhSelected.value)
    lhFilteredDevices.value.forEach((d) => set.add(d.id))
    lhSelected.value = [...set]
  }
  clearPreview()
}

function lhStatusOf(deviceId) {
  return lhStatus.value.find((s) => s.device_id === deviceId)
}

// 「实际收到日志」是配置真的生效的硬证据：设备在往本平台发日志，就说明它侧边
// 确实配好了 —— 此时无论下发记录是什么状态（甚至根本没有记录），都按"已下发"呈现，
// 否则操作人会以为没配上而重复下发（对生产设备是二次真实变更）。
function lhDisplayStatus(deviceId, item) {
  const s = item || lhStatusOf(deviceId)
  if (!s) return null
  return s.receiving ? 'applied' : s.status
}

function lhStatusHint(deviceId, item) {
  const s = item || lhStatusOf(deviceId)
  if (!s) return ''
  const parts = []
  if (s.receiving) {
    parts.push(`近 ${lhStatusInfo.value.receiving_days} 天已收到该设备日志 ${s.log_count} 条`
      + `（最近 ${fmtTime(s.last_log_at)}），设备侧确实在往本平台发日志`)
  }
  if (s.source === 'traffic') parts.push('平台没有下发记录，应为设备侧手工配置')
  if (s.status && s.status !== 'receiving') {
    parts.push(`下发记录：${STATUS_LABEL[s.status] || s.status}`)
  }
  if (s.message) parts.push(s.message)
  return parts.join('；')
}

const STATUS_BADGE = {
  applied: 'badge-success', rolled_back: 'badge-neutral',
  unverified: 'badge-warning', failed: 'badge-danger', receiving: 'badge-success',
}
const STATUS_LABEL = {
  applied: '已下发', rolled_back: '已回滚',
  unverified: '待确认', failed: '失败', receiving: '已下发',
}
const statusBadgeClass = (s) => STATUS_BADGE[s] || 'badge-neutral'
const statusLabel = (s) => STATUS_LABEL[s] || (s ? s : '未配置')

// 顶部「日志主机」卡片与详情弹层：口径与后端 /loghost/status 的 summary 完全一致
// （两处各算一套必然对不上：曾经卡片 10 台、副标题却只凑出 9 台）。
const lhHostSummary = computed(() => lhStatusInfo.value.summary || {})
const lhManaged = computed(() => lhStatus.value.filter(
  (s) => s.receiving || ['applied', 'unverified'].includes(s.status)))

function applyLoghostStatus(status) {
  lhStatus.value = status?.items || []
  lhStatusInfo.value = {
    summary: status?.summary || {},
    platform_address: status?.platform_address || null,
    udp_port: status?.udp_port ?? null,
    receiving_days: status?.receiving_days ?? 7,
  }
}

function openLoghostDetail() {
  lhDetailOpen.value = true
  loadLoghostStatus()   // 卡片数字可能是几分钟前的，点开时刷新一次
}

// 「待确认」= 命令已下发且无报错，但配置回读不可信，无法确认是否生效。
// 不能当失败看（会诱导重复下发），也不能当成功看（数据未必落上）。
const isUnverified = (r) => (r?.state || (r?.ok ? 'applied' : 'failed')) === 'unverified'

const lhSummary = computed(() => {
  const c = { applied: 0, unverified: 0, failed: 0 }
  for (const r of lhResults.value) {
    if (r.ok) c.applied += 1
    else if (isUnverified(r)) c.unverified += 1
    else c.failed += 1
  }
  return c
})

async function loadLoghost() {
  try {
    const [cand, status, devs] = await Promise.all([
      getLoghostCandidates(),
      getLoghostStatus(),
      getDevices({ page: 1, page_size: 300 }),
    ])
    lhCandidates.value = cand.candidates || []
    // 已有配置的平台地址优先，其次配置项，最后本机第一个候选地址
    if (!lhForm.value.address) {
      lhForm.value.address = cand.configured || cand.candidates?.[0] || ''
    }
    applyLoghostStatus(status)
    lhDevices.value = devs.items || []
  } catch (e) {
    console.error('加载日志主机配置失败', e)
  }
}

function lhPayload(ids, extra = {}) {
  const port = lhForm.value.port === '' ? null : Number(lhForm.value.port)
  return {
    device_ids: ids,
    address: lhForm.value.address,
    port,
    level: lhForm.value.level,
    save: lhForm.value.save,
    ...extra,
  }
}

async function doPreview() {
  lhLoading.value = true
  try {
    lhPreview.value = await previewLoghost(lhPayload(lhSelected.value))
  } catch (e) {
    alert('预览失败：' + (e?.response?.data?.detail || e?.message || e))
  } finally {
    lhLoading.value = false
  }
}

function copyCommands() {
  const lines = [`# 下发到 ${lhPreview.value?.devices?.length || 0} 台设备`]
  for (const v of lhVariants.value) {
    if (lhVariants.value.length > 1) lines.push(`# ${v.vendor}（${v.devices}）`)
    lines.push(...(v.commands || []), '')
  }
  lines.push('# 回滚命令')
  for (const v of lhVariants.value) {
    if (lhVariants.value.length > 1) lines.push(`# ${v.vendor}（${v.devices}）`)
    lines.push(...(v.rollback_commands || []))
  }
  copyText(lines.join('\n'))
}

function askApply() {
  pendingIds.value = [...lhSelected.value]
  confirmDialog.value = {
    title: '确认下发日志主机配置',
    danger: true,
    rollback: false,
    lines: [
      `将向 ${pendingIds.value.length} 台设备下发日志主机 ${lhForm.value.address}:${lhForm.value.port || 514}。`,
      `日志级别 ${lhForm.value.level}；${lhForm.value.save ? '并保存到启动配置（重启不丢失）' : '仅运行时生效（重启失效）'}。`,
      '平台会先读取 info-center 配置快照，逐条下发并回验；失败可一键回滚。',
    ],
    action: doApply,
  }
  if (!lhPreview.value) doPreview()
}

async function doApply() {
  const ids = pendingIds.value
  lhApplying.value = true
  lhResults.value = []
  lhProgress.value = { done: 0, total: ids.length, label: '正在下发' }
  try {
    for (let i = 0; i < ids.length; i += APPLY_CHUNK) {
      const part = ids.slice(i, i + APPLY_CHUNK)
      const res = await applyLoghost(lhPayload(part, { confirm: true }))
      lhResults.value.push(...(res.results || []))
      lhProgress.value = { ...lhProgress.value, done: Math.min(i + APPLY_CHUNK, ids.length) }
      await loadLoghostStatus()
    }
  } catch (e) {
    lhResults.value.push({
      ok: false, device_name: '（本批次请求失败）', ip: '',
      error: e?.response?.data?.detail || e?.message || String(e),
    })
  } finally {
    lhApplying.value = false
  }
}

function askRollback() {
  pendingIds.value = [...lhSelected.value]
  // 「待确认」也要算作有记录：命令很可能已经生效，正需要它的地址与 before 快照来回滚
  const withRecord = pendingIds.value.filter((id) => {
    const st = lhStatusOf(id)?.status
    return st === 'applied' || st === 'unverified'
  })
  const pending = pendingIds.value.filter((id) => lhStatusOf(id)?.status === 'unverified').length
  confirmDialog.value = {
    title: '确认回滚日志主机配置',
    danger: true,
    rollback: true,
    lines: [
      `将对 ${pendingIds.value.length} 台设备执行 undo info-center loghost。`,
      withRecord.length < pendingIds.value.length
        ? `其中仅 ${withRecord.length} 台有下发记录（地址与快照来自记录），其余需按当前地址回滚。`
        : '地址与 info-center 原始状态将取自各设备最近一次下发记录。',
      ...(pending ? [`其中 ${pending} 台上次下发状态为「待确认」，建议先登录设备核对其实际配置。`] : []),
      '若下发前 info-center 处于关闭状态，回滚会一并还原为关闭。',
    ],
    action: doRollback,
  }
}

async function doRollback() {
  const ids = pendingIds.value
  lhApplying.value = true
  lhResults.value = []
  lhProgress.value = { done: 0, total: ids.length, label: '正在回滚' }
  try {
    for (let i = 0; i < ids.length; i += APPLY_CHUNK) {
      const part = ids.slice(i, i + APPLY_CHUNK)
      const res = await rollbackLoghost(lhPayload(part, { confirm: true }))
      lhResults.value.push(...(res.results || []))
      lhProgress.value = { ...lhProgress.value, done: Math.min(i + APPLY_CHUNK, ids.length) }
      await loadLoghostStatus()
    }
  } catch (e) {
    lhResults.value.push({
      ok: false, device_name: '（本批次请求失败）', ip: '',
      error: e?.response?.data?.detail || e?.message || String(e),
    })
  } finally {
    lhApplying.value = false
  }
}

async function loadLoghostStatus() {
  try {
    applyLoghostStatus(await getLoghostStatus())
  } catch (e) {
    console.error('加载日志主机状态失败', e)
  }
}

function disposeCharts() {
  hourChart?.dispose()
  hourChart = null
  catChart?.dispose()
  catChart = null
}

function switchTab(key) {
  activeTab.value = key
  if (key === 'loghost') {
    // 切换 Tab 会把图表容器整个从 DOM 移除（v-if），不销毁实例再切回来会画在
    // 已脱离文档的节点上（页面空白、控制台无报错，很难查）。
    disposeCharts()
    loadLoghost()
  } else {
    nextTick(renderCharts)
  }
}

// ---------------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------------
let timer = null

onMounted(async () => {
  // 日志主机状态与统计卡同时在首屏加载：顶部「日志主机」卡片要立刻有数
  await Promise.all([loadReceiver(), loadMeta(), loadLoghostStatus()])
  await Promise.all([loadLogs(), loadStats()])
  window.addEventListener('resize', onResize)
  offTheme = onThemeChange(() => nextTick(renderCharts))
  // 接收器状态轮询：设备在发、平台没收到时，用户能第一时间看到
  timer = setInterval(loadReceiver, 15000)
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  offTheme?.()
  clearInterval(timer)
  disposeCharts()
})

// 时间范围变化时刷新筛选项（模块/设备列表随之变化）
watch(() => f.value.range, () => { if (activeTab.value === 'logs') loadMeta() })
</script>
