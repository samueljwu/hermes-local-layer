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
const hoveredNodeId = ref(null)
const cardCollapsed = ref(false)
const graphGesturesEnabled = ref(false)
const svgRef = ref(null)
const containerRef = ref(null)

let d3 = null
let simulation = null
let resizeObserver = null
let resizeHandler = null
let renderFrame = null
let zoomBehavior = null
let currentTransform = null
let pendingFit = true
let latestNodes = []
let latestLinks = []
let latestWidth = 0
let latestHeight = 0
let latestRoot = null
let latestSvg = null
let latestNodeSelection = null
let latestLinkSelection = null
let latestLabelSelection = null
let latestAdjacency = new Map()

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

const relationshipGroups = {
  evidence: {
    label: 'Evidence/source',
    color: '#f59e0b',
    dash: '7 4',
    arrow: true,
    types: ['derived_from', 'reported_in', 'mentions', 'measured_by', 'benchmarked_against'],
  },
  structure: {
    label: 'Structure',
    color: '#60a5fa',
    dash: '',
    arrow: true,
    types: ['is_a', 'part_of'],
  },
  flow: {
    label: 'Dependency/flow',
    color: '#22d3ee',
    dash: '4 3',
    arrow: true,
    types: ['depends_on', 'enables', 'upstream_of', 'downstream_of'],
  },
  argument: {
    label: 'Argument/change',
    color: '#34d399',
    dash: '',
    arrow: true,
    types: ['supports', 'supersedes'],
  },
  conflict: {
    label: 'Risk/conflict',
    color: '#fb7185',
    dash: '3 3',
    arrow: true,
    types: ['contradicts', 'threatened_by'],
  },
  market: {
    label: 'Business/network',
    color: '#a78bfa',
    dash: '',
    arrow: true,
    types: ['supplies', 'customer_of', 'competes_with', 'partnered_with', 'substitutes_for', 'complements', 'exposed_to', 'benefits_from'],
  },
  domain: {
    label: 'Domain/action',
    color: '#f472b6',
    dash: '',
    arrow: true,
    types: ['develops', 'uses_technology', 'targets', 'treats'],
  },
  general: {
    label: 'General',
    color: '#94a3b8',
    dash: '',
    arrow: false,
    types: ['related_to'],
  },
}

const relationshipGroupByType = Object.entries(relationshipGroups).reduce((acc, [group, meta]) => {
  meta.types.forEach((type) => { acc[type] = group })
  return acc
}, {})

function relationshipStyle(type) {
  const group = relationshipGroups[relationshipGroupByType[type]] || relationshipGroups.general
  if (type === 'related_to') return { ...group, color: '#64748b', opacity: 0.24, arrow: false }
  if (type === 'mentions') return { ...group, color: '#818cf8', opacity: 0.24, arrow: false, dash: '2 4' }
  if (type === 'derived_from') return { ...group, color: '#f59e0b', opacity: 0.48, dash: '7 4' }
  if (type === 'is_a') return { ...group, color: '#38bdf8', opacity: 0.38, arrow: true }
  if (type === 'supports') return { ...group, color: '#34d399', opacity: 0.72, arrow: true }
  if (type === 'contradicts') return { ...group, color: '#fb7185', opacity: 0.78, arrow: true, dash: '3 3' }
  return { ...group, opacity: 0.55 }
}

