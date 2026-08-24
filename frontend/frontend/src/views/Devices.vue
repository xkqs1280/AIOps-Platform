<template>
  <div class="min-h-screen bg-slate-950 text-slate-100 p-6 animate-in">
    <!-- Top Section: Search, Filters, Add Button -->
    <div class="flex flex-wrap items-end gap-4 mb-6">
      <div class="flex-1 min-w-[200px]">
        <label class="block text-sm text-slate-400 mb-1">关键词搜索</label>
        <input
          v-model="searchKeyword"
          type="text"
          placeholder="搜索设备名称、IP..."
          class="input"
          @keyup.enter="handleSearch"
        />
      </div>

      <div class="w-40">
        <label class="block text-sm text-slate-400 mb-1">厂商</label>
        <select
          v-model="filterVendor"
          class="select"
          @change="handleSearch"
        >
          <option value="">全部</option>
          <option value="Huawei">华为</option>
          <option value="H3C">H3C</option>
          <option value="Cisco">思科</option>
          <option value="锐捷">锐捷</option>
          <option value="深信服">深信服</option>
        </select>
      </div>

      <div class="w-40">
        <label class="block text-sm text-slate-400 mb-1">设备类型</label>
        <select
          v-model="filterDeviceType"
          class="select"
          @change="handleSearch"
        >
          <option value="">全部</option>
          <option value="router">路由器</option>
          <option value="switch">交换机</option>
          <option value="firewall">防火墙</option>
          <option value="load_balancer">负载均衡</option>
          <option value="server">服务器</option>
        </select>
      </div>

      <div class="w-36">
        <label class="block text-sm text-slate-400 mb-1">状态</label>
        <select
          v-model="filterStatus"
          class="select"
          @change="handleSearch"
        >
          <option value="">全部</option>
          <option value="online">在线</option>
          <option value="warning">告警</option>
          <option value="offline">离线</option>
          <option value="unknown">未知</option>
        </select>
      </div>

      <button
        class="btn btn-danger"
        @click="openBatchDelete"
      >
        批量删除
      </button>

      <button
        class="btn btn-ghost"
        :disabled="exporting"
        @click="handleExport"
      >
        {{ exporting ? '导出中...' : '⬇ 导出清单' }}
      </button>

      <button
        class="btn btn-primary"
        @click="showAddDialog = true"
      >
        + 添加设备
      </button>
    </div>

    <!-- Device Table -->
    <div class="bg-slate-900 rounded-xl border border-slate-800 overflow-hidden">
      <div class="overflow-x-auto">
        <table class="w-full">
          <thead>
            <tr class="bg-slate-800/50 text-left text-sm text-slate-400">
              <th class="px-4 py-3 w-10">
                <input
                  type="checkbox"
                  class="checkbox"
                  :checked="isAllSelected"
                  @change="toggleSelectAll"
                />
              </th>
              <th
                class="px-4 py-3 font-medium cursor-pointer select-none hover:text-slate-200 transition-colors"
                @click="toggleSort('name')"
              >名称{{ sortArrow('name') }}</th>
              <th
                class="px-4 py-3 font-medium cursor-pointer select-none hover:text-slate-200 transition-colors"
                @click="toggleSort('ip')"
              >IP 地址{{ sortArrow('ip') }}</th>
              <th
                class="px-4 py-3 font-medium cursor-pointer select-none hover:text-slate-200 transition-colors"
                @click="toggleSort('vendor')"
              >厂商{{ sortArrow('vendor') }}</th>
              <th
                class="px-4 py-3 font-medium cursor-pointer select-none hover:text-slate-200 transition-colors"
                @click="toggleSort('model')"
              >型号{{ sortArrow('model') }}</th>
              <th
                class="px-4 py-3 font-medium cursor-pointer select-none hover:text-slate-200 transition-colors"
                @click="toggleSort('device_type')"
              >类型{{ sortArrow('device_type') }}</th>
              <th
                class="px-4 py-3 font-medium cursor-pointer select-none hover:text-slate-200 transition-colors"
                @click="toggleSort('status')"
              >状态{{ sortArrow('status') }}</th>
              <th
                class="px-4 py-3 font-medium cursor-pointer select-none hover:text-slate-200 transition-colors"
                @click="toggleSort('cpu_usage')"
              >CPU%{{ sortArrow('cpu_usage') }}</th>
              <th
                class="px-4 py-3 font-medium cursor-pointer select-none hover:text-slate-200 transition-colors"
                @click="toggleSort('memory_usage')"
              >内存%{{ sortArrow('memory_usage') }}</th>
              <th
                class="px-4 py-3 font-medium cursor-pointer select-none hover:text-slate-200 transition-colors"
                @click="toggleSort('last_seen')"
              >最后在线{{ sortArrow('last_seen') }}</th>
              <th class="px-4 py-3 font-medium">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800">
            <tr v-if="loading" v-for="i in 5" :key="'sk-' + i" class="animate-pulse">
              <td v-for="j in 10" :key="'sc-' + j" class="px-4 py-3">
                <div class="h-4 bg-slate-700 rounded w-3/4"></div>
              </td>
            </tr>
            <tr
              v-else-if="devices.length === 0"
            >
              <td colspan="11" class="px-4 py-12 text-center text-slate-500">
                暂无设备数据
              </td>
            </tr>
            <tr
              v-for="device in devices"
              :key="device.id"
              class="hover:bg-slate-800/30 transition-colors"
              :class="{ 'bg-red-900/20': selectedIds.includes(device.id) }"
            >
              <td class="px-4 py-3">
                <input
                  type="checkbox"
                  class="checkbox"
                  :checked="selectedIds.includes(device.id)"
                  @change="toggleSelect(device.id)"
                />
              </td>
              <td class="px-4 py-3">
                <router-link
                  :to="`/devices/${device.id}`"
                  class="text-blue-400 hover:text-blue-300 hover:underline transition-colors"
                >
                  {{ device.name }}
                </router-link>
              </td>
              <td class="px-4 py-3 text-slate-300 font-mono text-sm">{{ device.ip }}</td>
              <td class="px-4 py-3 text-slate-300">{{ device.vendor }}</td>
              <td class="px-4 py-3 text-slate-300">{{ device.model || '-' }}</td>
              <td class="px-4 py-3 text-slate-300">{{ deviceTypeLabel(device.device_type) }}</td>
              <td class="px-4 py-3">
                <span :class="statusBadgeClass(device.status)" class="px-2 py-1 rounded-full text-xs font-medium">
                  {{ statusLabel(device.status) }}
                </span>
              </td>
              <td class="px-4 py-3">
                <div class="flex items-center gap-2">
                  <div class="w-12 bg-slate-700 rounded-full h-1.5">
                    <div
                      class="h-1.5 rounded-full transition-all"
                      :class="cpuBarColor(device.cpu_usage)"
                      :style="{ width: (device.cpu_usage ?? 0) + '%' }"
                    ></div>
                  </div>
                  <span class="text-sm text-slate-300">{{ device.cpu_usage ?? '-' }}%</span>
                </div>
              </td>
              <td class="px-4 py-3">
                <div class="flex items-center gap-2">
                  <div class="w-12 bg-slate-700 rounded-full h-1.5">
                    <div
                      class="h-1.5 rounded-full transition-all"
                      :class="memBarColor(device.memory_usage)"
                      :style="{ width: (device.memory_usage ?? 0) + '%' }"
                    ></div>
                  </div>
                  <span class="text-sm text-slate-300">{{ device.memory_usage ?? '-' }}%</span>
                </div>
              </td>
              <td class="px-4 py-3 text-sm text-slate-400">{{ formatTime(device.last_seen) }}</td>
              <td class="px-4 py-3">
                <div class="flex items-center gap-2">
                  <router-link
                    :to="`/devices/${device.id}`"
                    class="px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 rounded text-slate-300 transition-colors"
                  >
                    编辑
                  </router-link>
                  <button
                    class="px-2 py-1 text-xs bg-cyan-900/50 hover:bg-cyan-800 rounded text-cyan-300 transition-colors"
                    :disabled="syncingIds.includes(device.id)"
                    @click="syncOne(device)"
                  >
                    {{ syncingIds.includes(device.id) ? '同步中…' : '同步' }}
                  </button>
                  <button
                    class="px-2 py-1 text-xs bg-red-900/50 hover:bg-red-800 rounded text-red-300 transition-colors"
                    @click="confirmDelete(device)"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Pagination -->
    <div v-if="total > 0" class="flex items-center justify-between mt-4 text-sm text-slate-400">
      <span>共 {{ total }} 台设备（上限 {{ MAX_DEVICES }} 台，剩余 {{ Math.max(0, MAX_DEVICES - total) }} 台可添加）</span>
      <div class="flex items-center gap-1">
        <button
          class="px-3 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          :disabled="page <= 1"
          @click="page--; fetchDevices()"
        >
          上一页
        </button>
        <span class="px-3 py-1 text-slate-300">
          第 {{ page }} / {{ totalPages }} 页
        </span>
        <button
          class="px-3 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          :disabled="page >= totalPages"
          @click="page++; fetchDevices()"
        >
          下一页
        </button>
      </div>
      <div class="flex items-center gap-2">
        <span>每页</span>
        <select
          v-model="pageSize"
          class="px-2 py-1 bg-slate-800 border border-slate-700 rounded text-slate-300 focus:outline-none focus:border-blue-500"
          @change="page = 1; fetchDevices()"
        >
          <option :value="10">10</option>
          <option :value="20">20</option>
          <option :value="50">50</option>
        </select>
        <span>条</span>
      </div>
    </div>

    <!-- Delete Confirmation Dialog -->
    <div
      v-if="deleteTarget"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
    >
      <div class="bg-slate-900 border border-slate-700 rounded-xl p-6 w-96 shadow-2xl">
        <h3 class="text-lg font-semibold text-slate-100 mb-2">确认删除</h3>
        <p class="text-slate-400 mb-6">
          确定要删除设备 <span class="text-white font-medium">{{ deleteTarget.name }}</span> 吗？此操作不可撤销。
        </p>
        <div class="flex justify-end gap-3">
          <button
            class="btn btn-outline"
            @click="deleteTarget = null"
          >
            取消
          </button>
          <button
            class="btn btn-danger"
            :disabled="deleting"
            @click="handleDelete"
          >
            {{ deleting ? '删除中...' : '确认删除' }}
          </button>
        </div>
      </div>
    </div>

    <!-- Batch Delete Dialog -->
    <div
      v-if="showBatchDelete"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
    >
      <div class="bg-slate-900 border border-slate-700 rounded-xl p-6 w-[480px] shadow-2xl">
        <h3 class="text-lg font-semibold text-slate-100 mb-3">批量删除设备</h3>
        <p class="text-slate-400 mb-4">
          将删除 <span class="text-red-400 font-semibold">{{ batchDeleteTargets.length }}</span> 台设备：
          <span class="text-slate-300">{{ batchDeleteNames }}</span>
        </p>
        <p class="text-red-500/90 text-sm mb-6">此操作不可撤销，请确认。</p>
        <div class="flex justify-end gap-3">
          <button
            class="btn btn-outline"
            @click="showBatchDelete = false"
          >
            取消
          </button>
          <button
            class="btn btn-danger"
            :disabled="deleting"
            @click="handleBatchDelete"
          >
            {{ deleting ? '删除中...' : '确认批量删除' }}
          </button>
        </div>
      </div>
    </div>

    <!-- Add Device Dialog -->
    <div
      v-if="showAddDialog"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      @click.self="showAddDialog = false"
    >
      <div class="bg-slate-900 border border-slate-700 rounded-xl p-6 w-[640px] max-h-[85vh] overflow-auto shadow-2xl">
        <h3 class="text-lg font-semibold text-slate-100 mb-4">添加设备</h3>

        <!-- Mode tabs -->
        <div class="flex gap-2 mb-4">
          <button
            class="px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            :class="addMode === 'single' ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'"
            @click="addMode = 'single'"
          >
            单独添加
          </button>
          <button
            class="px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            :class="addMode === 'batch' ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'"
            @click="addMode = 'batch'"
          >
            批量发现
          </button>
        </div>

        <!-- Single Add Mode -->
        <div v-if="addMode === 'single'" class="space-y-3">
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-sm text-slate-400 mb-1">设备名称 *</label>
              <input v-model="singleForm.name" type="text" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="如: SW4">
            </div>
            <div>
              <label class="block text-sm text-slate-400 mb-1">IP 地址 *</label>
              <input v-model="singleForm.ip" type="text" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="如: 192.168.11.20">
            </div>
            <div>
              <label class="block text-sm text-slate-400 mb-1">厂商</label>
              <select v-model="singleForm.vendor" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500">
                <option value="">未知</option>
                <option value="华为">华为</option>
                <option value="H3C">H3C</option>
                <option value="Cisco">思科</option>
                <option value="锐捷">锐捷</option>
                <option value="深信服">深信服</option>
              </select>
            </div>
            <div>
              <label class="block text-sm text-slate-400 mb-1">设备类型</label>
              <select v-model="singleForm.device_type" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500">
                <option value="">未知</option>
                <option value="router">路由器</option>
                <option value="switch">交换机</option>
                <option value="firewall">防火墙</option>
                <option value="load_balancer">负载均衡</option>
                <option value="server">服务器</option>
              </select>
            </div>
            <div>
              <label class="block text-sm text-slate-400 mb-1">SNMP 团体字</label>
              <input v-model="singleForm.snmp_community" type="text" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="aiops">
            </div>
            <div>
              <label class="block text-sm text-slate-400 mb-1">SNMP 版本</label>
              <select v-model="singleForm.snmp_version" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500">
                <option value="v2c">v2c</option>
                <option value="v3">v3</option>
                <option value="v1">v1</option>
              </select>
            </div>
            <div>
              <label class="block text-sm text-slate-400 mb-1">型号</label>
              <input v-model="singleForm.model" type="text" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="如: S5700-28C-HI">
            </div>
            <div>
              <label class="block text-sm text-slate-400 mb-1">位置</label>
              <input v-model="singleForm.location" type="text" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="如: 机房A-机柜3">
            </div>
          </div>

          <!-- 远程管理配置 -->
          <div class="border-t border-slate-700 pt-3 mt-3">
            <div class="text-sm text-slate-400 mb-2 font-medium">远程管理配置</div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="block text-sm text-slate-400 mb-1">管理协议</label>
                <select v-model="singleForm.mgmt_protocol" @change="onProtocolChange(singleForm)" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500">
                  <option value="ssh">SSH</option>
                  <option value="telnet">Telnet</option>
                </select>
              </div>
              <div>
                <label class="block text-sm text-slate-400 mb-1">端口</label>
                <input v-model.number="singleForm.mgmt_port" type="number" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="22">
              </div>
              <div>
                <label class="block text-sm text-slate-400 mb-1">用户名</label>
                <input v-model="singleForm.mgmt_username" type="text" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="如: admin">
              </div>
              <div>
                <label class="block text-sm text-slate-400 mb-1">密码</label>
                <input v-model="singleForm.mgmt_password" type="password" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="输入管理密码">
              </div>
            </div>
          </div>
          <div class="flex justify-end gap-3 pt-2">
            <button class="btn btn-outline" @click="showAddDialog = false">取消</button>
            <button class="btn btn-primary" :disabled="!singleForm.name || !singleForm.ip" @click="addSingleDevice">添加设备</button>
          </div>
        </div>

        <!-- Batch Discovery Mode -->
        <div v-if="addMode === 'batch'" class="space-y-3">
          <div>
            <label class="block text-sm text-slate-400 mb-1">IP 列表（每行一个IP或用逗号分隔）</label>
            <textarea
              v-model="batchIpList"
              rows="5"
              class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500 font-mono text-sm"
              placeholder="192.168.11.10&#10;192.168.11.20&#10;192.168.11.30"
            ></textarea>
          </div>
          <div class="flex gap-3 items-end">
            <div class="flex-1">
              <label class="block text-sm text-slate-400 mb-1">SNMP 团体字</label>
              <input v-model="batchCommunity" type="text" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="aiops">
            </div>
            <button
              class="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 rounded-lg text-white font-medium text-sm transition-colors whitespace-nowrap"
              :disabled="discovering || !batchIpList.trim()"
              @click="runDiscovery"
            >
              {{ discovering ? '发现中...' : 'SNMP 发现' }}
            </button>
          </div>

          <!-- 远程管理凭据（批量添加时统一设置） -->
          <div class="border-t border-slate-700 pt-3">
            <div class="text-sm text-slate-400 mb-2 font-medium">远程管理凭据（应用于所有发现的设备）</div>
            <div class="grid grid-cols-4 gap-3">
              <div>
                <label class="block text-sm text-slate-400 mb-1">管理协议</label>
                <select v-model="batchMgmt.protocol" @change="onProtocolChange(batchMgmt)" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500">
                  <option value="ssh">SSH</option>
                  <option value="telnet">Telnet</option>
                </select>
              </div>
              <div>
                <label class="block text-sm text-slate-400 mb-1">端口</label>
                <input v-model.number="batchMgmt.port" type="number" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="22">
              </div>
              <div>
                <label class="block text-sm text-slate-400 mb-1">用户名</label>
                <input v-model="batchMgmt.username" type="text" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="如: admin">
              </div>
              <div>
                <label class="block text-sm text-slate-400 mb-1">密码</label>
                <input v-model="batchMgmt.password" type="password" class="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:border-blue-500" placeholder="输入管理密码">
              </div>
            </div>
          </div>

          <!-- Discovery Results -->
          <div v-if="discoveredDevices.length > 0" class="mt-4">
            <div class="flex items-center justify-between mb-2">
              <span class="text-sm text-slate-400">
                发现 {{ discoveredDevices.length }} 台设备
                <span v-if="discoveredManagedCount > 0" class="text-yellow-400 ml-2">（{{ discoveredManagedCount }} 台已纳管，不显示）</span>
              </span>
              <div class="flex gap-2">
                <button class="px-3 py-1 text-xs bg-slate-700 hover:bg-slate-600 rounded text-slate-300" @click="selectAllDiscovered">全选</button>
                <button
                  class="btn btn-primary btn-sm"
                  :disabled="selectedDiscovered.length === 0"
                  @click="addBatchDevices"
                >
                  添加选中 ({{ selectedDiscovered.length }})
                </button>
              </div>
            </div>

            <!-- Vendor filter for discovered devices -->
            <div class="flex gap-2 mb-2">
              <select v-model="discoveryVendorFilter" class="px-3 py-1.5 bg-slate-800 border border-slate-700 rounded-lg text-slate-100 text-sm focus:outline-none focus:border-blue-500">
                <option value="">全部厂商</option>
                <option value="华为">华为</option>
                <option value="H3C">H3C</option>
                <option value="Cisco">思科</option>
                <option value="锐捷">锐捷</option>
                <option value="深信服">深信服</option>
                <option value="unknown">未知</option>
              </select>
            </div>

            <div class="bg-slate-800/50 rounded-lg border border-slate-700 max-h-64 overflow-auto">
              <table class="w-full">
                <thead>
                  <tr class="text-left text-xs text-slate-400 border-b border-slate-700">
                    <th class="px-3 py-2 w-8"></th>
                    <th class="px-3 py-2">IP</th>
                    <th class="px-3 py-2">名称</th>
                    <th class="px-3 py-2">厂商</th>
                    <th class="px-3 py-2">型号</th>
                    <th class="px-3 py-2">类型</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="d in filteredDiscovered"
                    :key="d.ip"
                    class="border-b border-slate-800 text-sm hover:bg-slate-800/30"
                  >
                    <td class="px-3 py-2">
                      <input type="checkbox" :value="d.ip" v-model="selectedDiscovered" class="checkbox">
                    </td>
                    <td class="px-3 py-2 font-mono text-slate-300">{{ d.ip }}</td>
                    <td class="px-3 py-2 text-slate-300">{{ d.name || '-' }}</td>
                    <td class="px-3 py-2 text-slate-300">{{ d.vendor || '-' }}</td>
                    <td class="px-3 py-2 text-slate-400">{{ d.model || '-' }}</td>
                    <td class="px-3 py-2 text-slate-400">{{ deviceTypeMap[d.device_type] || d.device_type || '-' }}</td>
                  </tr>
                  <tr v-if="filteredDiscovered.length === 0">
                    <td colspan="6" class="px-3 py-6 text-center text-slate-500 text-sm">无可添加的设备</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          <div v-if="discoveryDone && discoveredDevices.length === 0" class="text-center text-slate-500 py-6">
            未发现任何设备，请检查 IP 和 SNMP 配置
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { getDevices, deleteDevice, batchDeleteDevices, createDevice, discoverDevices, batchCreateDevices, syncDevice, exportDevices } from '../api/index.js'

