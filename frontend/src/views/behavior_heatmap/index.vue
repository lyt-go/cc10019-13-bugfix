<template>
  <section class="page" data-module="behavior_heatmap">
    <header class="page-head">
      <div>
        <h2>行为热力</h2>
        <p class="page-desc">
          按模板口径消费事件聚合结果，展示热力格数据及对应色阶，并列出聚合尚未覆盖的地区。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-summary" type="button" :disabled="loading" @click="fetchSummary">
          刷新覆盖
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
        <select v-model="params.configId" data-testid="heatmap-config">
          <option v-for="c in configs" :key="c.config_id" :value="c.config_id">
            {{ c.config_id }} · {{ c.spatial_dim }} · {{ c.metric }}
          </option>
        </select>
      </label>
      <label>
        <span>模板口径</span>
        <select v-model="params.metric" data-testid="heatmap-metric">
          <option v-for="t in templates" :key="t.metric" :value="t.metric">{{ t.label }}</option>
        </select>
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="heatmap-error">{{ errorMessage }}</p>

    <template v-if="result">
      <div class="heatmap-meta" data-testid="heatmap-source">
        <span>口径：<strong>{{ result.metric_label }}</strong></span>
        <span>来源聚合：<strong>{{ result.source_aggregate }}</strong>（版本 {{ result.source_revision }}）</span>
        <span>色阶：<strong>{{ result.color_scale }}</strong></span>
        <span>可比维度：<strong data-testid="heatmap-comparable">{{ result.comparable_regions.join('、') || '—' }}</strong></span>
        <span>可比数量：<strong data-testid="heatmap-covered-count">{{ result.comparable_regions.length }}</strong></span>
        <span>未覆盖地区：<strong data-testid="heatmap-uncovered-count">{{ result.uncovered_regions.length }}</strong></span>
      </div>

      <div class="heatmap-legend" data-testid="heatmap-legend">
        <span v-for="stop in result.color_legend.stops" :key="stop.value" class="legend-stop">
          <i :style="{ background: stop.color }"></i>{{ stop.value }}
        </span>
      </div>

      <div v-if="result.uncovered_regions.length" class="heatmap-uncovered" data-testid="heatmap-uncovered">
        <strong>尚未覆盖的地区：</strong>
        <span v-for="region in result.uncovered_regions" :key="region" class="tag-missing">{{ region }}</span>
        <em>（这些地区没有聚合数据，不按 0 展示，也不参与冷点排序）</em>
      </div>

      <div class="heatmap-cold" data-testid="heatmap-cold">
        <strong>冷点（按{{ result.metric_label }}）：</strong>
        <span v-for="item in result.cold_regions" :key="item.dimension" class="tag-cold">
          {{ item.dimension }} · {{ item.value }}
        </span>
      </div>

      <div class="analysis-chart">
        <div
          v-for="bar in chartData"
          :key="bar.label"
          class="analysis-bar"
          :style="{ height: barHeight(bar.value) + 'px', background: bar.color }"
          :data-testid="`heat-cell-${bar.label}`"
        ><span>{{ bar.label }}</span></div>
      </div>

      <table class="data-table" data-testid="heatmap-table">
        <thead>
          <tr><th>地区</th><th>时段</th><th>口径</th><th>格值</th><th>色阶</th><th>来源聚合</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in result.cells" :key="`${row.dimension}-${row.time_slot}`">
            <td>{{ row.dimension }}</td>
            <td>{{ row.time_slot }}</td>
            <td>{{ row.unit }}</td>
            <td>{{ row.value }}</td>
            <td><i class="swatch" :style="{ background: row.color }"></i>{{ row.color }}</td>
            <td>{{ result.source_aggregate }}</td>
          </tr>
        </tbody>
      </table>
    </template>

    <p v-if="!result && !loading" style="text-align:center;color:var(--muted)">请选择配置与模板口径后运行分析</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Template = { metric: string; label: string; description: string }
