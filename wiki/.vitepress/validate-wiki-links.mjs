#!/usr/bin/env node
// @ts-nocheck
/**
 * Wiki Link Validator — runs as `npm run prebuild`
 *
 * Scans all .md files in src/ for [[wiki-links]], checks each target against
 * discovered page slugs, and exits with an error if any link is broken.
 *
 * Broken links print:
 *   [WIKI-LINK ERROR] src/concepts/foo.md:3 → [[bar]] — "bar" does not exist
 *
 * Exit code 1 if errors found, 0 if clean.
 */

import { readdirSync, statSync, readFileSync } from 'fs'
import { resolve, relative, join } from 'path'
import { fileURLToPath } from 'url'
import { dirname } from 'path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const WIKI_SRC = resolve(__dirname, '../src')

// ---- Discover all page slugs ----
function discoverSlugs(dir) {
  const slugs = new Set()
  const fullSlugs = new Set()
  const shortToFull = new Map()
  for (const entry of readdirSync(dir)) {
    const fullPath = join(dir, entry)
    const stat = statSync(fullPath)
    if (stat.isDirectory()) {
      const child = discoverSlugs(fullPath)
      for (const slug of child.slugs) slugs.add(slug)
      for (const slug of child.fullSlugs) fullSlugs.add(slug)
      for (const [short, targets] of child.shortToFull.entries()) {
        if (!shortToFull.has(short)) shortToFull.set(short, new Set())
        for (const target of targets) shortToFull.get(short).add(target)
      }
    } else if (entry.endsWith('.md')) {
      const rel = relative(WIKI_SRC, fullPath)
      const slug = rel.slice(0, -3) // strip .md
      slugs.add(slug)
      fullSlugs.add(slug)
      // Also add short names (last path segment) for disambiguation
      const short = slug.split('/').pop()
      if (short) {
        if (!shortToFull.has(short)) shortToFull.set(short, new Set())
        shortToFull.get(short).add(slug)
        slugs.add(short)
      }
    }
  }
  return { slugs, fullSlugs, shortToFull }
}

// ---- Scan a file for wiki links ----
function extractWikiLinks(src) {
  const links = []
  // Match [[slug]] and [[slug#section]] but NOT [[slug|display]] (those are handled differently)
  const re = /\[\[([^\]|]+)(?:\|[^\]]+)?\]\]/g
  let m
  while ((m = re.exec(src)) !== null) {
    const inner = m[1].trim()
    const slug = inner.split('#')[0].trim()
    if (slug) links.push(slug)
  }
  return links
}

// ---- Main ----
const { slugs: allSlugs, fullSlugs, shortToFull } = discoverSlugs(WIKI_SRC)
const errors = []

function scanDir(dir) {
  for (const entry of readdirSync(dir)) {
    const fullPath = join(dir, entry)
    const stat = statSync(fullPath)
    if (stat.isDirectory()) {
      scanDir(fullPath)
    } else if (entry.endsWith('.md')) {
      const src = readFileSync(fullPath, 'utf8')
      const relPath = relative(WIKI_SRC, fullPath)
      const wikiLinks = extractWikiLinks(src)
      for (const link of wikiLinks) {
        const shortTargets = shortToFull.get(link)
        if (!link.includes('/') && shortTargets && shortTargets.size > 1 && !fullSlugs.has(link)) {
          errors.push({ file: relPath, link, reason: `ambiguous short link; use one of: ${[...shortTargets].sort().map((s) => `[[${s}]]`).join(', ')}` })
          continue
        }
        // Check both the full slug and its short form
        const exists = allSlugs.has(link) ||
          allSlugs.has(link.split('/').pop())
        if (!exists) {
          errors.push({ file: relPath, link, reason: `"${link}" does not exist` })
        }
      }
    }
  }
}

scanDir(WIKI_SRC)

if (errors.length > 0) {
  console.error('\n[WIKI-LINK VALIDATION FAILED]\n')
  for (const { file, link, reason } of errors) {
    console.error(`  [WIKI-LINK ERROR] ${file} → [[${link}]] — ${reason}`)
  }
  console.error(`\n  ${errors.length} broken link(s) found.\n`)
  console.error('  Add the missing page or fix the link.\n')
  process.exit(1)
} else {
  console.log(`[wiki-link-validator] All clean — ${allSlugs.size} pages checked.`)
  process.exit(0)
}
