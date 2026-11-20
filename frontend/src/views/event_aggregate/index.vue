<template>
  <section class="page" data-module="event_aggregate">
    <header class="page-head">
      <div>
        <h2>事件聚合</h2>
        <p class="page-desc">
          维护埋点事件的多维聚合计算与预聚合结果管理；同一事件可以有多份口径，热力视图按口径消费。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="run-analysis" type="button" :disabled="loading" @click="runAnalysis">
          {{ loading ? '聚合中…' : '运行分析' }}
        </button>
      </div>
    </header>
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label>
        <span>配置编号</span>
        <select v-model="selectedConfig" data-testid="aggregate-config">
          <option v-for="c in configs" :key="c.config_id" :value="c.config_id">
            {{ c.config_id }} · {{ c.event_id }} · {{ c.agg_function }}
          </option>
        </select>
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis-submit">重新提交聚合</button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="aggregate-error">{{ errorMessage }}</p>

    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>

    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>

    <table v-if="coverageRows.length" class="data-table" data-testid="aggregate-coverage" style="margin-top:12px">
      <thead><tr><th>维度</th><th>全集</th><th>已交付</th><th>缺失</th><th>版本</th></tr></thead>
      <tbody>
        <tr v-for="row in coverageRows" :key="row.dimension">
          <td>{{ row.dimension }}</td>
          <td>{{ row.universe.join('、') }}</td>
          <td>{{ row.delivered.join('、') || '—' }}</td>
          <td>{{ row.missing.join('、') || '—' }}</td>
          <td>{{ row.revision }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请选择聚合配置后运行分析</p>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { fetchJson, request } from '@/api/client'

const ENDPOINT = '/api/event-aggregate'
type ConfigRow = {
  config_id: string
  event_id: string
  agg_function: string
  agg_dimensions: string[]
  time_grain: string
  status: string
  revision: number
  coverage: Record<string, { dimension: string; universe: string[]; delivered: string[]; missing: string[] }>
}
const summaryMetrics = ref<{ label: string; value: number; trend: number }[]>([])
const configs = ref<ConfigRow[]>([])
const selectedConfig = ref('')
const results = ref<any[]>([])
const coverageRows = ref<{ dimension: string; universe: string[]; delivered: string[]; missing: string[]; revision: number }[]>([])
const chartData = ref<{ label: string; value: number }[]>([])
const loading = ref(false)
const errorMessage = ref('')
const resultColumns = ['配置编号', '事件编号', '聚合函数', '维度值', '聚合值', '时间粒度']

function barHeight(v: number) {
  const max = Math.max(...chartData.value.map(d => d.value), 1)
  return Math.max(10, (v / max) * 140)
}

async function fetchSummary() {
  try {
    const data = await fetchJson<{ metrics: typeof summaryMetrics.value; configs: ConfigRow[] }>(`${ENDPOINT}/summary`)
    summaryMetrics.value = data.metrics || []
    configs.value = data.configs || []
    if (!selectedConfig.value && configs.value.length) selectedConfig.value = configs.value[0].config_id
  } catch {
    summaryMetrics.value = [
      { label: '聚合配置', value: 0, trend: 0 },
      { label: '已产出', value: 0, trend: 0 },
      { label: '待补算', value: 0, trend: 0 },
    ]
  }
}

async function runAnalysis() {
  if (!selectedConfig.value) return
  loading.value = true
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/run`, {
      method: 'POST',
      body: JSON.stringify({ values: { config_id: selectedConfig.value } }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.detail || `接口返回 ${res.status}`)
    results.value = (data.items || []).slice(0, 20).map((row: Record<string, unknown>, i: number) => ({ id: i, ...row }))
    chartData.value = results.value.slice(0, 6).map((row: any) => ({ label: String(row['维度值']), value: Number(row['聚合值']) || 0 }))
    const coverage = (data.result?.coverage || {}) as ConfigRow['coverage']
    coverageRows.value = Object.values(coverage).map((c) => ({
      dimension: c.dimension,
      universe: c.universe || [],
      delivered: c.delivered || [],
      missing: c.missing || [],
      revision: data.result?.revision ?? 0,
    }))
    await fetchSummary()
  } catch (e) {
    results.value = []
    chartData.value = []
    coverageRows.value = []
    errorMessage.value = e instanceof Error ? e.message : '聚合失败'
  } finally {
    loading.value = false
  }
}

onMounted(fetchSummary)
</script>
