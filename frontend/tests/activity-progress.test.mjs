import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import * as Vue from 'vue'
import { renderToString } from '@vue/server-renderer'
import { compileScript, compileStyle, parse } from '@vue/compiler-sfc'
import postcss from 'postcss'

const source = await readFile(new URL('../src/components/ActivityProgress.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const script = compileScript(descriptor, { id: 'progress-test', inlineTemplate: true, genDefaultAs: 'ActivityProgress' })
const code = script.content.replace(/import\s*\{([^}]+)\}\s*from\s*['"]vue['"];?/g,
  (_match, imports) => `const {${imports.replace(/\s+as\s+/g, ':')}} = Vue;`)
const ActivityProgress = new Function('Vue', `${code}\nreturn ActivityProgress`)(Vue)
const render = (props) => renderToString(Vue.createSSRApp({ render: () => Vue.h(ActivityProgress, props) }))

test('unknown totals do not claim a percentage or reuse the measured fill', async () => {
  for (const percent of [null, undefined, NaN, Infinity, '50']) {
    const html = await render({ status: 'running', progress: { percent, completed: 2, total: 4 } })
    assert.match(html, /class="progress-indeterminate"/)
    assert.doesNotMatch(html, /class="progress-fill"|aria-valuenow=/)
    assert.match(html, /总量未知/)
    assert.match(html, /已完成 2 \/ 4/)
    assert.match(html, /aria-busy="true"/)
  }
})

test('zero and measured percentages stay grounded in the supplied value', async () => {
  for (const [input, width, caption] of [[0, 0, 0], [42.5, 42.5, 42], [100, 99, 99], [-1, 0, 0]]) {
    const html = await render({ status: 'running', progress: { percent: input } })
    assert.match(html, new RegExp(`style="width:${width}%;"`))
    assert.match(html, new RegExp(`aria-valuenow="${caption}"`))
    assert.doesNotMatch(html, /class="progress-indeterminate"/)
  }
})

test('only success fills completely; failed or cancelled work keeps its actual progress', async () => {
  const success = await render({ status: 'success', progress: { percent: null } })
  assert.match(success, /class="progress-fill" style="width:100%;"/)
  assert.match(success, /aria-valuenow="100"/)
  assert.match(success, /aria-busy="false"/)
  for (const status of ['failed', 'cancelled', 'interrupted', 'queued']) {
    const unknown = await render({ status, progress: { percent: null } })
    assert.match(unknown, /style="width:0%;"/)
    assert.doesNotMatch(unknown, /progress-indeterminate|aria-valuenow=/)
    const partial = await render({ status, progress: { percent: 37 } })
    assert.match(partial, /style="width:37%;"/)
    assert.match(partial, /aria-valuenow="37"/)
  }
})

test('indeterminate travel spans left to right, including the explicit progress-only motion exception', () => {
  const compiled = compileStyle({ source: descriptor.styles[0].content, id: 'data-v-progress-test', scoped: true })
  assert.deepEqual(compiled.errors, [])
  const css = postcss.parse(compiled.code)
  const moves = []
  const keyframes = new Map()
  css.walkDecls((declaration) => {
    const rule = declaration.parent
    if (declaration.prop === 'animation') {
      assert.equal(rule.parent.type, 'root')
      assert.equal(declaration.important, true) // beat the global reduced-motion reset
      moves.push(declaration.value)
    }
    if (declaration.prop === 'transform') {
      assert.equal(rule.parent.name, 'keyframes')
      keyframes.set(rule.selector, declaration.value)
    }

  })
  assert.equal(moves.length, 1)
  assert.match(moves[0], /linear infinite/)
  assert.equal(keyframes.get('from'), 'translateX(-100%)')
  assert.ok(parseFloat(keyframes.get('to').match(/[\d.]+/)[0]) >= 100 / .3)
})