type SummaryConfig = {
  config_id: string
  event_id: string
  spatial_dim: string
  metric: string
  covered_regions: string[]
  uncovered_regions: string[]
  status: string
}
type Summary = {
  metrics: { label: string; value: number; trend: number }[]
  templates: Template[]
  configs: SummaryConfig[]
}
type HeatCell = {
  dimension: string
  time_slot: string
  value: number
  intensity: number
  unit: string
  color: string
}
type HeatResult = {
  metric: string
  metric_label: string
  color_scale: string
  color_legend: { stops: { value: number; color: string }[] }
  cells: HeatCell[]
  covered_regions: string[]
  comparable_regions: string[]
  uncovered_regions: string[]
  source_aggregate: string | null
  source_revision: number
  cold_regions: { dimension: string; value: number }[]
}

const ENDPOINT = '/api/behavior-heatmap'
const summaryMetrics = ref<Summary['metrics']>([])
const templates = ref<Template[]>([])
const configs = ref<SummaryConfig[]>([])
const params = ref<{ configId: string; metric: string }>({ configId: '', metric: 'uv' })
const result = ref<HeatResult | null>(null)
const loading = ref(false)
const errorMessage = ref('')

// 图表按地区聚合：人数取各时段峰值，次数取各时段合计，与后端口径一致。
const chartData = computed(() => {
  if (!result.value) return []
  const totals = new Map<string, { label: string; value: number; color: string }>()
  for (const cell of result.value.cells) {
    const current = totals.get(cell.dimension)
    const next = !current
      ? cell.value
      : result.value!.metric === 'uv'
        ? Math.max(current.value, cell.value)
        : current.value + cell.value
    totals.set(cell.dimension, { label: cell.dimension, value: next, color: cell.color })
  }
  return [...totals.values()].slice(0, 8)
})

function barHeight(value: number) {
  const max = Math.max(...chartData.value.map((d) => d.value), 1)
  return Math.max(10, (value / max) * 140)
}

async function fetchSummary() {
  try {
    const data = await fetchJson<Summary>(`${ENDPOINT}/summary`)
    summaryMetrics.value = data.metrics || []
    templates.value = data.templates || []
    configs.value = data.configs || []
    if (!params.value.configId && configs.value.length) {
      params.value.configId = configs.value[0].config_id
      params.value.metric = configs.value[0].metric || params.value.metric
    }
  } catch {
    summaryMetrics.value = [
      { label: '热力配置', value: 0, trend: 0 },
      { label: '可比地区', value: 0, trend: 0 },
      { label: '未覆盖地区', value: 0, trend: 0 },
    ]
  }
}

async function runAnalysis() {
  loading.value = true
  errorMessage.value = ''
  try {
    const values: Record<string, string> = { config_id: params.value.configId }
    if (params.value.metric) values.metric = params.value.metric
    const res = await request(`${ENDPOINT}/run`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}))
      throw new Error(detail.detail || `接口返回 ${res.status}`)
    }
    const data = await res.json()
    result.value = data.result || null
  } catch (e) {
    result.value = null
    errorMessage.value = e instanceof Error ? e.message : '热力图生成失败'
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await fetchSummary()
  // 首次打开面板时按默认模板口径取一次格值与色阶
  if (params.value.configId) await runAnalysis()
})
</script>

<style scoped>
.heatmap-meta { display: flex; flex-wrap: wrap; gap: 16px; margin: 8px 0; font-size: 13px; color: var(--muted); }
.heatmap-legend { display: flex; gap: 12px; align-items: center; margin: 8px 0; font-size: 12px; color: var(--muted); }
.legend-stop { display: inline-flex; align-items: center; gap: 4px; }
.legend-stop i { width: 16px; height: 10px; border-radius: 2px; display: inline-block; }
.heatmap-uncovered { margin: 8px 0; font-size: 13px; color: #92400e; }
.heatmap-uncovered em { color: var(--muted); font-style: normal; font-size: 12px; }
.tag-missing { display: inline-block; margin: 0 6px; padding: 1px 8px; border: 1px dashed #d97706; border-radius: 10px; font-size: 12px; }
.heatmap-cold { margin: 8px 0; font-size: 13px; color: var(--muted); }
.tag-cold { display: inline-block; margin: 0 6px; padding: 1px 8px; background: #eef2ff; border-radius: 10px; font-size: 12px; color: #3730a3; }
.swatch { display: inline-block; width: 12px; height: 12px; border-radius: 2px; margin-right: 6px; vertical-align: middle; }
</style>