const searchKeyword = ref('')
const filterVendor = ref('')
const filterDeviceType = ref('')
const filterStatus = ref('')

const devices = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
// 表头排序：sortField 为空表示不排序
const sortField = ref('')
const sortOrder = ref('asc')

// 可排序列（表头点击切换 正/反）
const sortableColumns = ['name', 'ip', 'vendor', 'model', 'device_type', 'status', 'cpu_usage', 'memory_usage', 'last_seen']

function toggleSort(field) {
  if (sortField.value === field) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortField.value = field
    sortOrder.value = 'asc'
  }
  page.value = 1
  fetchDevices()
}
function sortArrow(field) {
  if (sortField.value !== field) return ''
  return sortOrder.value === 'asc' ? ' ▲' : ' ▼'
}
const loading = ref(false)

const deleteTarget = ref(null)
const deleting = ref(false)
const showAddDialog = ref(false)

// 批量删除：多选状态
const selectedIds = ref([])
const showBatchDelete = ref(false)
const batchDeleteTargets = ref([])
const isAllSelected = computed(
  () => devices.value.length > 0 && selectedIds.value.length === devices.value.length
)
const batchDeleteNames = computed(() => {
  const names = batchDeleteTargets.value.map(d => d.name).filter(Boolean)
  return names.length > 0 ? names.join('、') : '所选设备'
})

