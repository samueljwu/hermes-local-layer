export const endpointId = (endpoint) => typeof endpoint === 'string' ? endpoint : endpoint?.id

export function wheelZoomDelta(event) {
  const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 320 : 1)
  return -Math.sign(pixels) * Math.min(Math.abs(pixels), 120) * 0.002
}

export function matchesNode(node, query) {
  const q = query.trim().toLowerCase()
  return !q || [node.title, node.slug, node.type, node.summary, ...(node.tags || []), ...(node.aliases || [])]
    .join(' ').toLowerCase().includes(q)
}

export function neighborhoodView(nodes, links, focusId) {
  if (!focusId) return { nodes, links }
  const allowed = new Set(nodes.map((node) => node.id))
  if (!allowed.has(focusId)) return { nodes: [], links: [] }
  const ids = new Set([focusId])
  for (const link of links) {
    const source = endpointId(link.source)
    const target = endpointId(link.target)
    if (!allowed.has(source) || !allowed.has(target)) continue
    if (source === focusId) ids.add(target)
    if (target === focusId) ids.add(source)
  }
  return {
    nodes: nodes.filter((node) => ids.has(node.id)),
    links: links.filter((link) => ids.has(endpointId(link.source)) && ids.has(endpointId(link.target))),
  }
}

export function nodeConnections(nodes, links, selectedId) {
  if (!selectedId) return []
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const rows = []
  for (const [index, link] of links.entries()) {
    const source = endpointId(link.source)
    const target = endpointId(link.target)
    if (source !== selectedId && target !== selectedId) continue
    const outgoing = source === selectedId
    const node = byId.get(outgoing ? target : source)
    if (!node) continue
    rows.push({ key: `${index}:${source}:${target}`, node, link, direction: outgoing ? 'outgoing' : 'incoming', type: link.type || link.kind || 'related_to' })
  }
  return rows.sort((a, b) => a.node.title.localeCompare(b.node.title) || a.type.localeCompare(b.type))
}
