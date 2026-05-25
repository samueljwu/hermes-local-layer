<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { withBase } from 'vitepress'

const loading = ref(true)
const error = ref('')
const graph = ref(null)
const search = ref('')
const includeRaw = ref(false)
const includeSystem = ref(false)
const includeQueries = ref(true)
const activeTypes = ref(new Set(['concept', 'entity', 'comparison', 'query']))
const activeRelationships = ref(new Set())
const selectedNode = ref(null)
const svgRef = ref(null)
const containerRef = ref(null)

let d3 = null
let simulation = null
let resizeObserver = null

const typeColors = {
  concept: '#60a5fa',
  entity: '#f59e0b',
  comparison: '#a78bfa',
  query: '#34d399',
  raw: '#94a3b8',
  system: '#64748b',
  tag: '#f472b6',
}

const relationshipLabels = {
  related_to: 'related to',
  mentions: 'mentions',
  derived_from: 'derived from',
  supports: 'supports',
  contradicts: 'contradicts',
  supersedes: 'supersedes',
  part_of: 'part of',
  is_a: 'is a',
  depends_on: 'depends on',
  enables: 'enables',
  benefits_from: 'benefits from',
  threatened_by: 'threatened by',
  substitutes_for: 'substitutes for',
  complements: 'complements',
  competes_with: 'competes with',
  partnered_with: 'partnered with',
  supplies: 'supplies',
  customer_of: 'customer of',
  exposed_to: 'exposed to',
  upstream_of: 'upstream of',
  downstream_of: 'downstream of',
  develops: 'develops',
  uses_technology: 'uses technology',
  targets: 'targets',
  treats: 'treats',
  measured_by: 'measured by',
  benchmarked_against: 'benchmarked against',
  reported_in: 'reported in',
}

const typeLabels = {
  concept: 'Concepts',
  entity: 'Entities',
  comparison: 'Comparisons',
  query: 'Queries',
  raw: 'Raw sources',
  system: 'System pages',
  tag: 'Tags',
}

const availableRelationshipTypes = computed(() => {
  if (!graph.value) return []
  const types = [...new Set(graph.value.links.map((link) => link.type || link.kind).filter(Boolean))]
  return types.sort((a, b) => (relationshipLabels[a] || a).localeCompare(relationshipLabels[b] || b))
})

const availableTypes = computed(() => {
  if (!graph.value) return []
  const types = [...new Set(graph.value.nodes.map((node) => node.type))]
  return types.sort((a, b) => (typeLabels[a] || a).localeCompare(typeLabels[b] || b))
})

const filtered = computed(() => {
  if (!graph.value) return { nodes: [], links: [] }
  const q = search.value.trim().toLowerCase()
  const nodes = graph.value.nodes.filter((node) => {
    if (node.raw && !includeRaw.value) return false
    if (node.type === 'system' && !includeSystem.value) return false
    if (node.type === 'query' && !includeQueries.value) return false
    if (!activeTypes.value.has(node.type) && node.type !== 'raw' && node.type !== 'system') return false
    if (!q) return true
    return [node.title, node.slug, node.type, node.summary, ...(node.tags || []), ...(node.aliases || [])]
      .join(' ')
      .toLowerCase()
      .includes(q)
  })
  const ids = new Set(nodes.map((node) => node.id))
  const relationshipFilter = activeRelationships.value
  const links = graph.value.links.filter((link) => {
    const source = typeof link.source === 'string' ? link.source : link.source?.id
    const target = typeof link.target === 'string' ? link.target : link.target?.id
    const type = link.type || link.kind
    if (!ids.has(source) || !ids.has(target)) return false
    if (relationshipFilter.size > 0 && !relationshipFilter.has(type)) return false
    return true
  })
  return { nodes, links }
})

function toggleType(type) {
  const next = new Set(activeTypes.value)
  if (next.has(type)) next.delete(type)
  else next.add(type)
  activeTypes.value = next
}

function toggleRelationship(type) {
  const next = new Set(activeRelationships.value)
  if (next.has(type)) next.delete(type)
  else next.add(type)
  activeRelationships.value = next
}

function clearRelationshipFilter() {
  activeRelationships.value = new Set()
}

function nodeRadius(node) {
  const degree = node.degree?.total || 0
  return Math.max(5, Math.min(18, 5 + Math.sqrt(degree) * 2.2))
}

function destroyGraph() {
  if (simulation) {
    simulation.stop()
    simulation = null
  }
  if (svgRef.value && d3) d3.select(svgRef.value).selectAll('*').remove()
}