const baseFilteredNodes = computed(() => {
  if (!graph.value) return []
  const q = search.value.trim().toLowerCase()
  return graph.value.nodes.filter((node) => {
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
})

const baseFilteredLinks = computed(() => {
  if (!graph.value) return []
  const ids = new Set(baseFilteredNodes.value.map((node) => node.id))
  return graph.value.links.filter((link) => {
    const source = typeof link.source === 'string' ? link.source : link.source?.id
    const target = typeof link.target === 'string' ? link.target : link.target?.id
    return ids.has(source) && ids.has(target)
  })
})

const relationshipCounts = computed(() => {
  const counts = new Map()
  for (const link of baseFilteredLinks.value) {
    const type = link.type || link.kind || 'related_to'
    counts.set(type, (counts.get(type) || 0) + 1)
  }
  return counts
})

const availableRelationshipTypes = computed(() => {
  if (!graph.value) return []
  const types = [...new Set(graph.value.links.map((link) => link.type || link.kind || 'related_to').filter(Boolean))]
  return types.sort((a, b) => {
    const countDiff = (relationshipCounts.value.get(b) || 0) - (relationshipCounts.value.get(a) || 0)
    if (countDiff !== 0) return countDiff
    return (relationshipLabels[a] || a).localeCompare(relationshipLabels[b] || b)
  })
})

const groupedRelationshipTypes = computed(() => {
  const groups = []
  const used = new Set()
  for (const [key, group] of Object.entries(relationshipGroups)) {
    const types = availableRelationshipTypes.value.filter((type) => {
      if (!group.types.includes(type)) return false
      return (relationshipCounts.value.get(type) || 0) > 0 || activeRelationships.value.has(type)
    })
    if (types.length > 0) {
      types.forEach((type) => used.add(type))
      groups.push({ key, label: group.label, types })
    }
  }
  const other = availableRelationshipTypes.value.filter((type) => {
    if (used.has(type)) return false
    return (relationshipCounts.value.get(type) || 0) > 0 || activeRelationships.value.has(type)
  })
  if (other.length > 0) groups.push({ key: 'other', label: 'Other', types: other })
  return groups
})

const availableTypes = computed(() => {
  if (!graph.value) return []
  const types = [...new Set(graph.value.nodes.map((node) => node.type))]
  return types.sort((a, b) => (typeLabels[a] || a).localeCompare(typeLabels[b] || b))
})

const filtered = computed(() => {
  const relationshipFilter = activeRelationships.value
  const links = baseFilteredLinks.value.filter((link) => {
    const type = link.type || link.kind || 'related_to'
    if (relationshipFilter.size > 0 && !relationshipFilter.has(type)) return false
    return true
  })
  return { nodes: baseFilteredNodes.value, links }
})

const selectedNodeId = computed(() => selectedNode.value?.id || null)
const selectedStillVisible = computed(() => !selectedNode.value || baseFilteredNodes.value.some((node) => node.id === selectedNode.value.id))

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

function resetFilters() {
  search.value = ''
  includeRaw.value = false
  includeSystem.value = false
  includeQueries.value = true
  activeTypes.value = new Set(['concept', 'entity', 'comparison', 'query'])
  activeRelationships.value = new Set()
}

function nodeRadius(node) {
  const degree = node.degree?.total || 0
  return Math.max(5, Math.min(18, 5 + Math.sqrt(degree) * 2.2))
}

function markerId(type) {
  return `wiki-graph-arrow-${(relationshipGroupByType[type] || 'general').replace(/[^a-z0-9_-]/gi, '-')}`
}

function destroyGraph() {
  if (simulation) {
    simulation.stop()
    simulation = null
  }
  latestNodes = []
  latestLinks = []
  latestRoot = null
  latestSvg = null
  latestNodeSelection = null
  latestLinkSelection = null
  latestLabelSelection = null
  latestAdjacency = new Map()
  if (svgRef.value && d3) d3.select(svgRef.value).selectAll('*').remove()
}

function scheduleRender(options = {}) {
  if (options.fit) pendingFit = true
  if (renderFrame) return
  renderFrame = window.requestAnimationFrame(() => {
    renderFrame = null
    renderGraph()
  })
}

function graphDimensions() {
  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 1024
  const fallbackHeight = Math.round(Math.min(Math.max((window.innerHeight || 768) * 0.74, 560), 960))
  const width = Math.max(containerRef.value?.clientWidth || viewportWidth * 0.92, 320)
  const height = Math.max(svgRef.value?.clientHeight || fallbackHeight, 360)
  return { width, height }
}

function updatePositions() {
  if (!latestNodeSelection || !latestLinkSelection || !latestLabelSelection) return
  latestLinkSelection
    .attr('x1', (d) => d.source.x)
    .attr('y1', (d) => d.source.y)
    .attr('x2', (d) => d.target.x)
    .attr('y2', (d) => d.target.y)

  latestNodeSelection.attr('transform', (d) => `translate(${d.x},${d.y})`)
  latestLabelSelection
    .attr('x', (d) => d.x + nodeRadius(d) + 5)
    .attr('y', (d) => d.y + 4)
}

function buildAdjacency(links) {
  const adjacency = new Map()
  for (const link of links) {
    const source = typeof link.source === 'string' ? link.source : link.source?.id
    const target = typeof link.target === 'string' ? link.target : link.target?.id
    if (!source || !target) continue
    if (!adjacency.has(source)) adjacency.set(source, new Set())
    if (!adjacency.has(target)) adjacency.set(target, new Set())
    adjacency.get(source).add(target)
    adjacency.get(target).add(source)
  }
  return adjacency
}

function updateHighlights() {
  if (!latestNodeSelection || !latestLinkSelection || !latestLabelSelection) return
  const focusId = hoveredNodeId.value || selectedNodeId.value
  if (!focusId) {
    latestNodeSelection.classed('is-dimmed', false).classed('is-highlighted', false).classed('is-selected', false)
    latestLabelSelection.classed('is-dimmed', false).classed('is-highlighted', false)
    latestLinkSelection.classed('is-dimmed', false).classed('is-highlighted', false)
    return
  }
  const neighbors = latestAdjacency.get(focusId) || new Set()
  latestNodeSelection
    .classed('is-selected', (d) => d.id === selectedNodeId.value)
    .classed('is-highlighted', (d) => d.id === focusId || neighbors.has(d.id))
    .classed('is-dimmed', (d) => d.id !== focusId && !neighbors.has(d.id))
  latestLabelSelection
    .classed('is-highlighted', (d) => d.id === focusId || neighbors.has(d.id))
    .classed('is-dimmed', (d) => d.id !== focusId && !neighbors.has(d.id))
  latestLinkSelection
    .classed('is-highlighted', (d) => d.source.id === focusId || d.target.id === focusId)
    .classed('is-dimmed', (d) => d.source.id !== focusId && d.target.id !== focusId)
}

function computeBounds(nodes) {
  const finite = nodes.filter((node) => Number.isFinite(node.x) && Number.isFinite(node.y))
  if (finite.length === 0) return null
  let minX = Infinity
  let maxX = -Infinity
  let minY = Infinity
  let maxY = -Infinity
  for (const node of finite) {
    const r = nodeRadius(node) + 18
    minX = Math.min(minX, node.x - r)
    maxX = Math.max(maxX, node.x + r)
    minY = Math.min(minY, node.y - r)
    maxY = Math.max(maxY, node.y + r)
  }
  return { minX, maxX, minY, maxY, width: maxX - minX, height: maxY - minY }
}

function transformForBounds(bounds, width, height, padding = 56) {
  if (!d3?.zoomIdentity || !bounds) return null
  if (bounds.width <= 0 || bounds.height <= 0) {
    return d3.zoomIdentity.translate(width / 2 - bounds.minX, height / 2 - bounds.minY)
  }
  const scale = Math.max(0.25, Math.min(4, Math.min((width - padding * 2) / bounds.width, (height - padding * 2) / bounds.height)))
  const tx = width / 2 - scale * (bounds.minX + bounds.width / 2)
  const ty = height / 2 - scale * (bounds.minY + bounds.height / 2)
  return d3.zoomIdentity.translate(tx, ty).scale(scale)
}

function applyTransform(transform) {
  if (!latestSvg || !zoomBehavior || !transform) return
  latestSvg.call(zoomBehavior.transform, transform)
}

function fitGraph() {
  const transform = transformForBounds(computeBounds(latestNodes), latestWidth, latestHeight)
  if (transform) applyTransform(transform)
}

function resetZoom() {
  if (!d3?.zoomIdentity) return
  applyTransform(d3.zoomIdentity)
}

function zoomIn() {
  if (latestSvg && zoomBehavior) latestSvg.call(zoomBehavior.scaleBy, 1.25)
}

function zoomOut() {
  if (latestSvg && zoomBehavior) latestSvg.call(zoomBehavior.scaleBy, 0.8)
}

function graphGestureAllowed(event) {
  if (event.button && event.button !== 0) return false
  if (event.target?.closest?.('.wiki-graph-node')) return false
  if (event.type === 'wheel') return true
  if (event.type?.startsWith?.('touch')) return graphGesturesEnabled.value
  if (event.pointerType === 'touch') return graphGesturesEnabled.value
  if (event.type === 'mousedown' || event.type === 'pointerdown') return true
  return true
}

function nodeDragAllowed(event) {
  if (event.button && event.button !== 0) return false
  if (event.type?.startsWith?.('touch')) return graphGesturesEnabled.value
  if (event.pointerType === 'touch') return graphGesturesEnabled.value
  return true
}

function toggleGraphGestures() {
  graphGesturesEnabled.value = !graphGesturesEnabled.value
}

async function renderGraph() {
  if (!d3 || !svgRef.value || !containerRef.value) return
  destroyGraph()

  const { nodes, links } = filtered.value
  const { width, height } = graphDimensions()
  latestWidth = width
  latestHeight = height

  const svg = d3.select(svgRef.value)
    .attr('viewBox', [0, 0, width, height])
    .attr('width', '100%')
    .attr('height', height)
    .attr('role', 'img')
    .attr('aria-label', 'Wiki semantic graph. Scroll over the graph to zoom, drag empty space to pan, and drag nodes to reposition them. On touch screens, enable graph controls before panning the graph.')

  latestSvg = svg
  svg.selectAll('*').remove()

  const defs = svg.append('defs')
  for (const [key, group] of Object.entries(relationshipGroups)) {
    defs.append('marker')
      .attr('id', `wiki-graph-arrow-${key}`)
      .attr('viewBox', '0 -5 10 10')
      .attr('refX', 16)
      .attr('refY', 0)
      .attr('markerWidth', 5)
      .attr('markerHeight', 5)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M0,-4L8,0L0,4')
      .attr('fill', group.color)
      .attr('opacity', 0.72)
  }

  svg.append('rect')
    .attr('class', 'wiki-graph-zoom-capture')
    .attr('width', width)
    .attr('height', height)
    .attr('fill', 'transparent')
    .attr('pointer-events', 'all')
    .on('click', () => { selectedNode.value = null })

  const root = svg.append('g').attr('class', 'wiki-graph-root')
  latestRoot = root

  zoomBehavior = d3.zoom()
    .scaleExtent([0.25, 4])
    .filter(graphGestureAllowed)
    .on('start', () => svg.classed('wiki-graph-dragging', true))
    .on('zoom', (event) => {
      currentTransform = event.transform
      root.attr('transform', event.transform)
    })
    .on('end', () => svg.classed('wiki-graph-dragging', false))

  svg.call(zoomBehavior)
  svg.on('dblclick.zoom', null)

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
      return { ...link, type: link.type || link.kind || 'related_to', source: byId.get(source), target: byId.get(target) }
    })

  latestNodes = simNodes
  latestLinks = simLinks
  latestAdjacency = buildAdjacency(simLinks)

  const link = root.append('g')
    .attr('class', 'wiki-graph-links')
    .selectAll('line')
    .data(simLinks)
    .join('line')
    .attr('class', (d) => `wiki-graph-link wiki-graph-link-${d.type}`)
    .attr('stroke', (d) => relationshipStyle(d.type).color)
    .attr('stroke-dasharray', (d) => relationshipStyle(d.type).dash || null)
    .attr('marker-end', (d) => relationshipStyle(d.type).arrow ? `url(#${markerId(d.type)})` : null)
    .attr('stroke-width', (d) => 0.8 + ((d.weight || 0.45) * 1.8))
    .attr('stroke-opacity', (d) => Math.max(0.16, Math.min(0.82, relationshipStyle(d.type).opacity ?? d.confidence ?? 0.55)))

  link.append('title')
    .text((d) => `${d.source.title} — ${relationshipLabels[d.type] || d.type} → ${d.target.title}`)

  const node = root.append('g')
    .attr('class', 'wiki-graph-nodes')
    .selectAll('g')
    .data(simNodes)
    .join('g')
    .attr('class', 'wiki-graph-node')
    .style('cursor', 'pointer')
    .on('mouseenter', (_event, d) => { hoveredNodeId.value = d.id })
    .on('mouseleave', () => { hoveredNodeId.value = null })
    .call(d3.drag()
      .filter(nodeDragAllowed)
      .on('start', dragstarted)
      .on('drag', dragged)
      .on('end', dragended))

  node.append('circle')
    .attr('class', 'wiki-graph-hit')
    .attr('r', (d) => Math.max(20, nodeRadius(d) + 10))

  node.append('circle')
    .attr('class', 'wiki-graph-node-dot')
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
    cardCollapsed.value = false
    event.stopPropagation()
  })

  latestNodeSelection = node
  latestLinkSelection = link
  latestLabelSelection = labels

  simulation = d3.forceSimulation(simNodes)
    .force('link', d3.forceLink(simLinks).id((d) => d.id).distance((d) => (d.type || d.kind) === 'derived_from' ? 115 : 82).strength((d) => Math.max(0.16, Math.min(0.58, d.weight || 0.45))))
    .force('charge', d3.forceManyBody().strength(-230))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('x', d3.forceX(width / 2).strength(0.035))
    .force('y', d3.forceY(height / 2).strength(0.035))
    .force('collision', d3.forceCollide().radius((d) => nodeRadius(d) + 10))
    .stop()

  simulation.tick(90)
  updatePositions()

  if (pendingFit || !currentTransform) {
    pendingFit = false
    fitGraph()
  } else {
    applyTransform(currentTransform)
  }

  updateHighlights()

  simulation
    .alpha(0.18)
    .on('tick', () => {
      updatePositions()
      updateHighlights()
    })
    .restart()

  function dragstarted(event, d) {
    event.sourceEvent?.stopPropagation()
    if (!event.active && simulation) simulation.alphaTarget(0.25).restart()
    d.fx = d.x
    d.fy = d.y
  }

  function dragged(event, d) {
    event.sourceEvent?.stopPropagation()
    d.fx = event.x
    d.fy = event.y
  }

  function dragended(event, d) {
    event.sourceEvent?.stopPropagation()
    if (!event.active && simulation) simulation.alphaTarget(0)
    d.fx = null
    d.fy = null
  }
}

