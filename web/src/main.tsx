import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import './styles/tokens.css'
import './styles/base.css'
import './styles/layout.css'
import './styles/components.css'
import './styles/tasks.css'
import './styles/write.css'

const host = document.getElementById('root')
if (!host) throw new Error('缺少 #root 挂载节点')

createRoot(host).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
