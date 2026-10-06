import katex from 'katex'

// Lightweight Markdown-it math plugin using the top-level modern KaTeX package.
// This replaces markdown-it-katex's bundled old KaTeX, which cannot render \tag{...}
// and silently emits raw LaTeX for tagged display equations.

function isValidDelim(state, pos) {
  const max = state.posMax
  const prevChar = pos > 0 ? state.src.charCodeAt(pos - 1) : -1
  const nextChar = pos + 1 <= max ? state.src.charCodeAt(pos + 1) : -1
  let canOpen = true
  let canClose = true

  if (prevChar === 0x20 || prevChar === 0x09 || (nextChar >= 0x30 && nextChar <= 0x39)) {
    canClose = false
  }
  if (nextChar === 0x20 || nextChar === 0x09) {
    canOpen = false
  }

  return { canOpen, canClose }
}

function mathInline(state, silent) {
  if (state.src[state.pos] !== '$') return false
  if (state.src[state.pos + 1] === '$') return false

  const open = isValidDelim(state, state.pos)
  if (!open.canOpen) {
    if (!silent) state.pending += '$'
    state.pos += 1
    return true
  }

  const start = state.pos + 1
  let match = start
  while ((match = state.src.indexOf('$', match)) !== -1) {
    let pos = match - 1
    while (state.src[pos] === '\\') pos -= 1
    if ((match - pos) % 2 === 1) break
    match += 1
  }

  if (match === -1) {
    if (!silent) state.pending += '$'
    state.pos = start
    return true
  }

  if (match - start === 0) {
    if (!silent) state.pending += '$$'
    state.pos = start + 1
    return true
  }

  const close = isValidDelim(state, match)
  if (!close.canClose) {
    if (!silent) state.pending += '$'
    state.pos = start
    return true
  }

  if (!silent) {
    const token = state.push('math_inline', 'math', 0)
    token.markup = '$'
    token.content = state.src.slice(start, match)
  }

  state.pos = match + 1
  return true
}

function mathBlock(state, start, end, silent) {
  let pos = state.bMarks[start] + state.tShift[start]
  let max = state.eMarks[start]

  if (pos + 2 > max) return false
  if (state.src.slice(pos, pos + 2) !== '$$') return false

  pos += 2
  let firstLine = state.src.slice(pos, max)

  if (silent) return true

  let found = false
  let lastLine = ''
  let next = start

  if (firstLine.trim().slice(-2) === '$$') {
    firstLine = firstLine.trim().slice(0, -2)
    found = true
  }

  while (!found) {
    next++
    if (next >= end) break

    pos = state.bMarks[next] + state.tShift[next]
    max = state.eMarks[next]

    if (pos < max && state.tShift[next] < state.blkIndent) break

    const line = state.src.slice(pos, max)
    if (line.trim().slice(-2) === '$$') {
      const lastPos = state.src.slice(0, max).lastIndexOf('$$')
      lastLine = state.src.slice(pos, lastPos)
      found = true
    }
  }

  state.line = next + 1
  const token = state.push('math_block', 'math', 0)
  token.block = true
  token.content = `${firstLine && firstLine.trim() ? `${firstLine}\n` : ''}${state.getLines(start + 1, next, state.tShift[start], true)}${lastLine && lastLine.trim() ? lastLine : ''}`
  token.map = [start, state.line]
  token.markup = '$$'
  return true
}

export default function katexModernPlugin(md, options = {}) {
  const baseOptions = { throwOnError: false, strict: false, ...options }

  md.inline.ruler.after('escape', 'math_inline', mathInline)
  md.block.ruler.after('blockquote', 'math_block', mathBlock, {
    alt: ['paragraph', 'reference', 'blockquote', 'list'],
  })

  md.renderer.rules.math_inline = (tokens, idx) => {
    return katex.renderToString(tokens[idx].content, { ...baseOptions, displayMode: false })
  }

  md.renderer.rules.math_block = (tokens, idx) => {
    return `${katex.renderToString(tokens[idx].content, { ...baseOptions, displayMode: true })}\n`
  }
}