function toggleSelect(id) {
  const idx = selectedIds.value.indexOf(id)
  if (idx !== -1) selectedIds.value.splice(idx, 1)
  else selectedIds.value.push(id)
}

function toggleSelectAll() {
  if (isAllSelected.value) {
    selectedIds.value = []
  } else {
    selectedIds.value = devices.value.map(d => d.id)
  }
}

function openBatchDelete() {
  const targets = devices.value.filter(d => selectedIds.value.includes(d.id))
  if (targets.length === 0) {
    alert('请先勾选要删除的设备（可勾选表头全选框，或逐行勾选）')
    return
  }
  batchDeleteTargets.value = targets
  showBatchDelete.value = true
}

async function handleBatchDelete() {
  if (batchDeleteTargets.value.length === 0) return
  deleting.value = true
  try {
    const ids = batchDeleteTargets.value.map(d => d.id)
    await batchDeleteDevices({ device_ids: ids, delete_all: false })
    selectedIds.value = []
    showBatchDelete.value = false
    batchDeleteTargets.value = []
    fetchDevices()
  } catch (err) {
    const detail = err.response?.data?.detail || err.message
    alert(`批量删除失败：${detail}`)
  } finally {
    deleting.value = false
  }
}

// Add device
const addMode = ref('single')

// 切换管理协议时自动设置默认端口（SSH->22，Telnet->23）
// 兼容两种表单字段：单个添加用 mgmt_protocol，批量添加用 protocol
function onProtocolChange(form) {
  const proto = form.mgmt_protocol ?? form.protocol
  if (proto === 'ssh') {
    form.mgmt_port = 22
    form.port = 22
  } else if (proto === 'telnet') {
    form.mgmt_port = 23
    form.port = 23
  }
}

