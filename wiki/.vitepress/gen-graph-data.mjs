#!/usr/bin/env node
// @ts-nocheck
/**
 * Wiki graph data generator.
 *
 * Scans all markdown files under src/ and emits public/wiki-graph.json for the VitePress graph page.
 * Edges are intentionally source-grounded:
 *   - explicit Obsidian-style wikilinks: [[slug]] / [[slug|text]] / [[slug#section]]
 *   - frontmatter sources: sources: [raw/...]
 */

import { readdirSync, statSync, readFileSync, mkdirSync, writeFileSync } from 'fs'
import { resolve, relative, join, basename } from 'path'

const ROOT = process.cwd()
const SRC = join(ROOT, 'src')
const OUT_DIR = join(ROOT, 'public')
const OUT_FILE = join(OUT_DIR, 'wiki-graph.json')

const SKIP_DIRS = new Set(['.vitepress', 'node_modules', 'dist', '_archive', '_meta'])

function walkMdFiles(dir) {
  const files = []
  for (const entry of readdirSync(dir).sort()) {
    if (entry.startsWith('.') || SKIP_DIRS.has(entry)) continue
    const fullPath = join(dir, entry)
    const stat = statSync(fullPath)
    if (stat.isDirectory()) files.push(...walkMdFiles(fullPath))
    else if (stat.isFile() && entry.endsWith('.md')) files.push(fullPath)
  }
  return files
}

function slugForFile(filePath) {
  return relative(SRC, filePath).replace(/\\/g, '/').replace(/\.md$/, '')
}

function routeForSlug(slug) {
  if (slug === 'index') return '/'
  return `/${slug}`
}

function titleFromSlug(slug) {
  const last = slug.split('/').pop() || slug
  if (last === 'SCHEMA') return 'Schema'
  return last
    .replace(/-/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

function parseFrontmatter(src) {
  if (!src.startsWith('---\n')) return { data: {}, body: src }
  const end = src.indexOf('\n---', 4)
  if (end === -1) return { data: {}, body: src }
  const raw = src.slice(4, end).trim()
  const body = src.slice(end + 4)
  const data = {}

  for (const line of raw.split('\n')) {
    const match = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/)
    if (!match) continue
    const key = match[1]
    let value = match[2].trim()
    if (!value) {
      data[key] = ''
      continue
    }
    value = value.replace(/^['"]|['"]$/g, '')
    if (value.startsWith('[') && value.endsWith(']')) {
      const inner = value.slice(1, -1).trim()
      data[key] = inner
        ? inner.split(',').map((v) => v.trim().replace(/^['"]|['"]$/g, '')).filter(Boolean)
        : []
    } else if (value === 'true') {
      data[key] = true
    } else if (value === 'false') {
      data[key] = false
    } else {
      data[key] = value
    }
  }

  return { data, body }
}

function normalizeSourcePath(source) {
  if (!source || typeof source !== 'string') return null
  return source
    .replace(/^src\//, '')
    .replace(/^\.\//, '')
    .replace(/\.md$/, '')
}

function extractWikiLinks(src) {
  const links = []
  const re = /\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]/g
  let match
  while ((match = re.exec(src)) !== null) {
    const slug = match[1].trim()
    if (slug) links.push(slug)
  }
  return links
}

function buildSlugIndexes(slugs) {
  const fullSlugs = new Set(slugs)
  const shortToFull = new Map()
  for (const slug of slugs) {
    const short = slug.split('/').pop()
    if (!short) continue
    if (!shortToFull.has(short)) shortToFull.set(short, [])
    shortToFull.get(short).push(slug)
  }
  for (const targets of shortToFull.values()) targets.sort()
  return { fullSlugs, shortToFull }
}

function resolveTarget(rawTarget, indexes) {
  if (!rawTarget) return null
  const target = rawTarget.replace(/\.md$/, '').trim()
  if (indexes.fullSlugs.has(target)) return target
  if (!target.includes('/')) {
    const matches = indexes.shortToFull.get(target) || []
    if (matches.length === 1) return matches[0]
  }
  return null
}

const files = walkMdFiles(SRC)
const pages = files.map((filePath) => {
  const slug = slugForFile(filePath)
  const src = readFileSync(filePath, 'utf8')
  const { data, body } = parseFrontmatter(src)
  const section = slug.includes('/') ? slug.split('/')[0] : 'home'
  const type = data.type || (section === 'raw' ? 'raw' : section === 'queries' ? 'query' : section === 'comparisons' ? 'comparison' : section === 'entities' ? 'entity' : section === 'concepts' ? 'concept' : 'system')
  return {
    id: slug,
    slug,
    title: data.title || titleFromSlug(slug),
    route: routeForSlug(slug),
    section,
    type,
    tags: Array.isArray(data.tags) ? data.tags : [],
    confidence: data.confidence || null,
    path: relative(ROOT, filePath).replace(/\\/g, '/'),
    raw: section === 'raw',
    sourceCount: Array.isArray(data.sources) ? data.sources.length : 0,
    _body: body,
    _sources: Array.isArray(data.sources) ? data.sources : [],
  }
})

const indexes = buildSlugIndexes(pages.map((page) => page.slug))
const nodeIds = new Set(pages.map((page) => page.slug))
const edgeMap = new Map()
const unresolved = []

function addEdge(source, target, kind) {
  if (!source || !target || source === target || !nodeIds.has(target)) return
  const id = `${source}→${target}→${kind}`
  if (!edgeMap.has(id)) edgeMap.set(id, { source, target, kind })
}

for (const page of pages) {
  for (const rawTarget of extractWikiLinks(page._body)) {
    const target = resolveTarget(rawTarget, indexes)
    if (target) addEdge(page.slug, target, 'wikilink')
    else unresolved.push({ source: page.slug, target: rawTarget, kind: 'wikilink' })
  }
  for (const sourcePath of page._sources) {
    const target = resolveTarget(normalizeSourcePath(sourcePath), indexes)
    if (target) addEdge(page.slug, target, 'source')
    else unresolved.push({ source: page.slug, target: sourcePath, kind: 'source' })
  }
}

const nodes = pages.map(({ _body, _sources, ...page }) => page)
const links = [...edgeMap.values()]

const degree = new Map(nodes.map((node) => [node.id, { in: 0, out: 0, total: 0 }]))
for (const edge of links) {
  const src = degree.get(edge.source)
  const dst = degree.get(edge.target)
  if (src) { src.out += 1; src.total += 1 }
  if (dst) { dst.in += 1; dst.total += 1 }
}
for (const node of nodes) node.degree = degree.get(node.id) || { in: 0, out: 0, total: 0 }

const graph = {
  generatedAt: new Date().toISOString(),
  counts: {
    nodes: nodes.length,
    links: links.length,
    unresolved: unresolved.length,
  },
  nodes,
  links,
  unresolved,
}

mkdirSync(OUT_DIR, { recursive: true })
writeFileSync(OUT_FILE, `${JSON.stringify(graph, null, 2)}\n`)
console.log(`[gen-graph-data] Generated ${relative(ROOT, OUT_FILE)} with ${nodes.length} nodes, ${links.length} links, ${unresolved.length} unresolved references`)