async function renderGraph() {
  if (!d3 || !svgRef.value || !containerRef.value) return
  destroyGraph()

  const { nodes, links } = filtered.value
  const width = Math.max(containerRef.value.clientWidth || 900, 320)
  const height = Math.max(Math.min(window.innerHeight * 0.7, 760), 520)

  const svg = d3.select(svgRef.value)
    .attr('viewBox', [0, 0, width, height])
    .attr('width', '100%')
    .attr('height', height)
    .attr('role', 'img')
    .attr('aria-label', 'Wiki graph view')

  svg.selectAll('*').remove()

  const root = svg.append('g')
  const zoom = d3.zoom()
    .scaleExtent([0.25, 4])
    .on('zoom', (event) => root.attr('transform', event.transform))
  svg.call(zoom)

  if (nodes.length === 0) {
    svg.append('text')
      .attr('x', width / 2)
      .attr('y', height / 2)
      .attr('text-anchor', 'middle')
      .attr('class', 'wiki-graph-empty')
      .text('No pages match the current filters.')
    return
  }

  const simNodes = nodes.map((node) => ({ ...node }))
  const byId = new Map(simNodes.map((node) => [node.id, node]))
  const simLinks = links
    .filter((link) => {
      const source = typeof link.source === 'string' ? link.source : link.source?.id
      const target = typeof link.target === 'string' ? link.target : link.target?.id
      return byId.has(source) && byId.has(target)
    })
    .map((link) => {
      const source = typeof link.source === 'string' ? link.source : link.source?.id
      const target = typeof link.target === 'string' ? link.target : link.target?.id
      return { ...link, source: byId.get(source), target: byId.get(target) }
    })

  const link = root.append('g')
    .attr('class', 'wiki-graph-links')
    .selectAll('line')
    .data(simLinks)
    .join('line')
    .attr('class', (d) => `wiki-graph-link wiki-graph-link-${d.type || d.kind}`)
    .attr('stroke-width', (d) => 0.8 + ((d.weight || 0.45) * 1.8))
    .attr('stroke-opacity', (d) => Math.max(0.2, Math.min(0.85, d.confidence || 0.55)))

  const node = root.append('g')
    .attr('class', 'wiki-graph-nodes')
    .selectAll('g')
    .data(simNodes)
    .join('g')
    .attr('class', 'wiki-graph-node')
    .style('cursor', 'pointer')
    .call(d3.drag()
      .on('start', dragstarted)
      .on('drag', dragged)
      .on('end', dragended))

  node.append('circle')
    .attr('r', nodeRadius)
    .attr('fill', (d) => typeColors[d.type] || '#cbd5e1')
    .attr('stroke-width', 1.5)

  node.append('title')
    .text((d) => `${d.title}\n${d.type} · ${d.degree?.total || 0} links\n${d.slug}`)

  const labels = root.append('g')
    .attr('class', 'wiki-graph-labels')
    .selectAll('text')
    .data(simNodes.filter((d) => (d.degree?.total || 0) >= 3 || d.type === 'entity'))
    .join('text')
    .attr('class', 'wiki-graph-label')
    .attr('font-size', 11)
    .text((d) => d.title)

  node.on('click', (event, d) => {
    selectedNode.value = d
    event.stopPropagation()
  })

  svg.on('click', () => { selectedNode.value = null })

  simulation = d3.forceSimulation(simNodes)
    .force('link', d3.forceLink(simLinks).id((d) => d.id).distance((d) => (d.type || d.kind) === 'derived_from' ? 115 : 82).strength((d) => Math.max(0.18, Math.min(0.65, d.weight || 0.45))))
    .force('charge', d3.forceManyBody().strength(-260))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius((d) => nodeRadius(d) + 10))
    .on('tick', () => {
      link
        .attr('x1', (d) => d.source.x)
        .attr('y1', (d) => d.source.y)
        .attr('x2', (d) => d.target.x)
        .attr('y2', (d) => d.target.y)

      node.attr('transform', (d) => `translate(${d.x},${d.y})`)
      labels
        .attr('x', (d) => d.x + nodeRadius(d) + 5)
        .attr('y', (d) => d.y + 4)
    })

  function dragstarted(event, d) {
    if (!event.active) simulation.alphaTarget(0.3).restart()
    d.fx = d.x
    d.fy = d.y
  }

  function dragged(event, d) {
    d.fx = event.x
    d.fy = event.y
  }

  function dragended(event, d) {
    if (!event.active) simulation.alphaTarget(0)
    d.fx = null
    d.fy = null
  }
}