const singleForm = ref({
  name: '', ip: '', vendor: '', device_type: '', snmp_community: 'aiops',
  snmp_version: 'v2c', model: '', location: '',
  mgmt_protocol: 'ssh', mgmt_port: 22, mgmt_username: '', mgmt_password: '',
})
const batchIpList = ref('')
const batchCommunity = ref('aiops')
const batchMgmt = ref({
  protocol: 'ssh',
  port: 22,
  username: '',
  password: '',
})
const discovering = ref(false)
const discoveryDone = ref(false)
const discoveredDevices = ref([])
const discoveredManagedCount = ref(0)
const selectedDiscovered = ref([])
const discoveryVendorFilter = ref('')

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

// 平台纳管设备数上限（与后端 settings.MAX_DEVICES 保持一致）
const MAX_DEVICES = 300

const statusMap = { online: '在线', warning: '告警', offline: '离线', unknown: '未知' }
const deviceTypeMap = { router: '路由器', switch: '交换机', firewall: '防火墙', load_balancer: '负载均衡', server: '服务器', wireless: '无线控制器' }

function statusLabel(status) {
  return statusMap[status] || status
}

function deviceTypeLabel(type) {
  return deviceTypeMap[type] || type
}

function statusBadgeClass(status) {
  const map = {
    online: 'bg-green-900/50 text-green-400 border border-green-700',
    warning: 'bg-yellow-900/50 text-yellow-400 border border-yellow-700',
    offline: 'bg-red-900/50 text-red-400 border border-red-700',
    unknown: 'bg-slate-700/50 text-slate-400 border border-slate-600',
  }
  return map[status] || map.unknown
}