async function loadGraph() {
  try {
    loading.value = true
    error.value = ''
    const graphUrl = withBase('/semantic/graph-view.json')
    const [selection, force, zoomModule, dragModule, response] = await Promise.all([
      import('d3-selection'),
      import('d3-force'),
      import('d3-zoom'),
      import('d3-drag'),
      fetch(graphUrl),
    ])
    d3 = {
      select: selection.select,
      forceSimulation: force.forceSimulation,
      forceLink: force.forceLink,
      forceManyBody: force.forceManyBody,
      forceCenter: force.forceCenter,
      forceCollide: force.forceCollide,
      forceX: force.forceX,
      forceY: force.forceY,
      zoom: zoomModule.zoom,
      zoomIdentity: zoomModule.zoomIdentity,
      drag: dragModule.drag,
    }
    currentTransform = d3.zoomIdentity
    if (!response.ok) throw new Error(`Could not fetch ${graphUrl} (${response.status})`)
    graph.value = await response.json()
    await nextTick()
    pendingFit = true
    await renderGraph()
  } catch (err) {
    error.value = err?.message || String(err)
  } finally {
    loading.value = false
  }
}

watch([filtered, search, includeRaw, includeSystem, includeQueries, activeTypes, activeRelationships], async () => {
  await nextTick()
  if (!selectedStillVisible.value) selectedNode.value = null
  scheduleRender({ fit: true })
}, { deep: true })

