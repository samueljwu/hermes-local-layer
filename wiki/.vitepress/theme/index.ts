import { h } from 'vue'
import DefaultTheme from 'vitepress/theme'
import WikiGraph from './components/WikiGraph.vue'
import HermesTopbar from './components/HermesTopbar.vue'
import './custom.css'

export default {
  extends: DefaultTheme,
  Layout() {
    return h(DefaultTheme.Layout, null, {
      'layout-top': () => h(HermesTopbar),
    })
  },
  enhanceApp({ app }) {
    app.component('WikiGraph', WikiGraph)
  },
}
