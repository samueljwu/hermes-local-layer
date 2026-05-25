#!/usr/bin/env node
// Regression test: identical wiki source inputs should not churn semantic graph
// edge/page timestamps or reorder generated graph/state content between runs.

import { execFileSync } from 'child_process'
import { readFileSync } from 'fs'
import { join } from 'path'

const ROOT = process.cwd()

function runGenerator() {
  execFileSync('node', ['.vitepress/gen-semantic-graph.mjs'], {
    cwd: ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
}

function readJson(relativePath) {
  return JSON.parse(readFileSync(join(ROOT, relativePath), 'utf8'))
}

function withoutTopLevelGeneratedAt(value) {
  const copy = JSON.parse(JSON.stringify(value))
  delete copy.generatedAt
  return copy
}

runGenerator()
const graph1 = withoutTopLevelGeneratedAt(readJson('src/_meta/semantic/graph.json'))
const state1 = withoutTopLevelGeneratedAt(readJson('src/_meta/semantic/state.json'))
const candidates1 = readJson('src/_meta/semantic/candidates.json')

runGenerator()
const graph2 = withoutTopLevelGeneratedAt(readJson('src/_meta/semantic/graph.json'))
const state2 = withoutTopLevelGeneratedAt(readJson('src/_meta/semantic/state.json'))
const candidates2 = readJson('src/_meta/semantic/candidates.json')

const pairs = [
  ['graph', graph1, graph2],
  ['state', state1, state2],
  ['candidates', candidates1, candidates2],
]

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

for (const [name, a, b] of pairs) {
  if (stableJson(a) !== stableJson(b)) {
    console.error(`[semantic-stability] ${name} changed across identical generator runs`)
    process.exit(1)
  }
}

console.log('[semantic-stability] graph/state/candidates are stable across identical generator runs')