function cpuBarColor(usage) {
  if (usage == null) return 'bg-slate-500'
  if (usage >= 90) return 'bg-red-500'
  if (usage >= 70) return 'bg-yellow-500'
  return 'bg-green-500'
}

function memBarColor(usage) {
  if (usage == null) return 'bg-slate-500'
  if (usage >= 90) return 'bg-red-500'
  if (usage >= 70) return 'bg-yellow-500'
  return 'bg-blue-500'
}

function formatTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function fetchDevices() {
  loading.value = true
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (searchKeyword.value) params.keyword = searchKeyword.value
    if (filterVendor.value) params.vendor = filterVendor.value
    if (filterDeviceType.value) params.device_type = filterDeviceType.value
    if (filterStatus.value) params.status = filterStatus.value
    if (sortField.value) {
      params.sort = sortField.value
      params.order = sortOrder.value
    }

    const res = await getDevices(params)
    devices.value = res.items || []
    total.value = res.total || 0
    // 翻页/刷新后清空勾选（避免跨页残留误删）
    selectedIds.value = []
  } catch (err) {
    console.error('获取设备列表失败:', err)
  } finally {
    loading.value = false
  }
}

// 导出资产清单（遵循当前搜索/筛选条件，后端全量导出 Excel）
const exporting = ref(false)
async function handleExport() {
  if (exporting.value) return
  exporting.value = true
  try {
    const params = {}
    if (searchKeyword.value) params.keyword = searchKeyword.value
    if (filterVendor.value) params.vendor = filterVendor.value
    if (filterDeviceType.value) params.device_type = filterDeviceType.value
    if (filterStatus.value) params.status = filterStatus.value
    const res = await exportDevices(params)
    const blob = res instanceof Blob ? res : (res.data instanceof Blob ? res.data : new Blob([res]))
    const url = URL.createObjectURL(blob)
    const now = new Date()
    const pad = (n) => String(n).padStart(2, '0')
    const fname = `设备资产清单_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}.xlsx`
    const a = document.createElement('a')
    a.href = url
    a.download = fname
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (err) {
    alert('导出失败：' + (err.response?.data?.detail || err.message || '未知错误'))
  } finally {
    exporting.value = false
  }
}

function handleSearch() {
  page.value = 1
  fetchDevices()
}

function confirmDelete(device) {
  deleteTarget.value = device
}

async function handleDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteDevice(deleteTarget.value.id)
    deleteTarget.value = null
    fetchDevices()
  } catch (err) {
    const detail = err.response?.data?.detail || err.message
    alert(`删除设备失败：${detail}`)
  } finally {
    deleting.value = false
  }
}