async function loadGraph() {
  try {
    loading.value = true
    error.value = ''
    d3 = await import('d3')
    const graphUrl = withBase('/semantic/graph.json')
    const response = await fetch(graphUrl, { cache: 'no-cache' })
    if (!response.ok) throw new Error(`Could not fetch ${graphUrl} (${response.status})`)
    graph.value = await response.json()
    await nextTick()
    await renderGraph()
  } catch (err) {
    error.value = err?.message || String(err)
  } finally {
    loading.value = false
  }
}

watch([filtered, search, includeRaw, includeSystem, includeQueries, activeTypes, activeRelationships], async () => {
  await nextTick()
  await renderGraph()
}, { deep: true })

onMounted(() => {
  loadGraph()
  resizeObserver = new ResizeObserver(() => renderGraph())
  if (containerRef.value) resizeObserver.observe(containerRef.value)
})

onBeforeUnmount(() => {
  destroyGraph()
  if (resizeObserver) resizeObserver.disconnect()
})
</script>

<template>
  <section class="wiki-graph" ref="containerRef">
    <div class="wiki-graph-toolbar">
      <div>
        <strong>Semantic graph</strong>
        <span v-if="graph" class="wiki-graph-counts">
          {{ filtered.nodes.length }} / {{ graph.counts.nodes }} nodes · {{ filtered.links.length }} / {{ graph.counts.links }} relationships · {{ graph.counts.candidates || 0 }} candidates
        </span>
      </div>
      <input v-model="search" class="wiki-graph-search" type="search" placeholder="Search pages, tags, types…" />
    </div>

    <div class="wiki-graph-filters" v-if="graph">
      <button
        v-for="type in availableTypes.filter((type) => type !== 'raw' && type !== 'system')"
        :key="type"
        class="wiki-graph-filter"
        :class="{ active: activeTypes.has(type) }"
        type="button"
        @click="toggleType(type)"
      >
        <span class="wiki-graph-dot" :style="{ background: typeColors[type] || '#cbd5e1' }"></span>
        {{ typeLabels[type] || type }}
      </button>
      <label class="wiki-graph-check"><input v-model="includeRaw" type="checkbox" /> Raw sources</label>
      <label class="wiki-graph-check"><input v-model="includeSystem" type="checkbox" /> System pages</label>
      <label class="wiki-graph-check"><input v-model="includeQueries" type="checkbox" /> Queries</label>
    </div>

    <div class="wiki-graph-filters wiki-graph-relationships" v-if="graph">
      <button
        class="wiki-graph-filter"
        :class="{ active: activeRelationships.size === 0 }"
        type="button"
        @click="clearRelationshipFilter"
      >
        All relationships
      </button>
      <button
        v-for="type in availableRelationshipTypes"
        :key="type"
        class="wiki-graph-filter"
        :class="{ active: activeRelationships.has(type) }"
        type="button"
        @click="toggleRelationship(type)"
      >
        {{ relationshipLabels[type] || type }}
      </button>
    </div>

    <p v-if="loading" class="wiki-graph-status">Loading graph…</p>
    <p v-else-if="error" class="wiki-graph-error">{{ error }}</p>

    <div v-show="!loading && !error" class="wiki-graph-stage">
      <svg ref="svgRef"></svg>
      <aside v-if="selectedNode" class="wiki-graph-card">
        <h3>{{ selectedNode.title }}</h3>
        <p><strong>Type:</strong> {{ selectedNode.type }}</p>
        <p><strong>Links:</strong> {{ selectedNode.degree?.total || 0 }} total · {{ selectedNode.degree?.in || 0 }} in · {{ selectedNode.degree?.out || 0 }} out</p>
        <p v-if="selectedNode.summary"><strong>Summary:</strong> {{ selectedNode.summary }}</p>
        <p v-if="selectedNode.tags?.length"><strong>Tags:</strong> {{ selectedNode.tags.join(', ') }}</p>
        <p v-if="selectedNode.aliases?.length"><strong>Aliases:</strong> {{ selectedNode.aliases.slice(0, 8).join(', ') }}</p>
        <p v-if="selectedNode.confidence"><strong>Confidence:</strong> {{ selectedNode.confidence }}</p>
        <p class="wiki-graph-slug">{{ selectedNode.slug }}</p>
        <a v-if="selectedNode.route" class="wiki-graph-open" :href="withBase(selectedNode.route)">Open page</a>
      </aside>
    </div>
  </section>
</template>