watch([hoveredNodeId, selectedNodeId], () => updateHighlights())

onMounted(() => {
  loadGraph()
  resizeObserver = new ResizeObserver(() => scheduleRender())
  if (containerRef.value) resizeObserver.observe(containerRef.value)
  resizeHandler = () => scheduleRender()
  window.addEventListener('resize', resizeHandler)
})

onBeforeUnmount(() => {
  if (renderFrame) window.cancelAnimationFrame(renderFrame)
  destroyGraph()
  if (resizeObserver) resizeObserver.disconnect()
  if (resizeHandler) window.removeEventListener('resize', resizeHandler)
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
      <div class="wiki-graph-toolbar-actions">
        <input v-model="search" class="wiki-graph-search" type="search" placeholder="Search pages, tags, types…" />
        <div class="wiki-graph-controls" aria-label="Graph navigation controls">
          <button class="wiki-graph-control wiki-graph-interaction-toggle" type="button" :class="{ active: graphGesturesEnabled }" :aria-pressed="graphGesturesEnabled" @click="toggleGraphGestures">
            {{ graphGesturesEnabled ? 'Touch controls on' : 'Enable touch controls' }}
          </button>
          <button class="wiki-graph-control" type="button" :disabled="loading || error || filtered.nodes.length === 0" @click="zoomOut" aria-label="Zoom out">−</button>
          <button class="wiki-graph-control" type="button" :disabled="loading || error || filtered.nodes.length === 0" @click="zoomIn" aria-label="Zoom in">+</button>
          <button class="wiki-graph-control" type="button" :disabled="loading || error || filtered.nodes.length === 0" @click="fitGraph">Fit</button>
          <button class="wiki-graph-control" type="button" :disabled="loading || error || filtered.nodes.length === 0" @click="resetZoom">Reset</button>
        </div>
      </div>
    </div>

    <div class="wiki-graph-filters" v-if="graph">
      <button
        v-for="type in availableTypes.filter((type) => type !== 'raw' && type !== 'system')"
        :key="type"
        class="wiki-graph-filter"
        :class="{ active: activeTypes.has(type) }"
        type="button"
        :aria-pressed="activeTypes.has(type)"
        @click="toggleType(type)"
      >
        <span class="wiki-graph-dot" :style="{ background: typeColors[type] || '#cbd5e1' }"></span>
        {{ typeLabels[type] || type }}
      </button>
      <label class="wiki-graph-check"><input v-model="includeRaw" type="checkbox" /> Raw sources</label>
      <label class="wiki-graph-check"><input v-model="includeSystem" type="checkbox" /> System pages</label>
      <label class="wiki-graph-check"><input v-model="includeQueries" type="checkbox" /> Queries</label>
    </div>

    <div class="wiki-graph-relationships" v-if="graph">
      <div class="wiki-graph-relationship-summary">
        <strong>Relationships</strong>
        <button
          class="wiki-graph-filter wiki-graph-all-relationships"
          :class="{ active: activeRelationships.size === 0 }"
          type="button"
          @click="clearRelationshipFilter"
        >
          All {{ baseFilteredLinks.length }}
        </button>
      </div>
      <div v-for="group in groupedRelationshipTypes" :key="group.key" class="wiki-graph-legend-group">
        <span class="wiki-graph-legend-title">{{ group.label }}</span>
        <button
          v-for="type in group.types"
          :key="type"
          class="wiki-graph-filter wiki-graph-relationship-filter"
          :class="{ active: activeRelationships.has(type) }"
          type="button"
          :aria-pressed="activeRelationships.has(type)"
          @click="toggleRelationship(type)"
        >
          <span
            class="wiki-graph-swatch"
            :style="{ borderColor: relationshipStyle(type).color, borderTopStyle: relationshipStyle(type).dash ? 'dashed' : 'solid' }"
          ></span>
          <span>{{ relationshipLabels[type] || type }}</span>
          <span class="wiki-graph-filter-count">{{ relationshipCounts.get(type) || 0 }}</span>
        </button>
      </div>
    </div>

    <p v-if="loading" class="wiki-graph-status">Loading graph…</p>
    <p v-else-if="error" class="wiki-graph-error">{{ error }}</p>

    <div v-show="!loading && !error" class="wiki-graph-layout">
      <div class="wiki-graph-stage" :class="{ 'gestures-enabled': graphGesturesEnabled }">
        <div class="wiki-graph-interaction-hint">
          <span class="wiki-graph-desktop-hint">Scroll over the graph to zoom. Drag empty space to pan; drag nodes to reposition them.</span>
          <span class="wiki-graph-touch-hint">Page scroll stays enabled. Tap “Enable graph controls” to pan or drag nodes, then turn it off to scroll normally.</span>
        </div>
        <svg ref="svgRef"></svg>
        <div v-if="filtered.nodes.length === 0" class="wiki-graph-empty-panel">
          <strong>No pages match the current filters.</strong>
          <button type="button" class="wiki-graph-control" @click="resetFilters">Reset filters</button>
        </div>
      </div>
      <aside v-if="selectedNode" class="wiki-graph-card" :class="{ collapsed: cardCollapsed }">
        <div class="wiki-graph-card-header">
          <h3>{{ selectedNode.title }}</h3>
          <div class="wiki-graph-card-actions">
            <button type="button" @click="cardCollapsed = !cardCollapsed">{{ cardCollapsed ? 'Expand' : 'Collapse' }}</button>
            <button type="button" aria-label="Close selected node" @click="selectedNode = null">×</button>
          </div>
        </div>
        <p><strong>Type:</strong> {{ selectedNode.type }}</p>
        <p><strong>Links:</strong> {{ selectedNode.degree?.total || 0 }} total · {{ selectedNode.degree?.in || 0 }} in · {{ selectedNode.degree?.out || 0 }} out</p>
        <template v-if="!cardCollapsed">
          <p v-if="selectedNode.summary"><strong>Summary:</strong> {{ selectedNode.summary }}</p>
          <p v-if="selectedNode.tags?.length"><strong>Tags:</strong> {{ selectedNode.tags.join(', ') }}</p>
          <p v-if="selectedNode.aliases?.length"><strong>Aliases:</strong> {{ selectedNode.aliases.slice(0, 8).join(', ') }}</p>
          <p v-if="selectedNode.confidence"><strong>Confidence:</strong> {{ selectedNode.confidence }}</p>
          <p class="wiki-graph-slug">{{ selectedNode.slug }}</p>
        </template>
        <a v-if="selectedNode.route" class="wiki-graph-open" :href="withBase(selectedNode.route)">Open page</a>
      </aside>
    </div>
  </section>
</template>