// 同步单台设备信息（从设备 SNMP 重新发现，覆盖厂商/型号/类型/序列号/名称）
const syncingIds = ref([])
async function syncOne(device) {
  if (syncingIds.value.includes(device.id)) return
  syncingIds.value.push(device.id)
  try {
    const updated = await syncDevice(device.id)
    // 用返回值就地更新列表，避免整表刷新
    const idx = devices.value.findIndex(d => d.id === device.id)
    if (idx !== -1) devices.value[idx] = { ...devices.value[idx], ...updated }
    alert(`设备「${device.name}」同步完成：名称=${updated.name}，型号=${updated.model || '-'}`)
  } catch (err) {
    const detail = err.response?.data?.detail || err.message
    alert(`同步失败：${detail}`)
  } finally {
    syncingIds.value = syncingIds.value.filter(id => id !== device.id)
  }
}

// Filtered discovered devices (exclude already managed, apply vendor filter)
const filteredDiscovered = computed(() => {
  return discoveredDevices.value.filter(d => {
    if (d.already_managed) return false
    if (discoveryVendorFilter.value === 'unknown') return !d.vendor
    if (discoveryVendorFilter.value && d.vendor !== discoveryVendorFilter.value) return false
    return true
  })
})

function isValidIP(ip) {
  if (!ip || typeof ip !== 'string') return false
  ip = ip.trim()
  // IPv4：四段 0-255
  const parts = ip.split('.')
  if (parts.length === 4 && parts.every(p => /^\d{1,3}$/.test(p) && Number(p) <= 255)) return true
  // IPv6：含冒号且非空段
  if (ip.includes(':') && ip.split(':').length >= 2) return true
  return false
}

async function addSingleDevice() {
  if (!isValidIP(singleForm.value.ip)) {
    alert('IP 地址格式不正确，请检查后重试')
    return
  }
  try {
    await createDevice(singleForm.value)
    showAddDialog.value = false
    // Reset form
    singleForm.value = {
      name: '', ip: '', vendor: '', device_type: '', snmp_community: 'aiops',
      snmp_version: 'v2c', model: '', location: '',
      mgmt_protocol: 'ssh', mgmt_port: 22, mgmt_username: '', mgmt_password: '',
    }
    await fetchDevices()
  } catch (err) {
    alert('添加失败: ' + (err.response?.data?.detail || err.message))
  }
}

async function runDiscovery() {
  discovering.value = true
  discoveryDone.value = false
  discoveredDevices.value = []
  selectedDiscovered.value = []
  discoveredManagedCount.value = 0

  const ips = batchIpList.value
    .split(/[\n,]/)
    .map(ip => ip.trim())
    .filter(ip => ip)

  if (ips.length === 0) {
    discovering.value = false
    return
  }

  const invalidIps = ips.filter(ip => !isValidIP(ip))
  if (invalidIps.length) {
    discovering.value = false
    alert(`以下 IP 地址格式不正确，无法发现：\n${invalidIps.join('\n')}\n\n请修正后重新执行发现。`)
    return
  }

  try {
    const data = await discoverDevices({ ips, snmp_community: batchCommunity.value })
    discoveredDevices.value = data.discovered || []
    discoveredManagedCount.value = data.already_managed_count || 0
    discoveryDone.value = true
  } catch (err) {
    alert('发现失败: ' + (err.response?.data?.detail || err.message))
  }
  discovering.value = false
}

function selectAllDiscovered() {
  const allIps = filteredDiscovered.value.map(d => d.ip)
  if (selectedDiscovered.value.length === allIps.length) {
    selectedDiscovered.value = []
  } else {
    selectedDiscovered.value = allIps
  }
}

async function addBatchDevices() {
  const toAdd = discoveredDevices.value.filter(d => selectedDiscovered.value.includes(d.ip))
  const devicesPayload = toAdd.map(d => ({
    name: d.name || d.ip,
    ip: d.ip,
    vendor: d.vendor || '',
    model: d.model || '',
    device_type: d.device_type || '',
    snmp_version: 'v2c',
    snmp_community: batchCommunity.value,
    mgmt_protocol: batchMgmt.value.protocol,
    mgmt_port: batchMgmt.value.port,
    mgmt_username: batchMgmt.value.username,
    mgmt_password: batchMgmt.value.password,
  }))

  try {
    await batchCreateDevices({ devices: devicesPayload })
    selectedDiscovered.value = []
    // Remove added devices from discovered list
    const addedIps = new Set(toAdd.map(d => d.ip))
    discoveredDevices.value = discoveredDevices.value.filter(d => !addedIps.has(d.ip))
    await fetchDevices()
    alert(`成功添加 ${devicesPayload.length} 台设备`)
  } catch (err) {
    alert('添加失败: ' + (err.response?.data?.detail || err.message))
  }
}

onMounted(() => {
  fetchDevices()
})
</script>
